#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Copy stdin to a log file, rotating it by size: scripts/piport_service.sh's log, in place of
tee. One writer for every pi-port process, so rotation is safe.

    log_writer.py LOG [--max-mb 50] [--keep 5]

LOG becomes LOG.1 once it reaches --max-mb (LOG.1 becomes LOG.2, and so on; LOG.<keep> is
dropped). Lines are also echoed to stdout when that's a terminal, as tee did; under launchd it
isn't, so they go to LOG only. Ctrl-C and SIGTERM are ignored so the service's own stop
messages still get logged; it exits when every writer has closed the pipe.
"""
import argparse
import os
import signal
import sys


def rotate(path, keep):
    """LOG -> LOG.1 -> ... -> LOG.<keep>, dropping the oldest."""
    for i in range(keep - 1, 0, -1):
        src = f"{path}.{i}"
        if os.path.exists(src):
            os.replace(src, f"{path}.{i + 1}")
    if keep > 0 and os.path.exists(path):
        os.replace(path, f"{path}.1")
    elif os.path.exists(path):
        os.remove(path)


class RotatingLog:
    def __init__(self, path, max_bytes, keep):
        self.path, self.max_bytes, self.keep = path, max_bytes, keep
        self.f = None
        self.open()

    def open(self):
        if os.path.exists(self.path) and os.path.getsize(self.path) >= self.max_bytes:
            rotate(self.path, self.keep)
        self.f = open(self.path, "ab")
        self.size = self.f.tell()

    def write(self, line):
        self.f.write(line)
        self.f.flush()
        self.size += len(line)
        if self.size >= self.max_bytes:
            self.f.close()
            rotate(self.path, self.keep)
            self.f = open(self.path, "ab")
            self.size = 0

    def close(self):
        if self.f:
            self.f.close()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("log")
    ap.add_argument("--max-mb", type=float, default=50, help="rotate at this size (default 50)")
    ap.add_argument("--keep", type=int, default=5, help="rotated files kept (default 5)")
    args = ap.parse_args(argv)
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, signal.SIG_IGN)
    echo = sys.stdout.buffer if sys.stdout.isatty() else None
    log = RotatingLog(args.log, int(args.max_mb * 1024 * 1024), max(0, args.keep))
    try:
        for line in sys.stdin.buffer:
            log.write(line)
            if echo:
                echo.write(line)
                echo.flush()
    finally:
        log.close()


if __name__ == "__main__":
    main()
