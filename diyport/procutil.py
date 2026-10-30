# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 hutchx86
"""Small subprocess helpers shared by the video paths."""
import collections
import threading


def start_stderr_drain(proc, max_lines=200):
    """Continuously drain a child's stderr and return a tail() callable.

    Reading stderr only after proc.wait() deadlocks as soon as the child fills
    the OS pipe buffer (ffmpeg warnings do this on a flaky RTSP source).
    """
    tail = collections.deque(maxlen=max_lines)

    def _drain():
        stream = getattr(proc, "stderr", None)
        if stream is None:
            return
        try:
            for line in iter(stream.readline, b""):
                tail.append(line)
        except (OSError, ValueError):
            pass

    threading.Thread(target=_drain, daemon=True, name="stderr-drain").start()

    def _tail():
        return b"".join(tail).decode(errors="replace")

    return _tail
