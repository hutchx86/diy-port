# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 hutchx86
"""Unit tests for onvif_push.py: the extendedFlv rewrite and push bookkeeping.
(The rewrite was also checked byte-for-byte against unifi-cam-proxy's
clock_sync.py on a real ffmpeg FLV, before the trailer units were corrected.)"""
import io
import itertools
import os
import struct
import sys
import unittest
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

import onvif_push  # noqa: E402


def _flv(tags):
    """Plain FLV: header + PreviousTagSize0 + tags (type, timestamp, payload)."""
    out = b"FLV\x01\x01\x00\x00\x00\x09" + b"\x00\x00\x00\x00"
    for tag_type, ts, payload in tags:
        out += onvif_push._flv_tag(tag_type, payload, ts)
    return out


# AVC video tag payload: byte0 0x17 = key frame, FLV codec id 7 (H.264).
_AVC = b"\x17\x00\x00\x00\x00"


def _clock(step):
    c = itertools.count()
    return lambda: 1000.0 + next(c) * step


class TestRewrite(unittest.TestCase):
    def _run(self, src, step=0.01):
        out = io.BytesIO()
        onvif_push.rewrite_flv(io.BytesIO(src).read, out.write, clock=_clock(step))
        return out.getvalue()

    def test_header_flags_and_trailers(self):
        out = self._run(_flv([(9, 0, _AVC), (8, 40, b"aud")]))
        self.assertEqual(out[:9], b"FLV\x01\x07\x00\x00\x00\x09")
        tag1 = onvif_push._flv_tag(9, _AVC, 0)
        body = out[13:]
        self.assertEqual(body[:len(tag1)], tag1)
        trailer = body[len(tag1):len(tag1) + 16]
        self.assertEqual(len(trailer), 16)
        self.assertEqual(trailer[:4], b"\x00\x01\x5f\x90")  # video: 90000
        rest = body[len(tag1) + 16:]
        tag2 = onvif_push._flv_tag(8, b"aud", 40)
        self.assertEqual(rest[:len(tag2)], tag2)
        self.assertEqual(rest[len(tag2) + 1:len(tag2) + 4], b"\x00\x2b\x11")  # non-video
        self.assertEqual(len(rest), len(tag2) + 16)

    def test_trailer_elapsed_in_its_timescale(self):
        # Protect divides by the declared timescale: 1.5 s must read back as 1.5 s.
        video = onvif_push.timing_trailer(True, 1.5)
        self.assertEqual(len(video), 16)
        self.assertEqual(int.from_bytes(video[1:4], "big"), 90000)
        self.assertEqual(struct.unpack(">Q", video[8:])[0], 135000)
        other = onvif_push.timing_trailer(False, 1.5)
        self.assertEqual(int.from_bytes(other[1:4], "big"), 11025)
        self.assertEqual(struct.unpack(">Q", other[8:])[0], 16537)

    def test_trailer_no_wrap_after_13h(self):
        t = onvif_push.timing_trailer(True, 20 * 3600)
        self.assertEqual(struct.unpack(">Q", t[8:])[0], 20 * 3600 * 90000)

    def test_clock_sync_injected_every_5s(self):
        src = _flv([(9, i * 40, _AVC) for i in range(20)])
        out = self._run(src, step=1.0)  # every tag read advances the clock 1 s
        self.assertEqual(out.count(b"onClockSync"), out.count(b"onMpma"))
        self.assertGreaterEqual(out.count(b"onClockSync"), 3)
        # No sync before 5 s of stream: Protect drops a push whose onClockSync
        # precedes the first frames.
        self.assertNotIn(b"onClockSync", self._run(src, step=0.01))

    def test_script_tag_amf(self):
        tag = onvif_push.script_tag("onX", {"a": 1, "o": {"b": 2.5}}, timestamp=0x01020304)
        self.assertEqual(tag[0], 18)
        size = int.from_bytes(tag[1:4], "big")
        self.assertEqual(tag[4:8], b"\x02\x03\x04\x01")  # ui24 + extended byte
        payload = tag[11:11 + size]
        self.assertEqual(payload[:6], b"\x02\x00\x03onX")
        self.assertEqual(payload[6], 3)  # AMF0 object
        self.assertTrue(payload.endswith(b"\x00\x00\x09\x00\x00\x09"))  # nested + outer end
        self.assertEqual(tag[-4:], struct.pack(">I", size + 11))

    def test_rejects_non_flv(self):
        with self.assertRaises(ValueError):
            self._run(b"RIFF....")

    def test_rejects_non_h264_video(self):
        # ffmpeg muxes HEVC into FLV as Enhanced-RTMP (byte0 0x9 = key, codec 12,
        # 'hvc1' fourCC); ms never ingests that. Codec 8 (UniFi's own HEVC) is
        # equally unusable because it needs the 0x68 0x01 VPS/SPS/PPS config tag.
        for payload, codec in ((b"\x90hvc1\x01\x01\x60", "hevc (enhanced flv)"),
                               (b"\x18\x01\x00\x00\x00\x00", "FLV codec id 8")):
            with self.assertRaises(onvif_push.UnsupportedCodec) as ctx:
                self._run(_flv([(9, 0, payload)]))
            self.assertEqual(ctx.exception.codec, codec)
            self.assertIn("H.264", str(ctx.exception))

    def test_truncated_source_returns(self):
        src = _flv([(9, 0, _AVC + b"video-payload")])
        out = self._run(src[:-6])
        self.assertTrue(out.startswith(b"FLV"))


