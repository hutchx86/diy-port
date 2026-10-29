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
import os
import signal
import subprocess
import sys
import time

import config

HERE = os.path.dirname(os.path.abspath(__file__))


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
                     help="isolate this instance's adopt_state.json/certs/streams under a "
                          "dedicated directory instead of the shared default paths -- "
                          "required when running more than one AI Port instance on the same "
                          "box (see instance_manager.py)")
    ap.add_argument("--device-id", default=cfg["device_id"],
                     help="AI Port's own device identity (shared by ucp4_client.py and "
                          "avclient.py -- 'same physical device'); defaults to the "
                          "cfg value, which MUST be unique per instance when running more "
                          "than one")
    args = ap.parse_args()

    # macOS lets any user bind ports below 1024 (since 10.14), so it runs under the user's own
    # account there (scripts/macos_launchd.sh); elsewhere :443 needs root.
    if os.geteuid() != 0 and sys.platform != "darwin":
        sys.exit("run_all.py needs root (binds ports 10001, 443)")

    args.iface = config.resolve_iface(args.iface, cfg)
    print(f"Using interface {args.iface}")

    if args.mac is None:
        args.mac = config.resolve_mac(args.iface, None, cfg)
        print(f"No --mac given; using MAC {args.mac}")

    # Bind the interface's own IP, not 0.0.0.0: a wildcard :443 would block
    # other instances' :443. Falls back to 0.0.0.0 if resolution fails. Not on macOS without
    # root: there a user may bind ports below 1024 only on the wildcard address (binding
    # en0's IP to :443 was refused), and one Mac runs one instance. Components still report
    # the interface's own IP.
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

    if args.state_dir:
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

    print(f"Started {len(procs)} components. Ctrl-C to stop all.")

    def _on_term(signum, frame):
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
                    return 1
            time.sleep(1)
    except KeyboardInterrupt:
        _stop_all(procs)
        return 0


if __name__ == "__main__":
    sys.exit(main())
