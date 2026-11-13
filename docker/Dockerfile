# syntax=docker/dockerfile:1
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 hutchx86
#
# AI Port emulator image (linux/amd64 + linux/arm64). Build from the repo root:
#
#   docker build -f docker/Dockerfile -t diyport-cpu .                       # CPU
#   docker build -f docker/Dockerfile --build-arg ACCEL=edgetpu -t diyport .  # Coral
#   docker build -f docker/Dockerfile --build-arg ACCEL=openvino -t diyport-openvino .  # Intel GPU
#
# ACCEL picks the detector backend baked in: cpu (default) | edgetpu | openvino
# (amd64 only; run with --device /dev/dri) | none.
# FACES=1 adds face detection/recognition (OpenCV + YuNet/SFace, diyport/enrich.py); off by default:
#   docker build -f docker/Dockerfile --build-arg ACCEL=openvino --build-arg FACES=1 -t diyport-openvino .
# The RK3588 NPU backend is not offered in the image -- use scripts/install.sh
# on that board. See docker/README.md.

FROM debian:trixie-slim

ARG ACCEL=cpu
ARG FACES=0
ARG TARGETARCH
# Same pinned libedgetpu build and digests as scripts/install.sh.
ARG LIBEDGETPU_TAG=16.0TF2.19.1-1
ARG LIBEDGETPU_VER=16.0tf2.19.1-1
ARG LIBEDGETPU_SHA256_ARM64=df9e9b16c802f4e4ad6fd8d9d0866d25eff8ddef989813f398794546d267bd11
ARG LIBEDGETPU_SHA256_AMD64=764d1bc680f65440ca1c8aca1d33ce986d7ca652b984ae0fc744d04b2f896e53
# Intel GPU compute runtime for openvino (trixie has no intel-opencl-icd); same pinned
# compute-runtime/IGC debs and digests as scripts/install.sh.
ARG NEO_TAG=26.35.39758.10
ARG IGC_TAG=v2.41.5
ARG IGC_VER=2.41.5+22716
ARG GMMLIB_VER=22.10.0

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# DL3003 is the temp dir used to install the pinned Intel debs.
# hadolint ignore=DL3008,DL3003
RUN case "$ACCEL" in cpu|edgetpu|openvino|none) ;; *) echo "ACCEL must be cpu|edgetpu|openvino|none" >&2; exit 1 ;; esac \
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
 && if [ "$ACCEL" = "openvino" ]; then \
        [ "$TARGETARCH" = "amd64" ] || { echo "openvino: amd64 only, got '$TARGETARCH'" >&2; exit 1; }; \
        igc="https://github.com/intel/intel-graphics-compiler/releases/download/${IGC_TAG}"; \
        neo="https://github.com/intel/compute-runtime/releases/download/${NEO_TAG}"; \
        mkdir /tmp/neo && cd /tmp/neo; \
        for url in "$igc/intel-igc-core-2_${IGC_VER}_amd64.deb" \
                   "$igc/intel-igc-opencl-2_${IGC_VER}_amd64.deb" \
                   "$neo/intel-opencl-icd_${NEO_TAG}-0_amd64.deb" \
                   "$neo/libigdgmm12_${GMMLIB_VER}_amd64.deb"; do \
            curl -fsSLO "$url"; \
        done; \
        printf '%s\n' \
            "0a6e64a663ae65a0fa02d6912ae3b6b37cf85b90c21cc423fd9fef70aaf4f628  intel-igc-core-2_${IGC_VER}_amd64.deb" \
            "779e1b9e88098eb25711e9a8f67c2752665bad22f134aa40ed5649f6e1b87058  intel-igc-opencl-2_${IGC_VER}_amd64.deb" \
            "61712caaddeba3d38e4f79e2a0fb23fea25596ca2d72c3144c6eea2331ec4301  intel-opencl-icd_${NEO_TAG}-0_amd64.deb" \
            "6031a63d6e8a12ce61c14efc15f2c8e727061286e3820b8594e6d00615e04d54  libigdgmm12_${GMMLIB_VER}_amd64.deb" \
            | sha256sum -c -; \
        apt-get install -y --no-install-recommends /tmp/neo/*.deb; \
        cd / && rm -rf /tmp/neo; \
    fi \
 && rm -rf /var/lib/apt/lists/*

ENV VIRTUAL_ENV=/opt/venv PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
COPY diyport/requirements.txt /tmp/requirements.txt
# hadolint ignore=DL3013
RUN python3 -m venv "$VIRTUAL_ENV" \
 && pip install --no-cache-dir -r /tmp/requirements.txt \
 && case "$ACCEL" in \
        edgetpu) pip install --no-cache-dir ai-edge-litert ;; \
        openvino) pip install --no-cache-dir openvino ;; \
        cpu)     pip install --no-cache-dir onnxruntime ;; \
    esac \
 && case "$FACES" in \
        1) pip install --no-cache-dir opencv-python-headless ;; \
        0) ;; \
        *) echo "FACES must be 0 or 1" >&2; exit 1 ;; \
    esac \
 && rm -f /tmp/requirements.txt

WORKDIR /app
COPY diyport/ diyport/
COPY scripts/fetch_models.py scripts/fetch_models.py
COPY scripts/detector_check.py scripts/detector_check.py
COPY docker/entrypoint.py docker/entrypoint.py
RUN rm -rf diyport/tests diyport/__pycache__ \
 && if [ "$ACCEL" != "none" ]; then python3 scripts/fetch_models.py --backend "$ACCEL"; fi \
 && if [ "$FACES" = 1 ]; then python3 scripts/fetch_models.py --face-models-only; fi

# avclient.py reads this ("none" turns detection off).
ENV AIPORT_DETECTOR=${ACCEL}
# OpenVINO's compiled-model cache on the /data volume, so a recreated container skips the
# GPU compile (detectors/openvino.py; other backends ignore it).
ENV AIPORT_OV_CACHE_DIR=/data/openvino_cache
VOLUME /data
EXPOSE 10001/udp 443/tcp 8090/tcp

# Stays root (binds :443/:10001). SIGINT so run_all.py reaps its four children.
STOPSIGNAL SIGINT
ENTRYPOINT ["python3", "diyport/run_all.py"]
