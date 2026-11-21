#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 hutchx86
"""Runs the discovery + adopt-HTTP + ucp4-client + classic-avclient stages
together. Needs root (binds :10001, :443). Settings come from aiport.cfg;
per-instance overrides are passed through to every stage.

Usage:
  sudo python3 run_all.py --iface eth0
"""
import argparse
import logging
import os
import signal
import subprocess
import sys
import threading
import time

import config
import envutil
import onvif_push

HERE = os.path.dirname(os.path.abspath(__file__))
log = logging.getLogger("diyport.run_all")

RESTREAMER_BIN = os.path.join(os.path.dirname(HERE), "go-restreamer", "bin", "diyport-restreamer")
RESTART_MAX_S = 30.0
RESTART_HEALTHY_S = 60.0  # a run this long starts the backoff again


class RestreamerSupervisor:
    """Runs the Go sidecar for AIPORT_ONVIF_MODE=go (or DIYPORT_RESTREAMER=1) and restarts it on
    exit, backing off 1, 2, 4 .. RESTART_MAX_S. It doesn't know the cameras: avclient's
    RestreamerClient re-sends every configure_camera when it reconnects to the new socket. A
    missing binary is one error and go cameras stay unpushed; the other stages are unaffected."""

    def __init__(self, binary, sock, popen=subprocess.Popen, clock=time.monotonic):
        self.binary, self.sock, self._popen, self._clock = binary, sock, popen, clock
        self._stop = threading.Event()
        self._proc = None
        self._thread = None

    def start(self):
        if not os.path.isfile(self.binary) or not os.access(self.binary, os.X_OK):
            log.error("go restreamer binary %s not found or not executable (build go-restreamer or "
                      "set DIYPORT_RESTREAMER_BIN): go-mode cameras will not be pushed", self.binary)
            return False
        self._thread = threading.Thread(target=self._run, daemon=True, name="restreamer-super")
        self._thread.start()
        return True

    def _run(self):
        delay = 1.0
        while not self._stop.is_set():
            try:
                os.unlink(self.sock)  # a dead sidecar's socket would fail the bind
            except OSError:
                pass
            began = self._clock()
            try:
                # AIPORT_ONVIF_G711=copy|aac: the sidecar's G.711 handling (default aac).
                # AIPORT_ONVIF_OPUS=on|off: the sidecar's browser-live-view Opus track (default on).
                # AIPORT_ONVIF_AUDIO_SHARE=on|off: one audio ffmpeg per camera URL (default on).
                # AIPORT_ONVIF_OPUS_RATE: force the Opus sample rate for every channel (default: Protect's).
                # AIPORT_ONVIF_PACE_MS: the sidecar's send buffer (default 1000; 0 = off).
                g711 = os.environ.get("AIPORT_ONVIF_G711")
                opus = os.environ.get("AIPORT_ONVIF_OPUS")
                share = os.environ.get("AIPORT_ONVIF_AUDIO_SHARE")
                opus_rate = os.environ.get("AIPORT_ONVIF_OPUS_RATE")
                pace = os.environ.get("AIPORT_ONVIF_PACE_MS")
                self._proc = self._popen([self.binary, "--sock", self.sock]
                                         + (["--g711", g711] if g711 else [])
                                         + (["--opus", opus] if opus else [])
                                         + (["--audio-share", share] if share else [])
                                         + (["--opus-rate", opus_rate] if opus_rate else [])
                                         + (["--pace-ms", pace] if pace else []))
                rc = self._proc.wait()
            except OSError as e:
                log.error("go restreamer failed to start: %s", e)
                rc = None
            if self._stop.is_set():
                return
            delay = 1.0 if self._clock() - began >= RESTART_HEALTHY_S else min(delay * 2, RESTART_MAX_S)
            log.warning("go restreamer exited rc=%s: restarting in %.0f s", rc, delay)
            self._stop.wait(delay)

    def stop(self):
        self._stop.set()
        proc = self._proc
        if proc and proc.poll() is None:
            proc.terminate()  # SIGTERM closes everything cleanly
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


