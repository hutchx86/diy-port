#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 hutchx86
# Move a running AI Port to another Mac keeping its identity (same adopted device,
# camera pairings and settings in Protect). Stop pi-port on the old Mac first.
#
#   old Mac:  sudo scripts/macos_move_host.sh export ~/piport-move.tgz [--iface en0]
#   new Mac:  sudo scripts/macos_move_host.sh import ~/piport-move.tgz
#
# The bundle holds the state dir (adoption token, mTLS cert/key, face DB), the
# model files (not in git) and identity.env (advertised MAC + device_id). The MAC
# must be pinned with --mac on the new Mac: it is otherwise derived from that
# Mac's own interface, and Protect would see a different device.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
USER_HOME="$(eval echo "~${SUDO_USER:-$USER}")"
STATE_DIR="${PIPORT_STATE_DIR:-$USER_HOME/.piport-state}"

usage() { sed -n '5,13p' "$0" | sed 's/^# \{0,1\}//'; exit 1; }
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
    if pgrep -f 'piport/run_all.py' >/dev/null; then
        echo "pi-port is still running here; stop it first (two hosts with one identity fight)" >&2
        exit 1
    fi
    [ -f "$STATE_DIR/adopt_state.json" ] || { echo "no adoption in $STATE_DIR" >&2; exit 1; }
    tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
    # Identity as this host advertises it (MAC derived from the interface unless pinned).
    (cd "$ROOT/piport" && python3 - "$iface" > "$tmp/identity.env" <<'EOF'
import sys, config
cfg = config.load_config(None)
iface = config.resolve_iface(sys.argv[1] or None, cfg)
print("PIPORT_MAC=%s" % config.resolve_mac(iface, None, cfg))
print("PIPORT_DEVICE_ID=%s" % cfg["device_id"])
print("PIPORT_SOURCE_IFACE=%s" % iface)
EOF
    )
    mkdir -p "$tmp/state" "$tmp/models"
    cp -Rp "$STATE_DIR/." "$tmp/state/"
    cp -p "$ROOT"/piport/models/*.onnx "$tmp/models/" 2>/dev/null || true
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
    mkdir -p "$STATE_DIR" "$ROOT/piport/models"
    cp -Rp "$tmp/state/." "$STATE_DIR/"
    cp -p "$tmp"/models/*.onnx "$ROOT/piport/models/" 2>/dev/null || true
    cfg_id="$(sed -n 's/^device_id *= *//p' "$ROOT/piport/aiport.cfg")"
    echo "restored state to $STATE_DIR and models to piport/models"
    echo "identity: MAC $PIPORT_MAC, device_id $PIPORT_DEVICE_ID"
    [ "$cfg_id" = "$PIPORT_DEVICE_ID" ] || echo "NOTE: aiport.cfg device_id differs; the start command passes --device-id"
    model=""
    [ -f "$ROOT/piport/models/yolov5s.onnx" ] && model=" AIPORT_MODEL_PATH=$ROOT/piport/models/yolov5s.onnx"
    echo
    echo "start (pick --iface from 'route -n get default'; add AIPORT_BOX_DELAY_MS as on the old host):"
    echo "  cd $ROOT && sudo AIPORT_CONSOLE_HOST=<console-ip>$model caffeinate -is venv/bin/python piport/run_all.py --iface en0 --state-dir $STATE_DIR --mac $PIPORT_MAC --device-id $PIPORT_DEVICE_ID"
    ;;
*) usage ;;
esac
