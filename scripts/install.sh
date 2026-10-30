#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 hutchx86
#
# One-shot installer: apt deps, venv + backend, model assets, librknnrt, systemd
# instance and web UI. Run with --help; see README.md.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DIYPORT_DIR="$REPO_ROOT/diyport"
VENV="$DIYPORT_DIR/.venv"
PY="$VENV/bin/python"
CFG="$DIYPORT_DIR/aiport.cfg"
RKNN_DEST="$DIYPORT_DIR/models/yolov5s_relu.rknn"
SYSTEMD_DIR="/etc/systemd/system"
RKNNRT_URL="https://raw.githubusercontent.com/airockchip/rknn-toolkit2/v2.3.2/rknpu2/runtime/Linux/librknn_api/aarch64/librknnrt.so"
# Pin the runtime blob too; the URL is a mutable tag on a third-party repo.
RKNNRT_SHA256="d31fc19c85b85f6091b2bd0f6af9d962d5264a4e410bfb536402ec92bac738e8"

# Known-good NPU model (RKNN 2.3.2/rk3588, 3 sigmoid heads); not committed --
# it lives in the separate AGPL-3.0 model repo. Empty URL disables the download.
DEFAULT_RKNN_URL="https://raw.githubusercontent.com/hutchx86/pi-port-models/v1/yolov5s_relu.rknn"
DEFAULT_RKNN_SHA256="f4fd0122ecca3117323b8268d270e6cc293e364d6a9dda8e83293d64c8377a3c"

# libedgetpu1-std from feranick/libedgetpu (TF 2.19.1, verified with ai-edge-litert
# 2.3.0); sha256 per <distro>_<arch> from the release digests.
LIBEDGETPU_TAG="16.0TF2.19.1-1"
LIBEDGETPU_VER="16.0tf2.19.1-1"
declare -A LIBEDGETPU_SHA256=(
    [bookworm_amd64]=23be53c72eff4d44afc2f727700da185791d3ca0867bd0b5e082ec3a0de21925
    [bookworm_arm64]=46ab47310d6de120bda4678febe5eb6143f5c7d7dd17c0d017ae5f6cd67dfec5
    [bullseye_amd64]=35ede313bb3883efcd1ce981713e8b57de5d368ce5618bd2dcc9fc83268c29e6
    [bullseye_arm64]=09ae76e712c5606788fa516cf85505343d0f931d010d7433bddee47f65ce8c93
    [trixie_amd64]=764d1bc680f65440ca1c8aca1d33ce986d7ca652b984ae0fc744d04b2f896e53
    [trixie_arm64]=df9e9b16c802f4e4ad6fd8d9d0866d25eff8ddef989813f398794546d267bd11
    [ubuntu22.04_amd64]=a8a254d241ab6c18c1932f6e725d61a2721097b9c65d20ca3c31b3298d817128
    [ubuntu22.04_arm64]=974bf41b59831817ca674e64a8bc857d84564cb25513da1a18711c50d3351377
    [ubuntu24.04_amd64]=5010e68411e22cafb135950c7322c769d4041fa5c2d2c11eaf638500fde3f357
    [ubuntu24.04_arm64]=308479366bd0c1b78b732a8eb5e60dc329c618917a0b4c8f0b19b1a50e9ca5c8
)

NAME="main"
PARENT_IFACE=""
CONSOLE=""
RKNN=""
RKNN_URL=""
RKNN_SHA256=""
WEBUI_PORT="8090"
ACCEL="auto"

log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mWARN:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

verify_sha256() {  # verify_sha256 <file> <expected-hex>
    local got expected
    got="$(sha256sum "$1" | awk '{print $1}')"
    expected="$(printf '%s' "$2" | tr '[:upper:]' '[:lower:]')"
    [ "$got" = "$expected" ]
}

usage() {
    cat <<'EOF'
Usage: sudo scripts/install.sh --console <UNVR-IP> [options]

  --console HOST[:PORT]   UniFi Protect console address (written to aiport.cfg)
  --parent-iface IFACE    NIC the macvlan instance is built on (alias --iface)
  --name NAME             instance name (default: main)
  --accel BACKEND         detector backend: auto|rknn|edgetpu|cpu|none
                          (auto: rknn on RK3588, edgetpu with a Coral, else cpu)
  --rknn PATH             prebuilt yolov5s_relu.rknn to install
  --rknn-url URL          download the .rknn from URL (overrides DIYPORT_RKNN_URL)
  --rknn-sha256 HEX       verify a downloaded/supplied .rknn
  --webui-port N          web UI listen port (default: 8090)
  --no-npu                same as --accel none: no detector runtime or model
  -h, --help
EOF
}