def _stop_all(procs):
    """Terminate the whole stage group, then kill anything that ignored TERM."""
    for p in procs:
        if p.poll() is None:
            p.terminate()
    deadline = time.monotonic() + 5
    for p in procs:
        try:
            p.wait(timeout=max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            p.kill()


def main():
    cfg = config.load_config_and_logging(sys.argv[1:])
    ap = argparse.ArgumentParser()
    config.add_common_flags(ap)
    ap.add_argument("--iface", default=None,
                     help="interface to bind/derive identity from; defaults to cfg [network] iface, "
                          "else the default-route interface (auto-detected)")
    ap.add_argument("--bind-ip", default="0.0.0.0")
    ap.add_argument("--mac", default=None,
                     help="defaults to a real Ubiquiti OUI (FC:EC:DA) + the interface's own "
                          "last 3 octets, not the interface's real (non-Ubiquiti) MAC")
    ap.add_argument("--hostname", default=cfg["hostname"])
    ap.add_argument("--fallback-host-port", default=None)
    ap.add_argument("--state-dir", default=None,
                     help="this instance's adopt_state.json/certs/streams; defaults to "
                          "$XDG_STATE_HOME/diyport or ~/.local/state/diyport, and is required "
                          "to run more than one instance (see instance_manager.py)")
    ap.add_argument("--device-id", default=cfg["device_id"],
                     help="AI Port's own device identity (shared by ucp4_client.py and "
                          "avclient.py -- 'same physical device'); defaults to the "
                          "cfg value, which MUST be unique per instance when running more "
                          "than one")
    args = ap.parse_args()

    if not args.state_dir:
        args.state_dir = config.default_state_dir()
        moved = config.migrate_repo_state(args.state_dir)
        if moved:
            print(f"Migrated legacy repo state to {args.state_dir}: {', '.join(moved)}")
    os.makedirs(args.state_dir, mode=0o700, exist_ok=True)

    # macOS lets any user bind ports below 1024 (since 10.14), so it runs under the user's own
    # account there (scripts/macos_launchd.sh); elsewhere :443 needs root.
    if os.geteuid() != 0 and sys.platform != "darwin":
        sys.exit("run_all.py needs root (binds ports 10001, 443)")

    args.iface = config.resolve_iface(args.iface, cfg)
    print(f"Using interface {args.iface}")

    if args.mac is None:
        args.mac = config.resolve_mac(args.iface, None, cfg)
        print(f"No --mac given; using MAC {args.mac}")

    # Bind the interface's own IP so other instances' :443 stays free (0.0.0.0 if it fails).
    # macOS without root is the exception: it can only bind :443 on the wildcard address.
    if args.bind_ip == "0.0.0.0" and sys.platform == "darwin" and os.geteuid() != 0:
        print(f"Binding 0.0.0.0: macOS lets a non-root user bind :443 only on the wildcard "
              f"address (it still reports {args.iface}'s own IP)")
    elif args.bind_ip == "0.0.0.0":
        resolved = config.resolve_ip(args.iface, cfg)
        if resolved and resolved != "0.0.0.0":
            args.bind_ip = resolved
            print(f"No --bind-ip given; using {args.iface}'s own IP {resolved}")
        else:
            print(f"WARNING: could not resolve {args.iface}'s IP, binding 0.0.0.0 -- "
                  f"this instance will collide with any other instance's :443/:10001")

    # Per-stage flags that must reach every child process.
    common = []
    if args.config:
        common += ["--config", args.config]
    if args.debug:
        common += ["--debug"]

    discovery_args = [sys.executable, os.path.join(HERE, "discovery.py"),
                   "--iface", args.iface, "--bind-ip", args.bind_ip, "--hostname", args.hostname,
                   *common]
    http_args = [sys.executable, os.path.join(HERE, "http_api.py"),
                 "--iface", args.iface, "--bind-ip", args.bind_ip, "--hostname", args.hostname,
                 *common]
    wss_args = [sys.executable, os.path.join(HERE, "ucp4_client.py"),
                "--iface", args.iface, "--bind-ip", args.bind_ip, *common]
    if args.fallback_host_port:
        wss_args += ["--fallback-host-port", args.fallback_host_port]

    classic_args = [sys.executable, os.path.join(HERE, "avclient.py"),
                    "--iface", args.iface, "--bind-ip", args.bind_ip, "--hostname", args.hostname,
                    *common]

    if args.mac:
        for a in (discovery_args, http_args, wss_args, classic_args):
            a += ["--mac", args.mac]

    for a in (discovery_args, http_args, wss_args, classic_args):
        a += ["--state-dir", args.state_dir]
    if args.device_id:
        # All stages must share one device-id, else multi-instance discover/info breaks.
        http_args += ["--device-id", args.device_id]
        wss_args += ["--device-id", args.device_id]
        classic_args += ["--device-id", args.device_id]
        discovery_args += ["--device-id", args.device_id]

    procs = [
        subprocess.Popen(discovery_args),
        subprocess.Popen(http_args),
        subprocess.Popen(wss_args),
        subprocess.Popen(classic_args),
    ]

    restreamer = None
    if onvif_push.any_go() or envutil.env_bool("DIYPORT_RESTREAMER", False):
        restreamer = RestreamerSupervisor(
            os.environ.get("DIYPORT_RESTREAMER_BIN") or RESTREAMER_BIN,
            onvif_push.restreamer_sock(args.state_dir))
        restreamer.start()

    print(f"Started {len(procs)} components. Ctrl-C to stop all.")

    def _on_term(*_):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _on_term)
    signal.signal(signal.SIGINT, _on_term)
    try:
        while True:
            for p in procs:
                rc = p.poll()
                if rc is not None:
                    print(f"Component {p.pid} exited rc={rc}; stopping the rest")
                    _stop_all(procs)
                    restreamer and restreamer.stop()
                    return 1
            time.sleep(1)
    except KeyboardInterrupt:
        _stop_all(procs)
        restreamer and restreamer.stop()
        return 0


if __name__ == "__main__":
    sys.exit(main())
