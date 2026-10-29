# SPDX-License-Identifier: AGPL-3.0-or-later
"""The camera's own clock, from the RTCP sender reports on its RTSP stream.

A native UniFi camera's sender reports carry its uptime clock (seen: 339676 s on Camera A,
843376 s on Camera C, steady to 2 ms), the clock its own EventSmartDetect `clockMonotonic`
uses. A Reader holds its own RTSP session to the relay, reads the reports and keeps
`offset_ms`: local wall-clock ms minus camera ms when a report arrives. A frame stamped at
local wall time W was captured at about camera time W - offset_ms (an experiment: the
frame's decode time is not taken out).
"""
import logging
import re
import socket
import statistics
import struct
import threading
import time

log = logging.getLogger("aiport-camera-clock")

NTP_UNIX_OFFSET = 2208988800
OFFSETS_KEPT = 12  # one report every ~5 s: the last minute


def parse_sender_reports(packet):
    """[(ntp seconds, rtp timestamp)] for each sender report (PT 200) in an RTCP packet."""
    out, off = [], 0
    while off + 20 <= len(packet):
        pt = packet[off + 1]
        length = (struct.unpack(">H", packet[off + 2:off + 4])[0] + 1) * 4
        if pt == 200:
            sec, frac, rtp = struct.unpack(">III", packet[off + 8:off + 20])
            out.append((sec - NTP_UNIX_OFFSET + frac / 2 ** 32, rtp))
        off += max(length, 4)
    return out


class Reader:
    """Reads one camera's sender reports on a background thread until stop()."""

    def __init__(self, device_id, url):
        self.device_id, self.url = device_id, url
        self.offsets = []          # recent local wall ms - camera ms
        self.camera_s = None       # camera clock at the last report
        self._stop = threading.Event()
        self._sock = None
        self._thread = threading.Thread(target=self._run, name=f"camclock-{device_id}",
                                        daemon=True)

    def start(self):
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        sock = self._sock
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

    def offset_ms(self):
        """Median recent (local wall ms - camera ms), or None before the first report."""
        return statistics.median(self.offsets) if self.offsets else None

    def add_report(self, ntp_s, received_s):
        self.camera_s = ntp_s
        self.offsets = (self.offsets + [(received_s - ntp_s) * 1000.0])[-OFFSETS_KEPT:]
        if len(self.offsets) == 1:
            log.info("camera clock for deviceID=%s: camera at %.3f s, offset %.0f ms",
                     self.device_id, ntp_s, self.offsets[0])

    def _run(self):
        while not self._stop.is_set():
            try:
                self._session()
            except Exception as exc:  # reconnect after any failure
                if not self._stop.is_set():
                    log.warning("camera clock for deviceID=%s: %s; retrying in 5 s",
                                self.device_id, exc)
            self._stop.wait(5)

    def _session(self):
        host, port = re.match(r"rtsp://([^:/]+):(\d+)/", self.url).groups()
        sock = self._sock = socket.create_connection((host, int(port)), timeout=15)
        rtsp = _Rtsp(sock)
        head, sdp = rtsp.request("DESCRIBE", self.url, "Accept: application/sdp\r\n")
        base = (re.search(r"Content-Base:\s*(\S+)", head, re.I) or [None, self.url + "/"])[1]
        video = sdp.split("m=video", 1)[1].split("m=", 1)[0]
        control = re.search(r"a=control:(\S+)", video).group(1)
        track = control if control.startswith("rtsp://") else base.rstrip("/") + "/" + control
        head, _ = rtsp.request("SETUP", track, "Transport: RTP/AVP/TCP;unicast;interleaved=0-1\r\n")
        session = re.search(r"Session:\s*([^;\r\n]+)", head, re.I).group(1)
        rtsp.request("PLAY", self.url, f"Session: {session}\r\nRange: npt=0.000-\r\n")
        try:
            while not self._stop.is_set():
                channel, packet = rtsp.interleaved()
                if channel == 1:
                    for ntp_s, _rtp in parse_sender_reports(packet):
                        self.add_report(ntp_s, time.time())
        finally:
            try:
                sock.sendall(f"TEARDOWN {self.url} RTSP/1.0\r\nCSeq: 99\r\nSession: {session}"
                             f"\r\n\r\n".encode())
            except OSError:
                pass
            sock.close()


class _Rtsp:
    def __init__(self, sock):
        self.sock, self.buf, self.cseq = sock, b"", 0

    def _fill(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("stream closed")
            self.buf += chunk

    def request(self, method, target, extra=""):
        self.cseq += 1
        self.sock.sendall(f"{method} {target} RTSP/1.0\r\nCSeq: {self.cseq}\r\n{extra}\r\n".encode())
        while b"\r\n\r\n" not in self.buf:
            self._fill(len(self.buf) + 1)
        head, self.buf = self.buf.split(b"\r\n\r\n", 1)
        text = head.decode(errors="replace")
        if " 200 " not in text.split("\r\n", 1)[0]:
            raise ConnectionError(f"{method} failed: {text.splitlines()[0]}")
        m = re.search(r"Content-Length:\s*(\d+)", text, re.I)
        body = b""
        if m:
            self._fill(int(m.group(1)))
            body, self.buf = self.buf[:int(m.group(1))], self.buf[int(m.group(1)):]
        return text, body.decode(errors="replace")

    def interleaved(self):
        """(channel, payload) of the next interleaved packet."""
        while True:
            self._fill(4)
            if self.buf[:1] == b"$":
                break
            i = self.buf.find(b"$", 1)
            self.buf = self.buf[i:] if i >= 0 else b""
        n = struct.unpack(">H", self.buf[2:4])[0]
        self._fill(4 + n)
        channel, packet, self.buf = self.buf[1], self.buf[4:4 + n], self.buf[4 + n:]
        return channel, packet
