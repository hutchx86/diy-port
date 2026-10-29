#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 hutchx86
#
# Build the CPU image and run the no-hardware smoke test on host networking.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$HERE")"
cd "$ROOT"

IMAGE="${IMAGE:-piport-cpu}"
NAME="${NAME:-piport-cpu-smoke}"
WAIT="${WAIT:-12}"
LOG="$(mktemp)"
BUILD_ARGS=()
[ -n "${DOCKER_PLATFORM:-}" ] && BUILD_ARGS+=(--platform "$DOCKER_PLATFORM")

# shellcheck disable=SC2329  # invoked via the EXIT trap below
cleanup() {
    docker rm -f "$NAME" >/dev/null 2>&1 || true
    rm -f "$LOG"
}
trap cleanup EXIT

command -v docker >/dev/null 2>&1 || { echo "ERROR: docker not found" >&2; exit 1; }

echo "== docker build -t $IMAGE . (context $ROOT) =="
docker build "${BUILD_ARGS[@]}" --build-arg ACCEL=cpu -f docker/Dockerfile -t "$IMAGE" .

docker rm -f "$NAME" >/dev/null 2>&1 || true
echo "== starting $NAME (host networking) =="
docker run -d --name "$NAME" --network host --cap-add NET_ADMIN "$IMAGE" >/dev/null

echo "== waiting ${WAIT}s =="
sleep "$WAIT"

running="$(docker inspect -f '{{.State.Running}}' "$NAME" 2>/dev/null || echo false)"
docker logs "$NAME" > "$LOG" 2>&1 || true
echo "== logs (tail) =="
tail -n 60 "$LOG"

fail=0
if [ "$running" != "true" ]; then
    echo "FAIL: container is not running" >&2
    fail=1
fi

# Every component must be alive inside the container.
if [ "$fail" -eq 0 ]; then
    if docker exec -i "$NAME" python3 - <<'PY'
import glob
import sys

want = ["discovery.py", "http_api.py", "ucp4_client.py", "avclient.py"]
alive = {w: 0 for w in want}
for p in glob.glob("/proc/[0-9]*/cmdline"):
    try:
        cmd = open(p, "rb").read().replace(b"\0", b" ").decode("utf-8", "replace")
    except OSError:
        continue
    for w in want:
        if w in cmd:
            alive[w] += 1
print("components alive:", alive)
sys.exit(0 if all(alive[w] for w in want) else 1)
PY
    then
        echo "components: all four running"
    else
        echo "FAIL: not all four components are running" >&2
        fail=1
    fi
fi

# Startup markers from run_all, the beacon and the control API.
for marker in "Started 4 components" "discovery probes" "HTTPS control API listening"; do
    if grep -q "$marker" "$LOG"; then
        echo "log marker OK: $marker"
    else
        echo "FAIL: missing log marker: $marker" >&2
        fail=1
    fi
done

# The detector must report the CPU backend + the downloaded model.
if grep -q "using detector backend 'cpu'" "$LOG" || grep -q "CPU detector initialized" "$LOG"; then
    echo "log marker OK: detector cpu backend"
else
    echo "FAIL: no detector-init log line (detector failed to load -- see logs)" >&2
    fail=1
fi

if [ "$fail" -eq 0 ]; then
    echo "PASS: built; all four components up; discovery + control API listening"
else
    echo "FAILED -- see logs above" >&2
fi
exit "$fail"
