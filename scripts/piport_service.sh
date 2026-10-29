#!/bin/bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Run pi-port on macOS and restart it without a new terminal or password.
#
#   scripts/macos_launchd.sh install     as a launchd agent under your account (recommended)
#   cd ~/Code/pi-port && scripts/piport_service.sh     in a terminal
#
# No sudo: macOS lets any user bind ports below 1024. (Run with sudo it still works, as before.)
#
# Settings come from ~/.piport.env (see scripts/piport.env.example): AIPORT_* settings and
# PIPORT_* identity, one KEY=value per line, re-read on every restart. Only those keys are
# taken; the file is never run as a script.
#
#   touch ~/.piport-restart     restart pi-port (new code, new settings), no sudo needed
#   Ctrl-C in this terminal     stop it
#
# If pi-port exits on its own it is restarted after 5 s. Output goes to ~/piport.log, rotated
# by size (PIPORT_LOG_MAX_MB, 50 by default; PIPORT_LOG_KEEP old files, 5), and to the
# terminal when there is one. caffeinate keeps the Mac awake while this runs.
set -u

if [ "$(uname)" != Darwin ] && [ "$(id -u)" -ne 0 ] && [ -z "${PIPORT_SERVICE_TEST:-}" ]; then
    echo "run with sudo: it binds port 443 on the interface's address" >&2
    exit 1
fi
USER_HOME=$(eval echo "~${SUDO_USER:-${USER:-$(id -un)}}")
REPO=${PIPORT_SERVICE_TEST:-$(cd "$(dirname "$0")/.." && pwd)}  # the test points it at a stub
ENV_FILE=${PIPORT_ENV_FILE:-$USER_HOME/.piport.env}
TRIGGER=${PIPORT_RESTART_FILE:-$USER_HOME/.piport-restart}
LOG=${PIPORT_LOG:-$USER_HOME/piport.log}
# pi-port's process group, so a wrapper killed outright (yesterday's app restart) doesn't leave
# an orphan holding :443 that the next start can't get past.
PIDFILE=${PIPORT_PIDFILE:-$USER_HOME/.piport-service.pid}

load_settings() {
    # Clear the previous run's settings so a removed line really goes away.
    for k in $(env | grep -E '^(AIPORT|PIPORT)_[A-Z0-9_]*=' | cut -d= -f1); do
        [ "$k" = PIPORT_ENV_FILE ] || [ "$k" = PIPORT_RESTART_FILE ] || [ "$k" = PIPORT_LOG ] \
            || unset "$k"
    done
    if [ ! -r "$ENV_FILE" ]; then
        echo "piport_service: no $ENV_FILE (copy scripts/piport.env.example)" >&2
        return 1
    fi
    while IFS= read -r line || [ -n "$line" ]; do
        line=${line%%#*}
        line=$(echo "$line" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
        [ -z "$line" ] && continue
        key=${line%%=*}
        value=${line#*=}
        value=${value#\"}; value=${value%\"}
        if [[ "$key" =~ ^(AIPORT|PIPORT)_[A-Z0-9_]+$ && "$line" == *=* ]]; then
            export "$key=$value"
        else
            echo "piport_service: ignoring line in $ENV_FILE: $line" >&2
        fi
    done < "$ENV_FILE"
    local missing=""
    for k in PIPORT_IFACE PIPORT_MAC PIPORT_DEVICE_ID PIPORT_STATE_DIR; do
        [ -n "${!k:-}" ] || missing="$missing $k"
    done
    if [ -n "$missing" ]; then
        echo "piport_service: $ENV_FILE is missing$missing" >&2
        return 1
    fi
}

mtime() { stat -f %m "$1" 2>/dev/null || echo 0; }

child=""
stop_child() {
    if [ -n "$child" ] && kill -0 "$child" 2>/dev/null; then
        kill -INT -- "-$child" 2>/dev/null || kill -INT "$child"   # its whole process group
        kill -CONT -- "-$child" 2>/dev/null   # a stopped process can't act on the signal
        for _ in $(seq 1 20); do kill -0 "$child" 2>/dev/null || break; sleep 0.5; done
        kill -TERM -- "-$child" 2>/dev/null
        kill -CONT -- "-$child" 2>/dev/null
        wait "$child" 2>/dev/null
    fi
    child=""
}
stop_stale() {
    # A pi-port left by an earlier wrapper that was killed outright: it has its own process
    # group, so it outlives the wrapper and keeps :443.
    local old
    old=$(cat "$PIDFILE" 2>/dev/null) || return 0
    if [ -n "$old" ] && ps -p "$old" -o command= 2>/dev/null | grep -q "run_all.py"; then
        echo "piport_service: stopping pi-port (pid $old) left running by an earlier start"
        child=$old
        stop_child
    fi
    rm -f "$PIDFILE"
}
trap 'echo "piport_service: stopping"; stop_child; rm -f "$PIDFILE"; exit 0' INT TERM

# One writer rotates the log; it ignores Ctrl-C/TERM so the stop is still logged. Its sizes are
# read once, here (a change needs a full restart, not ~/.piport-restart).
env_number() {
    local v
    v=$(sed -n "s/^[[:space:]]*$1=//p" "$ENV_FILE" 2>/dev/null | tail -1 | sed 's/[[:space:]#].*//')
    [[ "$v" =~ ^[0-9]+(\.[0-9]+)?$ ]] && echo "$v" || echo "$2"
}
exec > >("$REPO/venv/bin/python" "$(dirname "$0")/log_writer.py" "$LOG" \
             --max-mb "${PIPORT_LOG_MAX_MB:-$(env_number PIPORT_LOG_MAX_MB 50)}" \
             --keep "${PIPORT_LOG_KEEP:-$(env_number PIPORT_LOG_KEEP 5)}") 2>&1
[ -n "${PIPORT_SERVICE_TEST:-}" ] || caffeinate -is -w $$ &

while true; do
    if ! load_settings; then
        sleep 5
        continue
    fi
    stop_stale
    seen=$(mtime "$TRIGGER")
    echo "piport_service: starting pi-port at $(date '+%F %T') (settings from $ENV_FILE)"
    # A background job may start with SIGINT ignored, and Python then never raises
    # KeyboardInterrupt; restore the default before exec so run_all can shut down cleanly.
    # Job control only for this launch: pi-port gets its own process group (stop_child signals
    # all of it), while the loop's sleeps and $(...) don't each get one, which raced and
    # printed "child setpgid (...): Operation not permitted".
    set -m
    "$REPO/venv/bin/python" -c 'import os, signal, sys
signal.signal(signal.SIGINT, signal.SIG_DFL)
os.execv(sys.argv[1], sys.argv[1:])' "$REPO/venv/bin/python" "$REPO/piport/run_all.py" \
        --iface "$PIPORT_IFACE" --state-dir "$PIPORT_STATE_DIR" --mac "$PIPORT_MAC" \
        --device-id "$PIPORT_DEVICE_ID" < /dev/null &   # a background job reading the
    # terminal is stopped (SIGTTIN): ffmpeg and ffprobe read stdin
    child=$!
    echo "$child" > "$PIDFILE"
    set +m
    while kill -0 "$child" 2>/dev/null; do
        sleep 1
        if [ "$(mtime "$TRIGGER")" != "$seen" ]; then
            echo "piport_service: $TRIGGER touched, restarting"
            stop_child
            break
        fi
    done
    if [ -n "$child" ]; then
        wait "$child"
        echo "piport_service: pi-port exited ($?), restarting in 5 s"
        child=""
        sleep 5
    fi
done
