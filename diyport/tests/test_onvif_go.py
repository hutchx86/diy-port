#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for AIPORT_ONVIF_MODE=go: the JSON-RPC client to the Go sidecar (fake socket server),
the per-camera configure/remove payloads, and run_all's supervisor."""
import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import onvif_push  # noqa: E402
import run_all  # noqa: E402

DEST = "tcp://192.0.2.1:7550?retryInterval=1&connectTimeout=5"
URL = "rtsp://admin:s3cret@192.0.2.42/101"


class FakeSidecar:
    """Accepts connections on a unix socket and records every request line."""

    def __init__(self, path):
        self.path, self.requests, self.conns = path, [], []
        self.srv = socket.socket(socket.AF_UNIX)
        self.srv.bind(path)
        self.srv.listen(4)
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while True:
            try:
                conn, _ = self.srv.accept()
            except OSError:
                return
            self.conns.append(conn)
            threading.Thread(target=self._read, args=(conn,), daemon=True).start()

    reply = None  # (conn, request) -> None; a test sets it to answer requests

    def _read(self, conn):
        for line in conn.makefile("rb"):
            req = json.loads(line)
            self.requests.append(req)
            if self.reply:
                self.reply(conn, req)

    def kill(self):
        """The sidecar dies: connections close, the next one starts empty."""
        self.srv.close()
        for c in self.conns:
            c.shutdown(socket.SHUT_RDWR)  # close() alone: the reader's makefile holds the fd
            c.close()
        self.requests.clear()
        os.unlink(self.path)