ORIG_ARGS=("$@")
while [ $# -gt 0 ]; do
    case "$1" in
        --name)         NAME="${2:?}"; shift 2 ;;
        --parent-iface|--iface) PARENT_IFACE="${2:?}"; shift 2 ;;
        --console)      CONSOLE="${2:?}"; shift 2 ;;
        --rknn)         RKNN="${2:?}"; shift 2 ;;
        --rknn-url)     RKNN_URL="${2:?}"; shift 2 ;;
        --rknn-sha256)  RKNN_SHA256="${2:?}"; shift 2 ;;
        --webui-port)   WEBUI_PORT="${2:?}"; shift 2 ;;
        --accel)        ACCEL="${2:?}"; shift 2 ;;
        --no-npu)       ACCEL="none"; shift ;;
        -h|--help)      usage; exit 0 ;;
        *)              die "unknown argument: $1 (see --help)" ;;
    esac
done

[ "$(id -u)" -eq 0 ] || die "run as root: sudo $0 ${ORIG_ARGS[*]}"
[ -f "$DIYPORT_DIR/run_all.py" ] || die "diyport/ not found under $REPO_ROOT -- run this from the repo"
case "$NAME" in *[!a-zA-Z0-9_-]*|"") die "invalid --name '$NAME' (letters/digits/-/_ only, max 12)" ;; esac
[ "${#NAME}" -le 12 ] || die "--name must be <= 12 characters (becomes a Linux interface name)"

