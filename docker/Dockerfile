# syntax=docker/dockerfile:1
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 hutchx86
#
# AI Port emulator image (linux/amd64 + linux/arm64). Build from the repo root:
#
#   docker build -f docker/Dockerfile -t piport-cpu .                       # CPU
#   docker build -f docker/Dockerfile --build-arg ACCEL=edgetpu -t piport .  # Coral
#
# ACCEL picks the detector backend baked in: cpu (default) | edgetpu | none.
# The RK3588 NPU backend is not offered in the image -- use scripts/install.sh
# on that board. See docker/README.md.

FROM debian:trixie-slim

ARG ACCEL=cpu
ARG TARGETARCH
# Same pinned libedgetpu build and digests as scripts/install.sh.
ARG LIBEDGETPU_TAG=16.0TF2.19.1-1
ARG LIBEDGETPU_VER=16.0tf2.19.1-1
ARG LIBEDGETPU_SHA256_ARM64=df9e9b16c802f4e4ad6fd8d9d0866d25eff8ddef989813f398794546d267bd11
ARG LIBEDGETPU_SHA256_AMD64=764d1bc680f65440ca1c8aca1d33ce986d7ca652b984ae0fc744d04b2f896e53

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# hadolint ignore=DL3008
RUN case "$ACCEL" in cpu|edgetpu|none) ;; *) echo "ACCEL must be cpu|edgetpu|none" >&2; exit 1 ;; esac \
 && apt-get update \
 && apt-get install -y --no-install-recommends \
        python3 python3-venv ffmpeg iproute2 procps ca-certificates curl \
 && if [ "$ACCEL" = "edgetpu" ]; then \
        case "$TARGETARCH" in \
            arm64) sha="$LIBEDGETPU_SHA256_ARM64" ;; \
            amd64) sha="$LIBEDGETPU_SHA256_AMD64" ;; \
            *) echo "edgetpu: unsupported arch '$TARGETARCH'" >&2; exit 1 ;; \
        esac; \
        deb="libedgetpu1-std_${LIBEDGETPU_VER}.trixie_${TARGETARCH}.deb"; \
        curl -fsSL -o "/tmp/$deb" \
            "https://github.com/feranick/libedgetpu/releases/download/${LIBEDGETPU_TAG}/${deb}"; \
        echo "$sha  /tmp/$deb" | sha256sum -c -; \
        apt-get install -y --no-install-recommends "/tmp/$deb"; \
        rm -f "/tmp/$deb"; \
    fi \
 && rm -rf /var/lib/apt/lists/*

ENV VIRTUAL_ENV=/opt/venv PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
COPY piport/requirements.txt /tmp/requirements.txt
# hadolint ignore=DL3013
RUN python3 -m venv "$VIRTUAL_ENV" \
 && pip install --no-cache-dir -r /tmp/requirements.txt \
 && case "$ACCEL" in \
        edgetpu) pip install --no-cache-dir ai-edge-litert ;; \
        cpu)     pip install --no-cache-dir onnxruntime ;; \
    esac \
 && rm -f /tmp/requirements.txt

WORKDIR /app
COPY piport/ piport/
COPY scripts/fetch_models.py scripts/fetch_models.py
COPY docker/entrypoint.py docker/entrypoint.py
RUN rm -rf piport/tests piport/__pycache__ \
 && if [ "$ACCEL" != "none" ]; then python3 scripts/fetch_models.py --backend "$ACCEL"; fi

# avclient.py reads this ("none" turns detection off).
ENV AIPORT_DETECTOR=${ACCEL}
VOLUME /data
EXPOSE 10001/udp 443/tcp 8090/tcp

# Stays root (binds :443/:10001). SIGINT so run_all.py reaps its four children.
STOPSIGNAL SIGINT
ENTRYPOINT ["python3", "piport/run_all.py"]