def wait(check, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end and not check():
        time.sleep(0.02)
    return check()


class TestGoMode(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "r.sock")

    def _manager(self, client):
        modes = onvif_push.parse_modes("go")
        p = mock.patch.object(onvif_push, "MODES", modes)
        p.start()
        self.addCleanup(p.stop)
        return onvif_push.PushManager(fetch_profiles=lambda url: [], background=False,
                                      restreamer=client)

    def _push(self, m):
        m.set_source("CAM", URL, audio=True)
        m.configure("CAM", {"video1": {"avSerializer": {
            "destinations": [DEST], "parameters": {"streamName": "S1"}}}})

    def test_parse_modes_accepts_go(self):
        self.assertEqual(onvif_push.parse_modes("copy,AA:BB=go"), ("copy", {"AABB": "go"}))
        self.assertEqual(onvif_push.parse_modes("go")[0], "go")

    def test_configure_and_remove_payloads(self):
        fake = FakeSidecar(self.path)
        client = onvif_push.RestreamerClient(self.path, retry_s=(0.05,))
        self.addCleanup(client.close)
        m = self._manager(client)
        with mock.patch.object(onvif_push, "SHARED_DECODE", True):
            with self.assertLogs("aiport-onvif", "INFO") as logs:
                self._push(m)
                self.assertTrue(wait(lambda: fake.requests))
        req = fake.requests[0]
        self.assertEqual((req["jsonrpc"], req["method"]), ("2.0", "configure_camera"))
        p = req["params"]
        self.assertEqual((p["device_id"], p["mode"]), ("CAM", "copy"))
        self.assertEqual(p["shared_decode"]["socket"], onvif_push.detect_socket(URL))
        self.assertGreater(p["channels"][0].pop("request"), 0)  # one per Protect send
        self.assertEqual(p["channels"], [{
            "channel": "video1", "stream_name": "S1", "rtsp_url": URL, "dest_host": "192.0.2.1",
            "dest_port": 7550, "audio_enabled": True, "width": 0, "height": 0,
            "bitrate_kbps": 0, "opus": False, "opus_sample_rate": 0}])
        self.assertNotIn("s3cret", "\n".join(logs.output))
        m.stop_all()
        self.assertTrue(wait(lambda: any(r["method"] == "remove_camera" for r in fake.requests)))
        self.assertEqual(fake.requests[-1]["params"], {"device_id": "CAM"})

    def _configure(self, m, **parameters):
        m.configure("CAM", {"video1": {"avSerializer": {
            "type": "extendedFlv", "destinations": [DEST],
            "parameters": dict(streamName="S1", **parameters)}}})

    def test_opus_ask_is_carried_and_a_change_resends(self):
        """Protect's avSerializer withOpus/opusSampleRate (live 2026-11-12: true, 24000) reach
        configure_camera as opus/opus_sample_rate, and a change re-sends it."""
        fake = FakeSidecar(self.path)
        client = onvif_push.RestreamerClient(self.path, retry_s=(0.05,))
        self.addCleanup(client.close)
        m = self._manager(client)
        m.set_source("CAM", URL, audio=True)
        def last_opus():
            sent = [r["params"]["channels"][0] for r in fake.requests
                    if r["method"] == "configure_camera" and r["params"]["channels"]]
            return (sent[-1]["opus"], sent[-1]["opus_sample_rate"]) if sent else None
        for params, want in (({"withOpus": True, "opusSampleRate": 24000}, (True, 24000)),
                             ({"withOpus": True, "opusSampleRate": 16000}, (True, 16000)),
                             ({"withOpus": True}, (True, 0)),  # rate not given
                             ({}, (False, 0))):  # no Opus asked
            self._configure(m, **params)
            self.assertTrue(wait(lambda: last_opus() == want), (params, last_opus()))

    def test_re_send_bumps_request_without_replacing_the_push(self):
        """Live 2026-11-19: after bbc6af1's reconnect-in-place, a re-send raised
        AttributeError (GoPush had no reconnect) and closed the avclient WS. A re-send must
        keep the push and re-send the camera with a new request, so the sidecar un-parks a
        push Protect had rejected."""
        fake = FakeSidecar(self.path)
        client = onvif_push.RestreamerClient(self.path, retry_s=(0.05,))
        self.addCleanup(client.close)
        m = self._manager(client)
        self._push(m)
        self.assertTrue(wait(lambda: fake.requests))
        push = m._pushes[("CAM", "video1")]
        first = fake.requests[-1]["params"]["channels"][0]["request"]
        self._push(m)                               # Protect re-sent the same settings
        self.assertIs(m._pushes[("CAM", "video1")], push)
        self.assertTrue(wait(lambda: fake.requests[-1]["params"]["channels"][0]["request"]
                             > first))
        self.assertEqual(fake.requests[-1]["method"], "configure_camera")

    def test_re_send_survives_a_failing_push(self):
        """One push failing its reconnect is logged, not raised into the WS handler."""
        client = mock.Mock()
        client.last_sent.return_value = None       # no frame from the sidecar yet
        m = self._manager(client)
        self._push(m)
        push = m._pushes[("CAM", "video1")]
        with mock.patch.object(push, "reconnect", side_effect=RuntimeError("boom")), \
                self.assertLogs("aiport-onvif", "ERROR") as logs:
            self._push(m)                           # must not raise
        self.assertIn("re-send reconnect failed", "\n".join(logs.output))
        self.assertIs(m._pushes[("CAM", "video1")], push)

    def test_gopush_matches_includes_opus(self):
        push = onvif_push.GoPush("CAM", "video1", URL, "S1", "tcp://192.0.2.1:7550", True, {},
                                 None, (True, 24000))
        args = (URL, "S1", "tcp://192.0.2.1:7550")
        self.assertTrue(push.matches(*args, None, opus=(True, 24000)))
        self.assertFalse(push.matches(*args, None, opus=(True, 48000)))
        self.assertFalse(push.matches(*args, None, opus=(False, 0)))

    def test_reconfigure_after_sidecar_restart(self):
        fake = FakeSidecar(self.path)
        client = onvif_push.RestreamerClient(self.path, retry_s=(0.05,))
        self.addCleanup(client.close)
        self._push(self._manager(client))
        self.assertTrue(wait(lambda: fake.requests))
        fake.kill()
        again = FakeSidecar(self.path)
        self.assertTrue(wait(lambda: again.requests))
        self.assertEqual(again.requests[0]["method"], "configure_camera")
        self.assertEqual(again.requests[0]["params"]["device_id"], "CAM")

    def test_last_sent_from_get_stats(self):
        """A go channel's last_sent follows the sidecar's last_frame_age_ms, so avclient's
        _check_restream sees its stalls; before a first frame it holds the push's start."""
        ages = {"video1": 400, "video2": None}

        def reply(conn, req):
            if req.get("method") == "get_stats":
                chans = [{"channel": c, "last_frame_age_ms": a} for c, a in ages.items()]
                res = {"cameras": {"AABBCCDDEEFF": {"mode": "copy", "channels": chans}}}
                conn.sendall(json.dumps({"jsonrpc": "2.0", "id": req["id"],
                                         "result": res}).encode() + b"\n")
        self.sidecar = FakeSidecar(self.path)
        self.addCleanup(self.sidecar.srv.close)
        self.sidecar.reply = reply
        client = onvif_push.RestreamerClient(self.path, retry_s=(0.05,), stats_s=0.05)
        self.addCleanup(client.close)
        group = {}
        pushes = [onvif_push.GoPush("AABBCCDDEEFF", ch, URL, ch, "tcp://192.0.2.1:7550",
                                    True, group, client) for ch in ("video1", "video2")]
        for p in pushes:
            p.start()
        self.assertTrue(wait(lambda: client.last_sent("AABBCCDDEEFF", "video1") is not None))
        now = time.monotonic()
        pushes[0].last_sent = now - 10  # an older floor: the sidecar's age decides
        self.assertAlmostEqual(pushes[0].last_sent, now - 0.4, delta=0.3)
        self.assertLess(pushes[1].last_sent, now - 0.0)  # no frame yet: the start time
        self.assertIsNone(client.last_sent("AABBCCDDEEFF", "video2"))
        ages["video1"] = 60000  # a minute without video: the floor (start) wins until ...
        pushes[0].last_sent = now - 120  # ... it is older, as when carried over a re-send
        self.assertTrue(wait(lambda: pushes[0].last_sent < time.monotonic() - 59))

    def test_go_needs_no_gstreamer(self):
        with mock.patch.object(onvif_push, "MODES", onvif_push.parse_modes("go")), \
                mock.patch.object(onvif_push, "transcode_available", return_value=False):
            self.assertEqual(onvif_push.mode_of("CAM"), "go")
            self.assertFalse(onvif_push.transcodes("CAM"))
            self.assertTrue(onvif_push.any_go())


class TestSupervisor(unittest.TestCase):
    def test_missing_binary_logs_one_error_and_does_not_start(self):
        sup = run_all.RestreamerSupervisor("/nonexistent/bin", "/tmp/x.sock")
        with self.assertLogs("diyport.run_all", "ERROR") as logs:
            self.assertFalse(sup.start())
        self.assertEqual(len(logs.output), 1)

    def test_restarts_on_exit_and_stops(self):
        runs = []

        def popen(cmd):
            runs.append(cmd)
            proc = mock.Mock()
            proc.wait.side_effect = lambda: time.sleep(0.01) or 1
            proc.poll.return_value = 0
            return proc
        sup = run_all.RestreamerSupervisor(sys.executable, "/tmp/diyport-test.sock", popen=popen)
        with mock.patch.object(run_all, "RESTART_MAX_S", 0.0), \
                mock.patch.object(run_all.threading.Event, "wait", lambda self, t=None: time.sleep(0.01)):
            self.assertTrue(sup.start())
            self.assertTrue(wait(lambda: len(runs) >= 2))
            sup.stop()
        self.assertEqual(runs[0], [sys.executable, "--sock", "/tmp/diyport-test.sock"])

    def test_audio_share_and_opus_rate_env_become_flags(self):
        runs = []

        def popen(cmd):
            runs.append(cmd)
            proc = mock.Mock()
            proc.wait.side_effect = lambda: time.sleep(0.01) or 1
            proc.poll.return_value = 0
            return proc
        env = {"AIPORT_ONVIF_AUDIO_SHARE": "off", "AIPORT_ONVIF_OPUS_RATE": "48000"}
        sup = run_all.RestreamerSupervisor(sys.executable, "/tmp/diyport-test.sock", popen=popen)
        with mock.patch.dict(os.environ, env), mock.patch.object(run_all, "RESTART_MAX_S", 0.0), \
                mock.patch.object(run_all.threading.Event, "wait", lambda self, t=None: time.sleep(0.01)):
            self.assertTrue(sup.start())
            self.assertTrue(wait(lambda: len(runs) >= 1))
            sup.stop()
        self.assertEqual(runs[0], [sys.executable, "--sock", "/tmp/diyport-test.sock",
                                   "--audio-share", "off", "--opus-rate", "48000"])


if __name__ == "__main__":
    unittest.main()