# Same USB ids as detectors/edgetpu.py: 1a6e:089a before libedgetpu loads the
# firmware, 18d1:9302 after.
coral_present() {
    local d
    for d in /sys/bus/usb/devices/*; do
        [ -f "$d/idVendor" ] || continue
        case "$(cat "$d/idVendor"):$(cat "$d/idProduct")" in
            1a6e:089a|18d1:9302) return 0 ;;
        esac
    done
    [ -e /dev/apex_0 ]
}

ARCH="$(uname -m)"
case "$ACCEL" in
    auto)
        if tr -d '\0' < /proc/device-tree/compatible 2>/dev/null | grep -q rk3588; then
            ACCEL="rknn"
        elif coral_present; then
            ACCEL="edgetpu"
        else
            ACCEL="cpu"
        fi
        log "auto-detected detector backend: $ACCEL" ;;
    rknn|edgetpu|cpu|none) ;;
    *) die "invalid --accel '$ACCEL' (auto|rknn|edgetpu|cpu|none)" ;;
esac
if [ "$ACCEL" = "rknn" ] && [ "$ARCH" != "aarch64" ]; then
    die "--accel rknn needs an aarch64 RK3588 board, got $ARCH; use --accel cpu or edgetpu (the Docker stack is in docker/README.md)"
fi
if [ "$ACCEL" = "edgetpu" ]; then
    case "$ARCH" in
        aarch64) DEB_ARCH="arm64" ;;
        x86_64)  DEB_ARCH="amd64" ;;
        *) die "--accel edgetpu supports aarch64/x86_64, got $ARCH" ;;
    esac
    coral_present || warn "no Coral found on USB (or /dev/apex_0); installing anyway -- plug it in before starting"
fi

log "installing system packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq --no-install-recommends \
    python3-venv python3-pip ffmpeg isc-dhcp-client iproute2 curl ca-certificates diffutils git

if [ ! -x "$PY" ]; then
    log "creating venv at $VENV"
    python3 -m venv "$VENV"
fi
log "installing Python dependencies"
"$PY" -m pip install --quiet --upgrade pip
"$PY" -m pip install --quiet -r "$DIYPORT_DIR/requirements.txt"
case "$ACCEL" in
    rknn)    log "installing rknn-toolkit-lite2"; "$PY" -m pip install --quiet rknn-toolkit-lite2 ;;
    edgetpu) log "installing ai-edge-litert";     "$PY" -m pip install --quiet ai-edge-litert ;;
    cpu)     log "installing onnxruntime";        "$PY" -m pip install --quiet onnxruntime ;;
esac

if [ "$ACCEL" = "edgetpu" ] || [ "$ACCEL" = "cpu" ]; then
    log "fetching the $ACCEL model (binaries are gitignored)"
    "$PY" "$REPO_ROOT/scripts/fetch_models.py" --backend "$ACCEL"
fi

if [ "$ACCEL" = "edgetpu" ]; then
    # "|| true": dpkg-query exits 1 when either name is unknown, which pipefail would propagate.
    if { dpkg-query -W -f='${Status}\n' libedgetpu1-std libedgetpu1-max 2>/dev/null || true; } \
            | grep -q "install ok installed"; then
        log "libedgetpu already installed"
    else
        # shellcheck disable=SC1091
        . /etc/os-release
        distro="${VERSION_CODENAME:-}"
        [ "${ID:-}" = "ubuntu" ] && distro="ubuntu${VERSION_ID:-}"
        key="${distro}_${DEB_ARCH}"
        sha="${LIBEDGETPU_SHA256[$key]:-}"
        [ -n "$sha" ] || die "no libedgetpu build for '$key' (have: ${!LIBEDGETPU_SHA256[*]}); install libedgetpu1-std yourself and re-run"
        deb="libedgetpu1-std_${LIBEDGETPU_VER}.${distro}_${DEB_ARCH}.deb"
        tmpdir="$(mktemp -d)"
        log "installing $deb"
        curl -fsSL -o "$tmpdir/$deb" \
            "https://github.com/feranick/libedgetpu/releases/download/${LIBEDGETPU_TAG}/${deb}" \
            || die "failed to download $deb"
        verify_sha256 "$tmpdir/$deb" "$sha" || die "$deb failed sha256 verification"
        apt-get install -y -qq "$tmpdir/$deb"
        rm -rf "$tmpdir"
        # Its udev rule only applies on (re)plug; trigger so a Coral already in works now.
        udevadm control --reload-rules && udevadm trigger || true
    fi
fi

if [ "$ACCEL" = "rknn" ]; then
    log "fetching model assets (ONNX + labels/anchors; binaries are gitignored)"
    # --no-cpu-model: the CPU yolov5n.onnx is for x86/arm64/macOS, not this NPU board.
    "$PY" "$REPO_ROOT/scripts/fetch_models.py" --no-cpu-model

    # Source precedence: --rknn file > local copy > download, where
    # the download URL is --rknn-url > DIYPORT_RKNN_URL > DEFAULT_RKNN_URL.
    rknn_url="${RKNN_URL:-${DIYPORT_RKNN_URL:-$DEFAULT_RKNN_URL}}"
    verified=0

    # (a) explicit local build
    if [ -n "$RKNN" ]; then
        [ -f "$RKNN" ] || die "--rknn '$RKNN' does not exist"
        install -m 644 "$RKNN" "$RKNN_DEST"
        log "installed NPU model from $RKNN"
    fi

    # (b) local copy: models-rknn/ (linked model repo) or a hand-built
    # rknn_convert/ copy. Only if nothing is installed yet.
    if [ ! -f "$RKNN_DEST" ]; then
        if [ -f "$REPO_ROOT/.gitmodules" ] && command -v git >/dev/null 2>&1; then
            log "initializing model submodule(s)"
            git -c "safe.directory=$REPO_ROOT" -C "$REPO_ROOT" \
                submodule update --init --recursive >/dev/null 2>&1 \
                || warn "git submodule update failed; falling back to URL/existing model"
        fi
        for cand in "$REPO_ROOT/models-rknn/yolov5s_relu.rknn" \
                    "$REPO_ROOT/rknn_convert/yolov5s_relu.rknn"; do
            if [ -f "$cand" ]; then
                install -m 644 "$cand" "$RKNN_DEST"
                log "using local NPU model $cand"
                break
            fi
        done
    fi

    # An explicit --rknn-sha256 must match the supplied model; drop a rejected file
    # so a later re-run doesn't mistake it for a good install.
    if [ -n "$RKNN_SHA256" ] && [ -f "$RKNN_DEST" ]; then
        if ! verify_sha256 "$RKNN_DEST" "$RKNN_SHA256"; then
            rm -f "$RKNN_DEST"
            die "NPU model sha256 mismatch against --rknn-sha256"
        fi
        log "NPU model sha256 verified"
        verified=1
    fi

    # (c) download. Atomic: fetch to a temp file, verify, then install -- so an
    # interrupted/killed download can never leave a partial model in place.
    if [ ! -f "$RKNN_DEST" ] && [ -n "$rknn_url" ]; then
        log "downloading NPU model from $rknn_url"
        tmp="$(mktemp)"
        if ! curl -fL -o "$tmp" "$rknn_url"; then
            rm -f "$tmp"
            if [ "$rknn_url" = "$DEFAULT_RKNN_URL" ]; then
                warn "default NPU model download failed (offline, or URL not published yet)"
            else
                die "failed to download NPU model from $rknn_url"
            fi
        else
            # The published digest is the expectation for the default URL.
            dl_sha="$RKNN_SHA256"
            if [ -z "$dl_sha" ] && [ -n "$rknn_url" ] && [ "$rknn_url" = "$DEFAULT_RKNN_URL" ]; then
                dl_sha="$DEFAULT_RKNN_SHA256"
            fi
            if [ -n "$dl_sha" ] && ! verify_sha256 "$tmp" "$dl_sha"; then
                rm -f "$tmp"
                die "downloaded NPU model failed sha256 verification ($rknn_url)"
            fi
            if [ -n "$dl_sha" ]; then
                verified=1
            fi
            install -m 644 "$tmp" "$RKNN_DEST"
            rm -f "$tmp"
            log "installed downloaded NPU model"
        fi
    fi

    if [ ! -f "$RKNN_DEST" ]; then
        cat >&2 <<'EOF'
ERROR: diyport/models/yolov5s_relu.rknn is missing.

The .rknn cannot be built on the Pi -- rknn-toolkit2 (the converter) is
x86_64-only. On an x86_64 Linux box:

    python3 scripts/fetch_models.py --convert

then copy diyport/models/yolov5s_relu.rknn to this Pi, or re-run with
  --rknn <path>   /   --rknn-url <url>   /   DIYPORT_RKNN_URL=<url> ...

To install the protocol stack now and add NPU detection later, re-run with
  --accel none
EOF
        exit 1
    fi
    if [ "$verified" -eq 0 ]; then
        log "NPU model installed without a pinned digest; pass --rknn-sha256 to verify"
    fi
fi

# librknnrt.so must match rknn-toolkit2 2.3.2; the stock OS version is too old.
if [ "$ACCEL" = "rknn" ]; then
    lib="$(ldconfig -p 2>/dev/null | awk '/librknnrt\.so/{print $NF; exit}' || true)"
    lib="${lib:-/usr/lib/librknnrt.so}"
    tmp="$(mktemp)"
    log "fetching librknnrt.so (v2.3.2 runtime)"
    if ! curl -fL -o "$tmp" "$RKNNRT_URL"; then
        rm -f "$tmp"
        die "failed to download librknnrt.so from $RKNNRT_URL (offline?)"
    fi
    if ! verify_sha256 "$tmp" "$RKNNRT_SHA256"; then
        rm -f "$tmp"
        die "downloaded librknnrt.so failed sha256 verification"
    fi
    if [ -f "$lib" ] && cmp -s "$tmp" "$lib"; then
        log "system librknnrt.so already current ($lib)"
    else
        backup="/root/librknnrt.so.orig-backup"
        if [ -f "$lib" ] && [ ! -f "$backup" ]; then
            cp -a "$lib" "$backup"
            log "backed up original librknnrt.so -> $backup"
        fi
        install -m 755 "$tmp" "$lib"
        ldconfig
        log "installed librknnrt.so -> $lib"
    fi
    rm -f "$tmp"
fi

set_ini() {  # set_ini <section> <key> <value> <file>; adds the key/section if missing
    local section="$1" key="$2" value="$3" file="$4" tmp
    tmp="$(mktemp)"
    awk -v sec="$section" -v k="$key" -v v="$value" '
        /^\[/ {
            if (insec && !done) { print k " = " v; done = 1 }
            insec = ($0 == "[" sec "]"); if (insec) seen = 1
        }
        insec && $0 ~ ("^" k "[ \t]*=") { print k " = " v; done = 1; next }
        { print }
        END {
            if (insec && !done) { print k " = " v; done = 1 }
            if (!seen) { print ""; print "[" sec "]"; print k " = " v }
        }
    ' "$file" > "$tmp"
    cat "$tmp" > "$file"
    rm -f "$tmp"
}

[ -f "$CFG.bak-preinstall" ] || cp -a "$CFG" "$CFG.bak-preinstall"
set_ini hardware detector "$ACCEL" "$CFG"
log "set [hardware] detector=$ACCEL in aiport.cfg"
if [ -n "$CONSOLE" ]; then
    console_host="${CONSOLE%%:*}"
    console_port="7442"
    if [ "$CONSOLE" != "$console_host" ]; then
        console_port="${CONSOLE##*:}"
    fi
    set_ini console host "$console_host" "$CFG"
    set_ini console port "$console_port" "$CFG"
    log "set [console] host=$console_host port=$console_port in aiport.cfg"
fi
if [ -n "$PARENT_IFACE" ]; then
    set_ini network parent_iface "$PARENT_IFACE" "$CFG"
    log "set [network] parent_iface=$PARENT_IFACE in aiport.cfg"
fi

cfg_host="$("$PY" -c "import sys; sys.path.insert(0, '$DIYPORT_DIR'); import config; print(config.load_config()['host'])")"
if [ -z "$cfg_host" ]; then
    die "no console host configured; pass --console <UNVR-IP> or set [console] host in $CFG"
fi

INSTANCE_UNIT="$SYSTEMD_DIR/aiport-instance-${NAME}.service"
INSTANCE_IFACE="ap-${NAME}"
INSTANCE_IFACE="${INSTANCE_IFACE:0:15}"
if [ -f "$INSTANCE_UNIT" ]; then
    log "instance '$NAME' already exists; refreshing unit + restarting"
    "$PY" "$DIYPORT_DIR/instance_manager.py" refresh "$NAME"
else
    log "creating macvlan instance '$NAME' (systemd unit: aiport-instance-${NAME}.service)"
    im_args=(create "$NAME")
    if [ -n "$PARENT_IFACE" ]; then
        im_args+=(--parent-iface "$PARENT_IFACE")
    fi
    "$PY" "$DIYPORT_DIR/instance_manager.py" "${im_args[@]}"
fi

# dhclient exits 0 on timeout, so poll for a real IP instead of trusting
# systemctl "active"; a restart recreates the macvlan before the lease appears.
inst_ip=""
for _ in $(seq 1 20); do
    inst_ip="$(ip -4 -o addr show dev "$INSTANCE_IFACE" 2>/dev/null | awk '{print $4}' | cut -d/ -f1 | head -n1 || true)"
    if [ -n "$inst_ip" ]; then
        break
    fi
    sleep 0.5
done

log "writing web UI unit (diyport-webui.service), port $WEBUI_PORT"
cat > "$SYSTEMD_DIR/diyport-webui.service" <<EOF
[Unit]
Description=DIY Port web UI (sysinfo + instance manager)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$DIYPORT_DIR
ExecStart=$PY $DIYPORT_DIR/sysinfo_server.py --bind-ip 0.0.0.0 --port $WEBUI_PORT
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now diyport-webui.service
systemctl restart diyport-webui.service

box_ip="$(ip -4 -o addr show scope global 2>/dev/null | awk '{print $4}' | cut -d/ -f1 | head -n1 || true)"
sleep 1
if curl -fsS "http://127.0.0.1:${WEBUI_PORT}/api/stats" >/dev/null 2>&1; then
    webui_state="up"
else
    webui_state="NOT responding (check: journalctl -u diyport-webui -n 50)"
fi

if [ -n "$inst_ip" ]; then
    instance_state="active, $inst_ip"
else
    instance_state="running but NO IP on $INSTANCE_IFACE (DHCP failed? journalctl -u aiport-instance-${NAME})"
fi

cat <<EOF

============================================================
DIY Port installed.

  console        : $cfg_host
  instance unit  : aiport-instance-${NAME}.service  ($instance_state)
  web UI         : http://${box_ip:-<box-ip>}:${WEBUI_PORT}   [$webui_state]
  logs           : journalctl -u aiport-instance-${NAME} -f
                   journalctl -u diyport-webui -f

Next: adopt "AI Port" from the Protect console UI, then pair a camera.
Re-run safely any time to reinstall/update; the instance identity is preserved.
============================================================
EOF
case "$ACCEL" in
    rknn)    echo "Note: the distro ffmpeg (software decode) is installed; swap in a Rockchip"
             echo "rkmpp build for hardware H.264 decode." ;;
    edgetpu) echo "Note: a USB Coral is fastest on a USB 3 port; if it ever fails to open"
             echo "(\"Failed to load delegate\"), unplug and replug it." ;;
    none)    echo "Note: no detector backend installed; detection is disabled." ;;
esac
