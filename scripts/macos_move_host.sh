#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The DIY-Port project
# Move a running AI Port to another Mac with its identity: export | import.
# See diyport/README.md (the MAC must be pinned on the new host).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
USER_HOME="$(eval echo "~${SUDO_USER:-$USER}")"
STATE_DIR="${DIYPORT_STATE_DIR:-$USER_HOME/.diyport-state}"

usage() {
    echo "usage: $0 export <bundle.tgz> [--iface IFACE] [--state-dir DIR] | import <bundle.tgz>" >&2
    exit 1
}
[ $# -ge 2 ] || usage
cmd="$1"; bundle="$2"; shift 2
iface=""
while [ $# -gt 0 ]; do
    case "$1" in
        --iface) iface="$2"; shift 2 ;;
        --state-dir) STATE_DIR="$2"; shift 2 ;;
        *) usage ;;
    esac
done
[ "$(id -u)" -eq 0 ] || { echo "run with sudo (the state dir is root-owned)" >&2; exit 1; }

case "$cmd" in
export)
    if pgrep -f 'diyport/run_all.py' >/dev/null; then
        echo "diy-port is still running here; stop it first (two hosts with one identity fight)" >&2
        exit 1
    fi
    [ -f "$STATE_DIR/adopt_state.json" ] || { echo "no adoption in $STATE_DIR" >&2; exit 1; }
    tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
    # Identity as this host advertises it (MAC derived from the interface unless pinned).
    (cd "$ROOT/diyport" && python3 - "$iface" > "$tmp/identity.env" <<'EOF'
import sys, config
cfg = config.load_config(None)
iface = config.resolve_iface(sys.argv[1] or None, cfg)
print("DIYPORT_MAC=%s" % config.resolve_mac(iface, None, cfg))
print("DIYPORT_DEVICE_ID=%s" % cfg["device_id"])
print("DIYPORT_SOURCE_IFACE=%s" % iface)
EOF
    )
    mkdir -p "$tmp/state" "$tmp/models"
    cp -Rp "$STATE_DIR/." "$tmp/state/"
    cp -p "$ROOT"/diyport/models/*.onnx "$tmp/models/" 2>/dev/null || true
    tar -czf "$bundle" -C "$tmp" identity.env state models
    chown "${SUDO_USER:-$USER}" "$bundle"
    chmod 600 "$bundle"   # holds the adoption token and private key
    echo "wrote $bundle:"; cat "$tmp/identity.env"
    echo "it contains the device's private key and adoption token: copy it securely"
    ;;
import)
    [ -f "$bundle" ] || { echo "no bundle at $bundle" >&2; exit 1; }
    tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
    tar -xzf "$bundle" -C "$tmp"
    # shellcheck disable=SC1091
    . "$tmp/identity.env"
    if [ -e "$STATE_DIR/adopt_state.json" ]; then
        mv "$STATE_DIR" "$STATE_DIR.before-import.$(date +%Y%m%d%H%M%S)"
    fi
    mkdir -p "$STATE_DIR" "$ROOT/diyport/models"
    cp -Rp "$tmp/state/." "$STATE_DIR/"
    cp -p "$tmp"/models/*.onnx "$ROOT/diyport/models/" 2>/dev/null || true
    cfg_id="$(sed -n 's/^device_id *= *//p' "$ROOT/diyport/aiport.cfg")"
    echo "restored state to $STATE_DIR and models to diyport/models"
    echo "identity: MAC $DIYPORT_MAC, device_id $DIYPORT_DEVICE_ID"
    [ "$cfg_id" = "$DIYPORT_DEVICE_ID" ] || echo "NOTE: aiport.cfg device_id differs; the start command passes --device-id"
    model=""
    [ -f "$ROOT/diyport/models/yolov5s.onnx" ] && model=" AIPORT_MODEL_PATH=$ROOT/diyport/models/yolov5s.onnx"
    echo
    echo "start (pick --iface from 'route -n get default'; add AIPORT_BOX_DELAY_MS as on the old host):"
    echo "  cd $ROOT && sudo AIPORT_CONSOLE_HOST=<console-ip>$model caffeinate -is venv/bin/python diyport/run_all.py --iface en0 --state-dir $STATE_DIR --mac $DIYPORT_MAC --device-id $DIYPORT_DEVICE_ID"
    ;;
*) usage ;;
esac