class TestDestination(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(onvif_push.parse_destination(
            "tcp://192.168.200.208:7550?retryInterval=1&connectTimeout=5"),
            ("192.168.200.208", 7550, 1.0, 5.0))

    def test_rejects_other(self):
        for bad in ("file:///dev/null", "tcp://host", "udp://h:1"):
            with self.assertRaises(ValueError):
                onvif_push.parse_destination(bad)

    def test_ffmpeg_cmd_copies_video_with_aac(self):
        cmd = onvif_push.ffmpeg_push_cmd("ffmpeg", "rtsp://cam/1", "ABC")
        self.assertEqual(cmd[cmd.index("-c:v") + 1], "copy")
        self.assertEqual(cmd[cmd.index("-c:a") + 1], "aac")
        self.assertIn("0:a:0", cmd)
        self.assertNotIn("lavfi", cmd)
        self.assertIn("streamName=ABC", cmd)
        self.assertEqual(cmd[-3:], ["-f", "flv", "-"])

    def test_ffmpeg_cmd_silent_track_without_camera_audio(self):
        cmd = onvif_push.ffmpeg_push_cmd("ffmpeg", "rtsp://cam/1", "ABC", camera_audio=False)
        self.assertIn("anullsrc=r=32000:cl=mono", cmd)
        self.assertIn("1:a:0", cmd)
        self.assertNotIn("0:a:0", cmd)


def _settings(channel, name, dest="tcp://10.0.0.1:7550?retryInterval=1&connectTimeout=5"):
    return {channel: {"avSerializer": {"type": "extendedFlv",
                                       "parameters": {"streamName": name},
                                       "destinations": [dest]}, "type": "h264"}}


class TestPushManager(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(onvif_push, "FlvPush")
        self.FlvPush = patcher.start()
        self.addCleanup(patcher.stop)
        self.started = []

        def make(device_id, channel, url, name, dest, ffmpeg_bin, camera_audio=True):
            push = mock.Mock()
            push.args = (device_id, channel, url, name, dest)
            push.matches.side_effect = lambda u, n, d: (u, n, d) == (url, name, dest)
            push.start.return_value = push
            self.started.append(push)
            return push

        self.FlvPush.side_effect = make
        self.m = onvif_push.PushManager()

    def test_waits_for_source(self):
        self.m.configure("CAM", _settings("video1", "S1"))
        self.assertEqual(self.started, [])
        self.m.set_source("CAM", "rtsp://cam/101")
        self.assertEqual([p.args[1:4] for p in self.started], [("video1", "rtsp://cam/101", "S1")])

    def test_idempotent_and_replaces_changed(self):
        self.m.set_source("CAM", "rtsp://cam/101")
        self.m.configure("CAM", _settings("video1", "S1"))
        self.m.configure("CAM", _settings("video1", "S1"))  # Protect re-sends every ~10 s
        self.assertEqual(len(self.started), 1)
        self.m.configure("CAM", _settings("video1", "S2"))
        self.assertEqual(len(self.started), 2)
        self.started[0].stop.assert_called_once()

    def test_channels_accumulate_and_dev_null_stops(self):
        self.m.set_source("CAM", "rtsp://cam/101")
        self.m.configure("CAM", _settings("video1", "S1"))
        self.m.configure("CAM", _settings("video2", "S2"))
        self.assertEqual(len(self.started), 2)
        self.m.configure("CAM", _settings("video2", "S2", dest="file:///dev/null"))
        self.started[1].stop.assert_called_once()
        self.started[0].stop.assert_not_called()

    def test_stop_device_keeps_destinations(self):
        # Protect's streaming:false is a stop, not an unassign: a later start must
        # resume the same streamName without a fresh ChangeVideoSettings.
        self.m.set_source("CAM", "rtsp://cam/101")
        self.m.configure("CAM", _settings("video1", "S1"))
        self.m.stop_device("CAM")
        self.started[0].stop.assert_called_once()
        self.assertFalse(self.m.has_source("CAM"))
        self.m.set_source("CAM", "rtsp://cam/101")
        self.assertEqual([p.args[1:4] for p in self.started],
                         [("video1", "rtsp://cam/101", "S1"),
                          ("video1", "rtsp://cam/101", "S1")])

    def test_clear_device_stops_all(self):
        self.m.set_source("CAM", "rtsp://cam/101")
        self.m.configure("CAM", {**_settings("video1", "S1"), **_settings("video3", "S3")})
        self.m.clear_device("CAM")
        for push in self.started:
            push.stop.assert_called_once()
        self.assertFalse(self.m.has_source("CAM"))
        self.assertIsNone(self.m.source("CAM"))

    def test_source_geometry_and_audio(self):
        self.m.set_source("CAM", "rtsp://cam/101", audio=False, width=3840, height=2160, fps=12)
        self.assertEqual(self.m.source("CAM"), {"url": "rtsp://cam/101", "audio": False,
                                                "width": 3840, "height": 2160, "fps": 12})



class TestRetryDelay(unittest.TestCase):
    def test_backs_off_only_while_pushes_send_nothing(self):
        delays = [onvif_push.retry_delay(1, n) for n in range(0, 10)]
        self.assertEqual(delays, [1, 1, 1, 1, 2, 4, 8, 16, 30, 30])
        self.assertEqual(onvif_push.retry_delay(0.2, 0), 1.0)   # never under a second

if __name__ == "__main__":
    unittest.main()
