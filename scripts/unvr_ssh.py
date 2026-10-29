#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 hutchx86
import os, sys, time, paramiko

HOST = os.environ.get("UNVR_HOST")
PORT = int(os.environ.get("UNVR_SSH_PORT", "22"))
USER = "root"
PASS = os.environ.get("UNVR_PASS")
if not HOST:
    sys.exit("UNVR_HOST is unset")
if not PASS:
    sys.exit("UNVR_PASS is unset")

def _handler(_title, _instructions, prompt_list):
    return [PASS for _ in prompt_list]

def run(cmd, timeout=60):
    t = paramiko.Transport((HOST, PORT))
    t.connect()
    t.auth_interactive(USER, _handler)
    chan = t.open_session()
    chan.settimeout(timeout)
    chan.exec_command(cmd)
    out, err = b"", b""
    while not chan.exit_status_ready():
        while chan.recv_ready():
            out += chan.recv(65536)
        while chan.recv_stderr_ready():
            err += chan.recv_stderr(65536)
        time.sleep(0.05)
    time.sleep(0.2)
    while chan.recv_ready():
        out += chan.recv(65536)
    while chan.recv_stderr_ready():
        err += chan.recv_stderr(65536)
    rc = chan.recv_exit_status()
    t.close()
    return rc, out.decode(errors="replace"), err.decode(errors="replace")

if __name__ == "__main__":
    rc, out, err = run(sys.argv[1])
    sys.stdout.write(out)
    sys.stderr.write(err)
    sys.exit(rc)
