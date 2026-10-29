#!/bin/bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Run pi-port as a launchd agent under your own account on macOS: started at login, restarted if
# it exits, logging to ~/piport.log with rotation (scripts/piport_service.sh does the work).
#
#   scripts/macos_launchd.sh install     write ~/Library/LaunchAgents/local.piport.plist, start it
#   scripts/macos_launchd.sh restart     restart pi-port (touch ~/.piport-restart also works)
#   scripts/macos_launchd.sh stop        stop it until the next start/install or login
#   scripts/macos_launchd.sh start       start it again
#   scripts/macos_launchd.sh status      launchd's view and the last log lines
#   scripts/macos_launchd.sh logs        follow ~/piport.log
#   scripts/macos_launchd.sh uninstall   stop it and remove the plist
#
# No sudo: macOS lets any user bind ports below 1024. Files an earlier `sudo` run left owned by
# root have to be handed back first; install says which and how. The host stats line's GPU and
# power figures need powermetrics as root: allow just that command with
#   echo "$USER ALL=(root) NOPASSWD: /usr/bin/powermetrics" | sudo tee /etc/sudoers.d/piport-powermetrics
#   sudo chmod 440 /etc/sudoers.d/piport-powermetrics && sudo visudo -cf /etc/sudoers.d/piport-powermetrics
set -u

LABEL=${PIPORT_LAUNCHD_LABEL:-local.piport}
REPO=$(cd "$(dirname "$0")/.." && pwd)
PLIST=$HOME/Library/LaunchAgents/$LABEL.plist
DOMAIN=gui/$(id -u)
ENV_FILE=${PIPORT_ENV_FILE:-$HOME/.piport.env}
LOG=${PIPORT_LOG:-$HOME/piport.log}
LAUNCHD_LOG=$HOME/Library/Logs/piport-launchd.log   # the wrapper's own errors only

die() { echo "macos_launchd: $*" >&2; exit 1; }

loaded() { launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; }

state_dir() {
    sed -n 's/^[[:space:]]*PIPORT_STATE_DIR=//p' "$ENV_FILE" 2>/dev/null | tail -1 \
        | sed 's/[[:space:]]*#.*//;s/^"//;s/"$//'
}

check_ownership() {
    # Everything pi-port writes must be yours: its state dir, the stream dir under /tmp, the
    # repo (compiled files, the Apple helper build) and the log.
    local paths=("$REPO" "$LOG") dir
    dir=$(state_dir)
    [ -n "$dir" ] && paths+=("$dir")
    [ -e /tmp/aiport-streams ] && paths+=("/private/tmp/aiport-streams")
    local bad=()
    for p in "${paths[@]}"; do
        [ -e "$p" ] || continue
        if [ -n "$(find "$p" ! -user "$(id -u)" -print -quit 2>/dev/null)" ]; then
            bad+=("$p")
        fi
    done
    if [ ${#bad[@]} -gt 0 ]; then
        echo "These still hold files owned by root from an earlier sudo run:" >&2
        printf '  %s\n' "${bad[@]}" >&2
        echo "Hand them back, then run install again:" >&2
        echo "  sudo chown -R $(id -un):staff$(printf ' "%s"' "${bad[@]}")" >&2
        exit 1
    fi
}

check_not_running() {
    local pids
    pids=$(pgrep -f "$REPO/piport/run_all.py" || true)
    if [ -n "$pids" ] && ! loaded; then
        echo "pi-port is already running outside launchd (pid $(echo $pids | tr ' ' ','))." >&2
        echo "Stop it first (Ctrl-C in its terminal, or: sudo pkill -INT -f piport/run_all.py)." >&2
        exit 1
    fi
}

write_plist() {
    mkdir -p "$(dirname "$PLIST")" "$(dirname "$LAUNCHD_LOG")"
    cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>$REPO/scripts/piport_service.sh</string>
    </array>
    <key>WorkingDirectory</key><string>$REPO</string>
    <key>EnvironmentVariables</key>
    <dict>
        <!-- ffmpeg and ffprobe come from Homebrew; launchd's own PATH doesn't have it. -->
        <key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    </dict>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>ThrottleInterval</key><integer>10</integer>
    <!-- The wrapper gives pi-port 10 s to stop cleanly before TERM. -->
    <key>ExitTimeOut</key><integer>30</integer>
    <!-- Not throttled as a background job: it analyses video in real time. -->
    <key>ProcessType</key><string>Interactive</string>
    <key>StandardOutPath</key><string>$LAUNCHD_LOG</string>
    <key>StandardErrorPath</key><string>$LAUNCHD_LOG</string>
</dict>
</plist>
EOF
    plutil -lint "$PLIST" >/dev/null || die "wrote an invalid $PLIST"
}

start() {
    loaded || launchctl bootstrap "$DOMAIN" "$PLIST" || die "launchctl bootstrap failed"
    launchctl enable "$DOMAIN/$LABEL"
}

status() {
    if loaded; then
        launchctl print "$DOMAIN/$LABEL" | grep -E "^\s*(state|pid|last exit code|runs) =" \
            | sed 's/^[[:space:]]*/  /'
    else
        echo "  not loaded"
    fi
    echo "last log lines ($LOG):"
    tail -n 5 "$LOG" 2>/dev/null | cut -c1-200 | sed 's/^/  /'
}

[ "$(uname)" = Darwin ] || die "this is for macOS"
[ "$(id -u)" -ne 0 ] || die "run it as yourself, not with sudo: the agent runs under your account"

case "${1:-}" in
    install)
        [ -r "$ENV_FILE" ] || die "no $ENV_FILE (copy scripts/piport.env.example)"
        [ -x "$REPO/venv/bin/python" ] || die "no venv at $REPO/venv"
        check_ownership
        check_not_running
        loaded && launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null
        write_plist
        start
        echo "installed $PLIST; pi-port starts now and at every login"
        sleep 3
        status
        ;;
    uninstall)
        loaded && launchctl bootout "$DOMAIN/$LABEL"
        rm -f "$PLIST"
        echo "removed $LABEL"
        ;;
    start)
        [ -f "$PLIST" ] || die "not installed (run: $0 install)"
        start
        status
        ;;
    stop)
        loaded && launchctl bootout "$DOMAIN/$LABEL"
        echo "stopped $LABEL (starts again at login, or with: $0 start)"
        ;;
    restart)
        loaded || die "not running (run: $0 start)"
        launchctl kickstart -k "$DOMAIN/$LABEL"
        status
        ;;
    status) status ;;
    logs) exec tail -n 50 -F "$LOG" ;;
    *)
        sed -n '3,13p' "$0" | sed 's/^# \{0,1\}//'
        exit 2
        ;;
esac
