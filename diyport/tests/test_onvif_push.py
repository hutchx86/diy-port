# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 hutchx86
"""Unit tests for onvif_push.py: the extendedFlv rewrite and push bookkeeping.
(The rewrite was also checked byte-for-byte against unifi-cam-proxy's
clock_sync.py on a real ffmpeg FLV, before the trailer units were corrected.)"""
import io
import itertools
import os
import socket
import struct
import sys
import threading
import time
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



def _modes(default, **per_camera):
    """AIPORT_ONVIF_MODE, on a machine that can transcode."""
    return mock.patch.multiple(onvif_push, MODES=(default, per_camera),
                               transcode_available=lambda: True)


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
        # ffmpeg muxes HEVC as Enhanced-RTMP (codec 12), which ms never ingests; codec 8
        # needs the VPS/SPS/PPS config tag.
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


class TestStreamURL(unittest.TestCase):
    def test_accepts_rtsp_and_rtsps(self):
        for url in ("rtsp://cam/1", "rtsps://user:p%40ss@cam:8554/a?x=1"):
            self.assertEqual(onvif_push.validate_stream_url(url), url)

    def test_rejects_non_rtsp_and_shell_metacharacters(self):
        for bad in ("file:///etc/passwd", "concat:/etc/passwd", "http://cam/1",
                    "rtsp://cam/a b", "rtsp://cam/a!filesink", 'rtsp://cam/a"b',
                    "rtsp://cam/a\\b", "rtsp:///x", "rtsp://cam:99999/x",
                    "rtsp://cam/x#fragment"):
            with self.assertRaises(ValueError, msg=bad):
                onvif_push.validate_stream_url(bad)

    def test_ffmpeg_gets_a_protocol_whitelist(self):
        cmd = onvif_push.ffmpeg_push_cmd("ffmpeg", "rtsp://cam/1", "S")
        at = cmd.index("-protocol_whitelist")
        self.assertEqual(cmd[at + 1], "rtsp,tcp,udp,rtp,tls")
        self.assertLess(at, cmd.index("-i"))

    def test_transcode_rejects_a_non_rtsp_source(self):
        with self.assertRaises(ValueError):
            onvif_push.transcode_cmd("gst", "file:///etc/passwd", [])


class TestDestination(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(onvif_push.parse_destination(
            "tcp://192.0.2.208:7550?retryInterval=1&connectTimeout=5"),
            ("192.0.2.208", 7550, 1.0, 5.0))

    def test_rejects_other(self):
        for bad in ("file:///dev/null", "tcp://host", "udp://h:1"):
            with self.assertRaises(ValueError):
                onvif_push.parse_destination(bad)

    def test_ffmpeg_cmd_copies_video_and_audio(self):
        # Re-encoding the camera's G.711 to AAC made live view stop and start every second.
        cmd = onvif_push.ffmpeg_push_cmd("ffmpeg", "rtsp://cam/1", "ABC")
        self.assertEqual(cmd[cmd.index("-c:v") + 1], "copy")
        self.assertEqual(cmd[cmd.index("-c:a") + 1], "copy")
        self.assertIn("0:a:0", cmd)
        reencoded = onvif_push.ffmpeg_push_cmd("ffmpeg", "rtsp://cam/1", "ABC", copy_audio=False)
        self.assertEqual(reencoded[reencoded.index("-c:a") + 1], "aac")
        self.assertNotIn("lavfi", cmd)
        self.assertIn("streamName=ABC", cmd)
        self.assertEqual(cmd[-3:], ["-f", "flv", "-"])

    def test_ffmpeg_cmd_wallclock_or_camera_timestamps(self):
        cmd = onvif_push.ffmpeg_push_cmd("ffmpeg", "rtsp://cam/1", "ABC")
        self.assertEqual(cmd[cmd.index("-use_wallclock_as_timestamps") + 1], "1")
        self.assertLess(cmd.index("-use_wallclock_as_timestamps"), cmd.index("-i"))
        camera = onvif_push.ffmpeg_push_cmd("ffmpeg", "rtsp://cam/1", "ABC", wallclock=False)
        self.assertNotIn("-use_wallclock_as_timestamps", camera)
        self.assertEqual(camera[camera.index("-i") + 1], "rtsp://cam/1")
        i = cmd.index("-use_wallclock_as_timestamps")
        self.assertEqual(cmd[:i] + cmd[i + 2:], camera)  # nothing else differs

    def test_use_camera_timestamps_setting(self):
        use = onvif_push.use_camera_timestamps
        self.assertFalse(use({"AABBCCDDEEFF"}, setting=False))
        self.assertTrue(use({"AABBCCDDEEFF"}, setting=True))
        self.assertTrue(use(set(), setting=True))
        listed = onvif_push.parse_camera_list("aa:bb:cc:dd:ee:ff, 112233445566")
        self.assertTrue(use({"AABBCCDDEEFF"}, setting=listed))
        self.assertFalse(use({"AABBCC000002"}, setting=listed))
        self.assertFalse(use(set(), setting=listed))

    def test_shared_pull_command_follows_its_cameras(self):
        class Sink:
            def __init__(self, device_id, channel):
                self.device_id, self.channel = device_id, channel
        pull = onvif_push.SharedPull("rtsp://cam/101")
        pull._sinks[Sink("AABBCC000002", "video1")] = True
        with mock.patch.object(onvif_push, "CAMERA_TIMESTAMPS", False):
            cmd, camera_ts = pull._command()
        self.assertFalse(camera_ts)
        self.assertIn("-use_wallclock_as_timestamps", cmd)
        with mock.patch.object(onvif_push, "CAMERA_TIMESTAMPS", {"AABBCC000002"}):
            cmd, camera_ts = pull._command()
        self.assertTrue(camera_ts)
        self.assertNotIn("-use_wallclock_as_timestamps", cmd)

    def test_pool_pull_knows_its_camera_before_any_push_subscribes(self):
        # PullPool starts a SharedPull before the FlvPush subscribes and the pull builds its
        # first ffmpeg command at once, so the camera must already be known then.
        pool = onvif_push.PullPool()
        with mock.patch.object(onvif_push.SharedPull, "start", lambda self: self), \
                mock.patch.object(onvif_push, "CAMERA_TIMESTAMPS", {"AABBCC000002"}):
            pull = pool.acquire("rtsp://cam/101", False, "video1", device_id="AABBCC000002")
            self.assertEqual(pull._sinks, {})
            cmd, camera_ts = pull._command()
            self.assertTrue(camera_ts)
            self.assertNotIn("-use_wallclock_as_timestamps", cmd)
            other = pool.acquire("rtsp://cam/202", False, "video1", device_id="AABBCCDDEEFF")
            self.assertFalse(other._command()[1])
            self.assertIs(pool.acquire("rtsp://cam/101", False, "video3",
                                       device_id="AABBCC000002"), pull)

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

        def make(device_id, channel, url, name, dest, ffmpeg_bin, camera_audio=True, pool=None,
                 renditions=None):
            push = mock.Mock()
            push.args = (device_id, channel, url, name, dest)
            push.renditions = renditions
            push.matches.side_effect = lambda u, n, d, r=None, a=None: (
                (u, n, d, r) == (url, name, dest, renditions)
                and (a is None or bool(a) == bool(camera_audio)))
            push.start.return_value = push
            self.started.append(push)
            return push

        self.FlvPush.side_effect = make
        self.profiles = []      # what the camera's ONVIF Media service lists
        self.lookups = []

        def fetch(url):
            self.lookups.append(url)
            if isinstance(self.profiles, Exception):
                raise self.profiles
            return self.profiles

        self.m = onvif_push.PushManager(fetch_profiles=fetch, background=False)

    def test_waits_for_source(self):
        self.m.configure("CAM", _settings("video1", "S1"))
        self.assertEqual(self.started, [])
        self.m.set_source("CAM", "rtsp://cam/101")
        self.assertEqual([p.args[1:4] for p in self.started], [("video1", "rtsp://cam/101", "S1")])

    def test_repeat_reconnects_at_most_every_10_s_and_change_replaces(self):
        # Protect re-sends a channel's settings when it lost the stream and waits for a
        # fresh connection (live: video3 "Timed out waiting for ..._2 to open" every 30 s).
        clock = [100.0]
        with mock.patch.object(onvif_push.time, "monotonic", lambda: clock[0]):
            self.m.set_source("CAM", "rtsp://cam/101")
            self.m.configure("CAM", _settings("video1", "S1"))
            self.m.configure("CAM", _settings("video1", "S1"))   # the repeat: reconnect
            self.assertEqual(len(self.started), 1)
            self.started[0].stop.assert_not_called()
            self.started[0].restart_source.assert_called_once()
            self.started[0].reconnect.assert_called_once()
            clock[0] += 5
            self.m.configure("CAM", _settings("video1", "S1"))   # within 10 s: kept
            self.assertEqual(len(self.started), 1)
            self.assertEqual(self.started[0].reconnect.call_count, 1)
            clock[0] += 5
            self.m.configure("CAM", _settings("video1", "S1"))   # 10 s on: reconnect
            self.assertEqual(len(self.started), 1)
            self.assertEqual(self.started[0].reconnect.call_count, 2)
            self.m.configure("CAM", _settings("video1", "S2"))   # a new name replaces it
            self.assertEqual(len(self.started), 2)
            self.started[0].stop.assert_called_once()

    def test_h265_camera_falls_back_to_copy(self):
        self.addCleanup(onvif_push._codecs.clear)
        self.addCleanup(onvif_push._codec_fell_back.clear)
        with _modes("transcode"):
            onvif_push.note_codec("CAM", "hevc")
            self.assertFalse(onvif_push.transcodes("CAM"))
            self.m.set_source("CAM", "rtsp://cam/101", width=2560, height=1440, fps=20,
                              codec="hevc")
            self.m.configure("CAM", _settings("video1", "S1"))
        self.assertIsNone(self.started[0].renditions)

    def test_h264_camera_stays_on_transcode(self):
        self.addCleanup(onvif_push._codecs.clear)
        with _modes("transcode"):
            onvif_push.note_codec("CAM", "h264")
            self.assertTrue(onvif_push.transcodes("CAM"))

    def test_full_re_send_reconnects_without_replacing_pushes(self):
        settings = {**_settings("video1", "S1"), **_settings("video2", "S2"),
                    **_settings("video3", "S3")}
        self.m.set_source("CAM", "rtsp://cam/101")
        self.m.configure("CAM", settings)
        first = list(self.started)
        self.m.configure("CAM", settings)          # Protect re-sent every channel
        self.assertEqual(self.started, first)      # no stop/recreate, so no pull teardown
        for push in first:
            push.stop.assert_not_called()
            push.reconnect.assert_called_once()

    def test_channels_accumulate_and_dev_null_stops(self):
        self.m.set_source("CAM", "rtsp://cam/101")
        self.m.configure("CAM", _settings("video1", "S1"))
        self.m.configure("CAM", _settings("video2", "S2"))
        self.assertEqual(len(self.started), 2)
        self.m.configure("CAM", _settings("video2", "S2", dest="file:///dev/null"))
        self.started[1].stop.assert_called_once()
        self.started[0].stop.assert_not_called()

    def test_encoder_only_settings_keep_pushes(self):
        # Live 2026-10-28: Protect sends fps/bitrate settings for every channel without an
        # avSerializer right after assigning a camera; that stopped the running pushes.
        self.m.set_source("CAM", "rtsp://cam/101")
        self.m.configure("CAM", _settings("video1", "S1"))
        self.m.configure("CAM", {"video1": {"dynamicFpsMode": 2, "enabled": True, "fps": 20},
                                 "video2": {"enabled": True, "fps": 20}})
        self.started[0].stop.assert_not_called()
        self.assertEqual(len(self.started), 1)

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

    def test_source_geometry_and_audio(self):
        self.m.set_source("CAM", "rtsp://cam/101", audio=False, width=3840, height=2160, fps=12)
        self.assertEqual(self.m.source("CAM"), {"url": "rtsp://cam/101", "audio": False,
                                                "width": 3840, "height": 2160, "fps": 12,
                                                "codec": None})

    def _two_profiles(self):
        self.profiles = [
            {"token": "P1", "encoding": "H264", "width": 2560, "height": 1440, "fps": 20,
             "url": "rtsp://cam/101"},
            {"token": "P2", "encoding": "H264", "width": 640, "height": 360, "fps": 20,
             "url": "rtsp://cam/102"},
        ]

    def _pushed(self):
        return {p.args[1]: p.args[2] for p in self.started if not p.stop.called}

    def test_video2_and_video3_carry_substream(self):
        self._two_profiles()
        self.m.configure("CAM", {**_settings("video1", "S1"), **_settings("video2", "S2"),
                                 **_settings("video3", "S3")})
        self.m.set_source("CAM", "rtsp://cam/101")
        self.assertEqual(self._pushed(), {"video1": "rtsp://cam/101", "video2": "rtsp://cam/102",
                                          "video3": "rtsp://cam/102"})
        self.assertEqual(self.m.substream("CAM"), {"width": 640, "height": 360, "fps": 20})

    def test_transcode_pulls_main_once_and_encodes_substream_size(self):
        # AIPORT_ONVIF_MODE=transcode (option C, as the AI Port): every channel comes from one
        # pull of the main stream, video2/video3 encoded at the size Protect is told.
        self._two_profiles()
        with _modes("transcode"):
            self.m.configure("CAM", {**_settings("video1", "S1"), **_settings("video2", "S2"),
                                     **_settings("video3", "S3")})
            self.m.set_source("CAM", "rtsp://cam/101", width=2560, height=1440, fps=20)
        self.assertEqual(set(self._pushed().values()), {"rtsp://cam/101"})
        renditions = {p.renditions for p in self.started}
        self.assertEqual(len(renditions), 1)  # one TranscodePull for the camera
        self.assertEqual(renditions.pop(), (("video1", 2560, 1440, 20, 4000, 20),
                                            ("video2", 640, 360, 20, 2000, 24),
                                            ("video3", 640, 360, 20, 500, 24)))

    def test_transcode_video3_is_360_lines_whatever_the_substream(self):
        self._two_profiles()
        self.profiles[1].update(width=1280, height=720)
        with _modes("transcode"):
            self.m.configure("CAM", {**_settings("video1", "S1"), **_settings("video3", "S3")})
            self.m.set_source("CAM", "rtsp://cam/101", width=2560, height=1440, fps=20)
            self.assertEqual(self.m.transcode_channels("CAM")["video3"],
                             {"width": 640, "height": 360, "fps": 20, "bitrate": 500000})
        self.assertEqual(self.started[0].renditions, (("video1", 2560, 1440, 20, 4000, 20),
                                                      ("video2", 1280, 720, 20, 2000, 24),
                                                      ("video3", 640, 360, 20, 500, 24)))
        self.assertIsNone(self.m.transcode_channels("CAM"))  # only under TRANSCODE

    def test_transcode_channels_leave_out_what_is_unknown(self):
        with _modes("transcode"):
            self.assertIsNone(self.m.transcode_channels("CAM"))  # no source yet
            self.m.set_source("CAM", "rtsp://cam/101")
            self.assertEqual(self.m.transcode_channels("CAM")["video2"], {"bitrate": 2000000})

    def test_stalled_channels_are_the_pushes_with_nothing_sent(self):
        self.m.set_source("CAM", "rtsp://cam/101")
        self.m.configure("CAM", {**_settings("video1", "S1"), **_settings("video3", "S3")})
        self.started[0].last_sent, self.started[1].last_sent = 100.0, 120.0
        self.assertEqual(self.m.stalled_channels(30, now=125.0), {})
        self.assertEqual(self.m.stalled_channels(30, now=130.0), {"CAM": ["video1"]})
        self.assertEqual(self.m.stalled_channels(30, now=150.0), {"CAM": ["video1", "video3"]})

    def test_a_re_send_does_not_make_a_silent_channel_look_healthy(self):
        # Live 2026-11-03 17:24: Protect re-sent every ~13 s, which must not reset a silent push.
        self.m.set_source("CAM", "rtsp://cam/101")
        self.m.configure("CAM", _settings("video1", "S1"))
        self.started[0].last_sent = 50.0
        self.m.configure("CAM", _settings("video1", "S1"))
        self.assertEqual(self.started[0].last_sent, 50.0)  # same push, no false health
        self.assertEqual(self.m._last_sent, {("CAM", "video1"): 50.0})

    def test_stopping_forgets_what_a_re_send_kept(self):
        self.m._last_sent[("CAM", "video1")] = 50.0
        self.m.stop_device("CAM")
        self.assertEqual(self.m._last_sent, {})

    def _failing_then(self, *results):
        """fetch_profiles raising or returning `results` in turn."""
        results = iter(results)

        def fetch(url):
            result = next(results)
            if isinstance(result, Exception):
                raise result
            return result
        self.m._fetch_profiles = fetch

    def test_quick_retry_leaves_a_brief_failure_with_no_effect(self):
        # Live 2026-11-04 19:23:39: "No route to host", reachable 2 s later.
        self._two_profiles()
        good, self.profiles = self.profiles, []
        self._failing_then(OSError("no route"), good)
        self.m._background = True
        self.m._looking_up.add("rtsp://cam/101")
        with mock.patch.object(onvif_push.time, "sleep") as sleep, \
                mock.patch.object(onvif_push.threading, "Timer") as timer:
            self.m._find_substream("rtsp://cam/101")
        sleep.assert_called_once_with(onvif_push.PROFILE_QUICK_RETRY_S[0])
        timer.assert_not_called()
        self.assertEqual(self.m._subs["rtsp://cam/101"]["url"], "rtsp://cam/102")

    def test_failed_lookup_keeps_trying_with_longer_waits(self):
        self.m._background = True
        self.m._sources["CAM"] = {"url": "rtsp://cam/101", "audio": True, "width": None,
                                  "height": None, "fps": None}
        self._failing_then(*[OSError("no route")] * 20)
        waits = []
        with mock.patch.object(onvif_push.time, "sleep"), \
                mock.patch.object(onvif_push.threading, "Timer") as timer:
            self.m._find_substream("rtsp://cam/101")
            for late in range(1, 5):
                waits.append(timer.call_args.args[0])
                self.assertEqual(timer.call_args.kwargs["args"], ("rtsp://cam/101", late))
                self.m._find_substream("rtsp://cam/101", late)
        self.assertEqual(waits, [30.0, 120.0, 600.0, 600.0])
        self.assertTrue(timer.return_value.daemon)

    def test_a_late_lookup_moves_the_channels_to_the_substream(self):
        self.profiles = OSError("no route")
        self.m.configure("CAM", {**_settings("video1", "S1"), **_settings("video2", "S2")})
        self.m.set_source("CAM", "rtsp://cam/101")
        self.assertEqual(self._pushed(), {"video1": "rtsp://cam/101", "video2": "rtsp://cam/101"})
        self._two_profiles()
        self.m._find_substream("rtsp://cam/101", late=1)
        self.assertEqual(self._pushed(), {"video1": "rtsp://cam/101", "video2": "rtsp://cam/102"})

    def test_a_camera_without_a_substream_is_not_looked_up_again(self):
        self.m._background = True
        self.m._sources["CAM"] = {"url": "rtsp://cam/101", "audio": True, "width": None,
                                  "height": None, "fps": None}
        self.profiles = []
        with mock.patch.object(onvif_push.threading, "Timer") as timer:
            self.m._find_substream("rtsp://cam/101")
        timer.assert_not_called()

    def test_retries_stop_once_nothing_uses_the_camera(self):
        self.m._background = True
        self._failing_then(*[OSError("no route")] * 3)
        with mock.patch.object(onvif_push.time, "sleep"), \
                mock.patch.object(onvif_push.threading, "Timer") as timer:
            self.m._find_substream("rtsp://cam/101", late=1)
        timer.assert_not_called()
        self.assertNotIn("rtsp://cam/101", self.m._subs)   # the next set_source looks it up

    def test_wait_for_substream(self):
        self.m._sources["CAM"] = {"url": "rtsp://cam/101"}
        self.m._looking_up.add("rtsp://cam/101")
        started = time.monotonic()
        self.m.wait_for_substream("CAM", timeout=0.05)     # gives up at the timeout
        self.assertGreaterEqual(time.monotonic() - started, 0.04)

        def finish():
            time.sleep(0.05)
            self.m._find_substream("rtsp://cam/101")
        threading.Thread(target=finish, daemon=True).start()
        started = time.monotonic()
        self.m.wait_for_substream("CAM", timeout=5)
        self.assertLess(time.monotonic() - started, 4)
        self.m.wait_for_substream("UNKNOWN", timeout=5)    # no such camera: no wait
        self.assertLess(time.monotonic() - started, 4)

    def test_transcode_is_per_camera(self):
        self._two_profiles()
        settings = {**_settings("video1", "S1"), **_settings("video3", "S3")}
        with _modes("copy", CAM="transcode"):
            for device in ("CAM", "OTHER"):
                self.m.configure(device, settings)
                self.m.set_source(device, f"rtsp://{device}/101", width=2560, height=1440, fps=20)
            self.assertEqual(self.m.transcode_channels("CAM")["video1"],
                             {"width": 2560, "height": 1440, "fps": 20, "bitrate": 4000000})
            self.assertIsNone(self.m.transcode_channels("OTHER"))
        pushes = {p.args[:2]: p for p in self.started if not p.stop.called}
        self.assertIsNotNone(pushes["CAM", "video1"].renditions)
        self.assertIsNone(pushes["OTHER", "video1"].renditions)

    def test_modes_setting(self):
        parse = onvif_push.parse_modes
        self.assertEqual(parse(None), ("copy", {}))
        self.assertEqual(parse(""), ("copy", {}))
        self.assertEqual(parse("isolated"), ("isolated", {}))
        self.assertEqual(parse("ISOLATED, aa:bb:cc:dd:ee:ff=copy,AABBCC000002=transcode"),
                         ("isolated", {"AABBCCDDEEFF": "copy", "AABBCC000002": "transcode"}))
        with self.assertLogs("aiport-onvif", "WARNING"):
            self.assertEqual(parse("isolated,CAM=fast,1"), ("isolated", {}))

    def test_mode_is_per_camera(self):
        with _modes("isolated", OTHER="copy", THIRD="transcode"):
            self.assertEqual(onvif_push.mode_of("CAM"), "isolated")
            self.assertEqual(onvif_push.mode_of("OTHER"), "copy")
            self.assertEqual(onvif_push.mode_of("THIRD"), "transcode")
            self.assertTrue(onvif_push.transcodes("CAM"))
            self.assertTrue(onvif_push.transcodes("THIRD"))
            self.assertFalse(onvif_push.transcodes("OTHER"))

    def test_camera_falls_back_to_copy_where_it_cannot_transcode(self):
        # Before this, such a camera had no video at all, and retried forever.
        onvif_push._fell_back.discard("NOGST")
        with mock.patch.multiple(onvif_push, MODES=("isolated", {}),
                                 transcode_available=lambda: False):
            with self.assertLogs("aiport-onvif", "ERROR") as logs:
                self.assertEqual(onvif_push.mode_of("NOGST"), "copy")
            self.assertIn("using copy", logs.output[0])
            self.assertFalse(onvif_push.transcodes("NOGST"))
            with self.assertNoLogs("aiport-onvif", "ERROR"):     # said once
                onvif_push.mode_of("NOGST")

    def test_transcode_needs_macos_and_gstreamer(self):
        with mock.patch.object(onvif_push.os.path, "exists", return_value=True):
            with mock.patch.object(onvif_push.sys, "platform", "darwin"):
                self.assertTrue(onvif_push.transcode_available())
            with mock.patch.object(onvif_push.sys, "platform", "linux"):
                self.assertFalse(onvif_push.transcode_available())
        with mock.patch.object(onvif_push.sys, "platform", "darwin"), \
                mock.patch.object(onvif_push.os.path, "exists", return_value=False):
            self.assertFalse(onvif_push.transcode_available())

    def test_old_settings_are_called_out(self):
        with self.assertLogs("aiport-onvif", "WARNING") as logs:
            onvif_push.warn_old_settings({"AIPORT_ONVIF_TRANSCODE": "1",
                                          "AIPORT_ONVIF_SUBPROCESS": "CAM"})
        self.assertEqual(len(logs.output), 2)
        with self.assertNoLogs("aiport-onvif", "WARNING"):
            onvif_push.warn_old_settings({"AIPORT_ONVIF_TRANSCODE": ""})

    def test_camera_list_setting(self):
        parse = onvif_push.parse_camera_list
        self.assertFalse(parse(""))
        self.assertFalse(parse("0"))
        self.assertIs(parse("1"), True)
        self.assertEqual(parse("aa:bb:cc:dd:ee:ff, CAM"), {"AABBCCDDEEFF", "CAM"})

    def test_transcode_video3_keeps_the_camera_aspect(self):
        self.assertEqual(onvif_push.transcode_video3_size(1920, 1080), (640, 360))
        self.assertEqual(onvif_push.transcode_video3_size(1280, 960), (480, 360))
        self.assertIsNone(onvif_push.transcode_video3_size(None, None))

    def test_video3_waits_for_profiles(self):
        # No push on the main stream first: a camera allows few RTSP sessions, and a
        # restart onto the substream right after would overlap the two.
        self._two_profiles()
        self.m.configure("CAM", {**_settings("video1", "S1"), **_settings("video3", "S3")})
        seen = []
        fetch = self.m._fetch_profiles
        self.m._fetch_profiles = lambda url: (seen.append(dict(self._pushed())), fetch(url))[1]
        self.m.set_source("CAM", "rtsp://cam/101")
        self.assertEqual(seen, [{"video1": "rtsp://cam/101"}])
        self.assertEqual(len(self.started), 2)

    def test_lookup_once_per_url(self):
        self._two_profiles()
        self.m.configure("CAM", _settings("video3", "S3"))
        self.m.set_source("CAM", "rtsp://cam/101")
        self.m.stop_device("CAM")
        self.m.set_source("CAM", "rtsp://cam/101")
        self.assertEqual(self.lookups, ["rtsp://cam/101"])
        self.assertEqual([p.args[2] for p in self.started], ["rtsp://cam/102"] * 2)

    def test_no_substream_or_failed_lookup_uses_main(self):
        for profiles in ([], OSError("timed out")):
            with self.subTest(profiles=profiles):
                self.setUp()
                self.profiles = profiles
                self.m.configure("CAM", _settings("video3", "S3"))
                self.m.set_source("CAM", "rtsp://cam/101")
                self.assertEqual(self._pushed(), {"video3": "rtsp://cam/101"})
                self.assertIsNone(self.m.substream("CAM"))



class TestRetryDelay(unittest.TestCase):
    def test_backs_off_only_while_pushes_send_nothing(self):
        delays = [onvif_push.retry_delay(1, n) for n in range(0, 10)]
        self.assertEqual(delays, [1, 1, 1, 1, 2, 4, 8, 16, 30, 30])
        self.assertEqual(onvif_push.retry_delay(0.2, 0), 1.0)   # never under a second

def _meta(stream_name="diyport"):
    """ffmpeg's onMetaData: an ECMA array with a number and the streamName."""
    amf = onvif_push._amf_string
    pairs = (amf("duration") + b"\x00" + struct.pack(">d", 0)
             + amf("streamName") + b"\x02" + amf(stream_name))
    return b"\x02" + amf("onMetaData") + b"\x08" + struct.pack(">I", 2) + pairs + b"\x00\x00\x09"


_AVC_CONFIG = b"\x17\x00\x00\x00\x00\x01\x64"
_AAC_CONFIG = b"\xaf\x00\x12\x10"
_KEY = b"\x17\x01\x00\x00\x00key"
_INTER = b"\x27\x01\x00\x00\x00inter"
_AAC = b"\xaf\x01aac"

# A camera stream as the shared pull's ffmpeg writes it, starting mid-stream (t=1000 ms).
_SOURCE = _flv([(18, 0, _meta()), (9, 0, _AVC_CONFIG), (8, 0, _AAC_CONFIG),
                (9, 1000, _KEY), (8, 1010, _AAC), (9, 1040, _INTER), (9, 2000, _KEY)])


def _parse_extended(data):
    """[(tag type, timestamp, payload)] from extendedFlv (each tag + 16-byte trailer)."""
    tags, pos = [], 13
    while pos + 11 <= len(data):
        size = int.from_bytes(data[pos + 1:pos + 4], "big")
        ts = onvif_push._tag_timestamp(data[pos:pos + 11])
        tags.append((data[pos], ts, data[pos + 11:pos + 11 + size]))
        pos += 11 + size + 4 + 16
    return tags


class TestEarlySync(unittest.TestCase):
    def _write(self, tags, early_sync):
        out = io.BytesIO()
        writer = onvif_push.ExtendedFlvWriter(out.write, _clock(0.01), early_sync=early_sync)
        writer.header(_flv([])[:13])
        for tag_type, ts, payload in tags:
            tag = onvif_push._flv_tag(tag_type, payload, ts)
            writer.sync_if_due(ts)
            writer.tag(tag[:11], tag[11:])
        return [t[2] for t in _parse_extended(out.getvalue())]

    def test_first_sync_right_after_the_first_frame(self):
        # Never ahead of the frames (Protect closes that push), but not 5 s late either:
        # ms drops a recording file that closes before its first sync.
        payloads = self._write([(9, 0, _AVC_CONFIG), (8, 0, _AAC_CONFIG), (9, 0, _KEY),
                                (8, 10, _AAC), (9, 50, _INTER), (9, 100, _INTER)], True)
        syncs = [i for i, p in enumerate(payloads) if b"onClockSync" in p]
        self.assertEqual(syncs, [3])
        self.assertEqual(payloads[2], _KEY)

    def test_rewrite_keeps_waiting_5_s(self):
        payloads = self._write([(9, 0, _KEY), (9, 50, _INTER)], False)
        self.assertFalse(any(b"onClockSync" in p for p in payloads))


class TestG3Sync(unittest.TestCase):
    """Option C's clock syncs: yi-protect's G3 placement."""

    def test_sync_at_start_and_before_every_key_frame(self):
        out = io.BytesIO()
        writer = onvif_push.ExtendedFlvWriter(out.write, _clock(0.01), g3_sync=True)
        writer.header(_flv([])[:13])
        meta = onvif_push._flv_tag(18, _meta(), 0)
        writer.sync_if_due(0)
        writer.tag(meta[:11], meta[11:])
        writer.initial_sync()
        for tag_type, ts, payload in [(9, 0, _AVC_CONFIG), (9, 0, _KEY), (9, 50, _INTER),
                                      (9, 1000, _KEY)]:
            tag = onvif_push._flv_tag(tag_type, payload, ts)
            # Codec configs reach FlvPush apart from the frames, never as a key frame.
            writer.sync_if_due(ts, payload is not _AVC_CONFIG
                               and onvif_push._is_keyframe(tag[:11], payload))
            writer.tag(tag[:11], tag[11:])
        names = [b"onMpma" if b"onMpma" in p else b"onClockSync" if b"onClockSync" in p
                 else p for p in (t[2] for t in _parse_extended(out.getvalue()))]
        self.assertEqual(names, [_meta(), b"onMpma", b"onClockSync", _AVC_CONFIG,
                                 b"onClockSync", b"onMpma", _KEY, _INTER,
                                 b"onClockSync", b"onMpma", _KEY])


class TestProtectClock(unittest.TestCase):
    def test_new_stream_uses_the_local_clock_until_a_write(self):
        clock = onvif_push.ProtectClock()
        started = time.monotonic()
        self.assertIsNone(clock.wall(1000, 1.0))       # no per-frame timeout on startup
        self.assertLess(time.monotonic() - started, 0.2)
        clock.synced(0, 1000)
        clock.wrote(2000)
        self.assertEqual(clock.wall(1000, 0.1), 2000)  # sync at 0 = wall 1000

    def test_a_silent_writer_latches_the_fallback(self):
        clock = onvif_push.ProtectClock()
        clock.synced(0, 1000)
        clock.wrote(1000)
        with clock._cond:
            clock._last_write = time.monotonic() - 2.0
        started = time.monotonic()
        self.assertIsNone(clock.wall(2000, 1.0))
        self.assertLess(time.monotonic() - started, 0.2)

    def test_reset_wakes_a_waiting_frame(self):
        clock = onvif_push.ProtectClock()
        clock.synced(0, 1000)
        clock.wrote(0)                                 # clear the startup latch
        result = []
        thread = threading.Thread(target=lambda: result.append(clock.wall(1000, 2.0)))
        thread.start()
        time.sleep(0.05)
        clock.reset()
        thread.join(0.5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result, [None])


class TestDetectServer(unittest.TestCase):
    def test_close_ends_the_client_and_the_accept_thread(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "detect.sock")
            pull = mock.Mock(source_url="rtsp://cam/1")
            server = onvif_push.DetectServer(path, pull)
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.addCleanup(client.close)
            client.connect(path)
            client.settimeout(2.0)
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline and not any(
                    t.name == "detect-client" and t.is_alive() for t in threading.enumerate()):
                time.sleep(0.01)
            server.close()
            self.assertFalse(os.path.exists(path))
            self.assertEqual(client.recv(16), b"")  # server closed the reader's connection
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline and any(
                    t.name == "detect-accept" and t.is_alive() for t in threading.enumerate()):
                time.sleep(0.01)
            self.assertFalse(any(t.name == "detect-accept" and t.is_alive()
                                 for t in threading.enumerate()))


class TestFullReSendPull(unittest.TestCase):
    class FakeTranscode:
        made = []

        def __init__(self, url, audio, renditions):
            self.source_url, self.camera_audio, self.renditions = url, audio, renditions
            self.outputs = {ch: mock.Mock() for ch in onvif_push.TRANSCODE_CHANNELS}
            for out in self.outputs.values():
                out.transcode = self
            self.stopped = 0
            self.restarted = 0
            self.made.append(self)

        def start(self):
            return self

        def stop(self):
            self.stopped += 1

        def restart(self, reason):
            self.restarted += 1

    def test_full_re_send_keeps_one_transcode_pull(self):
        self.FakeTranscode.made.clear()
        settings = {**_settings("video1", "S1"), **_settings("video2", "S2"),
                    **_settings("video3", "S3")}
        with _modes("transcode"), \
                mock.patch.object(onvif_push, "TranscodePull", self.FakeTranscode):
            m = onvif_push.PushManager(fetch_profiles=lambda url: [], background=False)
            m.set_source("CAM", "rtsp://cam/101", width=2560, height=1440, fps=20)
            m.configure("CAM", settings)
            self.assertEqual(len(self.FakeTranscode.made), 1)
            owner = self.FakeTranscode.made[0]
            m.configure("CAM", settings)          # Protect re-sent every channel
            self.assertEqual(owner.stopped, 0)     # gst/DetectServer stay up
            self.assertEqual(owner.restarted, 3)   # one re-send per channel
            m.stop_all()
            self.assertEqual(owner.stopped, 1)


class TestPushReconnect(unittest.TestCase):
    def test_reconnect_wakes_the_push_without_releasing_the_pull(self):
        pull = mock.Mock()
        push = onvif_push.FlvPush("CAM", "video1", "rtsp://cam/101", "S1",
                                  "tcp://127.0.0.1:1", pool=mock.Mock())
        push._pull = pull
        push.reconnect("Protect re-sent its settings")
        self.assertTrue(push._reconnect.is_set())
        self.assertEqual(push._queue.get_nowait(), ("end", "Protect re-sent its settings"))
        pull.unsubscribe.assert_not_called()

    def test_ending_a_push_sends_fin_to_protect(self):
        ours, protect = socket.socketpair()
        self.addCleanup(protect.close)
        push = onvif_push.FlvPush("CAM", "video1", "rtsp://cam/101", "S1", "tcp://127.0.0.1:1",
                                  pool=mock.Mock())
        push.put(("end", "camera stream ended"))
        header = b"FLV\x01\x05\x00\x00\x00\x09\x00\x00\x00\x00"
        with mock.patch.object(onvif_push.socket, "create_connection", return_value=ours):
            self.assertTrue(push._push_once(("start", header, [], 0), 1))
        protect.settimeout(1.0)
        self.assertEqual(protect.recv(64)[:3], b"FLV")
        self.assertEqual(protect.recv(16), b"")  # FIN, not just close() with a drain thread


class TestStreamName(unittest.TestCase):
    def test_replaced_and_tag_resized(self):
        tag = onvif_push._flv_tag(18, _meta("diyport"), 0)
        header, body = onvif_push.set_stream_name(tag[:11], tag[11:], "lJg3D2LxtQkAADlB")
        payload = body[:-4]
        self.assertEqual(int.from_bytes(header[1:4], "big"), len(payload))
        self.assertEqual(struct.unpack(">I", body[-4:])[0], len(payload) + 11)
        self.assertEqual(payload, _meta("lJg3D2LxtQkAADlB"))

    def test_other_tags_untouched(self):
        tag = onvif_push._flv_tag(9, _KEY, 40)
        self.assertEqual(onvif_push.set_stream_name(tag[:11], tag[11:], "X"),
                         (tag[:11], tag[11:]))


class TestTranscode(unittest.TestCase):
    """Option C: decode once, one encoder and FLV pipe per channel."""

    OUTPUTS = [(7, 2560, 1440, 20, 4000, 20), (8, 640, 360, 20, 2000, 24),
               (9, None, None, None, 500, 24)]

    def test_command_decodes_once_and_encodes_per_output(self):
        cmd = onvif_push.transcode_cmd("gst", "rtsp://cam/101", self.OUTPUTS, audio="pcmu")
        self.assertEqual(cmd.count("vtdec"), 1)
        self.assertEqual(cmd.count("vtenc_h264"), 3)
        self.assertIn("latency=500", cmd)
        for fd, kbps in ((7, 4000), (8, 2000), (9, 500)):
            self.assertIn(f"fd={fd}", cmd)
            self.assertIn(f"bitrate={kbps}", cmd)
            self.assertIn(f"mux{fd}.audio", cmd)  # the camera's audio teed to each mux
        self.assertIn("video/x-raw,format=NV12,width=640,height=360,framerate=20/1", cmd)
        self.assertIn("video/x-raw,format=NV12", cmd)  # unknown size: the camera's
        self.assertEqual(cmd.count("rtppcmudepay"), 1)
        self.assertNotIn("mux.audio", cmd)

    def test_no_camera_audio_is_silent_aac(self):
        cmd = onvif_push.transcode_cmd("gst", "rtsp://cam/101", self.OUTPUTS, camera_audio=False)
        self.assertIn("wave=silence", cmd)
        self.assertEqual(cmd.count("avenc_aac"), 1)

    def test_pool_shares_one_transcode_across_channels(self):
        renditions = (("video1", 2560, 1440, 20, 4000, 20), ("video2", 640, 360, 20, 2000, 24))
        with mock.patch.object(onvif_push.TranscodePull, "start", lambda self: self), \
                mock.patch.object(onvif_push.TranscodePull, "stop") as stop:
            pool = onvif_push.PullPool()
            v1 = pool.acquire("rtsp://cam/101", True, "video1", renditions)
            v2 = pool.acquire("rtsp://cam/101", True, "video2", renditions)
            self.assertIsNot(v1, v2)
            self.assertIs(v1.transcode, v2.transcode)
            pool.release(v1)
            stop.assert_not_called()
            pool.release(v2)
            stop.assert_called_once()

    def test_flvmux_metadata_gets_a_stream_name(self):
        amf = onvif_push._amf_string
        flvmux = (b"\x02" + amf("onMetaData") + b"\x08" + struct.pack(">I", 1)
                  + amf("duration") + b"\x00" + struct.pack(">d", 0) + b"\x00\x00\x09")
        tag = onvif_push._flv_tag(18, flvmux, 0)
        _, body = onvif_push.set_stream_name(tag[:11], tag[11:], "diyport")
        self.assertEqual(body[:-4], b"\x02" + amf("onMetaData") + b"\x08" + struct.pack(">I", 2)
                         + amf("streamName") + b"\x02" + amf("diyport")
                         + amf("duration") + b"\x00" + struct.pack(">d", 0) + b"\x00\x00\x09")

    def test_only_a_transcode_push_syncs_as_a_g3(self):
        # C was only ever run live with G3 syncs; the current option keeps its early sync.
        for renditions, g3 in ((None, False), ((("video1", 1, 1, 20, 1, 20),), True)):
            push = onvif_push.FlvPush("CAM", "video1", "rtsp://cam/101", "S", "tcp://h:1",
                                      pool=mock.Mock(), renditions=renditions)
            writer = push._new_writer(lambda data: None)
            self.assertEqual((writer.g3_sync, writer.early_sync), (g3, not g3))

    def test_restart_replaces_a_hung_pipeline(self):
        # Live 2026-11-03 17:24: gst-launch alive, camera socket CLOSED, no output for 2.5 h.
        pull = onvif_push.TranscodePull("rtsp://cam/101", False,
                                        (("video1", 2560, 1440, 20, 4000, 20),))
        with mock.patch.object(onvif_push, "transcode_cmd", return_value=["sleep", "30"]):
            runner = threading.Thread(target=pull._pull_once, args=(1,), daemon=True)
            runner.start()
            while pull._proc is None:
                time.sleep(0.01)
            self.assertFalse(pull.restart("re-send"))    # just started: left to come up
            pull._restarted -= onvif_push.RECONNECT_MIN_S
            self.assertTrue(pull.restart("re-send"))
            self.assertFalse(pull.restart("re-send"))    # the other channel's re-send
            runner.join(timeout=10)
        self.assertFalse(runner.is_alive())
        self.assertIsNotNone(pull._proc.poll())

    def _pull(self, *channels):
        pull = onvif_push.TranscodePull("rtsp://cam/101", False, tuple(
            (c, 2560, 1440, 20, 4000, 20) for c in channels))
        pull._proc = mock.Mock(pid=1)
        pull._proc.poll.return_value = None
        return pull

    def test_re_send_leaves_a_pipeline_that_is_producing(self):
        # A restart costs all three channels; a push reconnect is enough while frames flow.
        pull = self._pull("video1", "video3")
        now = time.monotonic()
        pull._restarted = now - 60
        for out in pull.outputs.values():
            out._last_video = now
        self.assertFalse(pull.restart("re-send"))
        self.assertFalse(pull._restart.is_set())
        pull.outputs["video3"]._last_video = now - onvif_push.RESEND_RESTART_QUIET_S - 1
        self.assertTrue(pull.restart("re-send"))
        self.assertTrue(pull._restart.is_set())

    def test_silent_channels_count_from_the_pipeline_start(self):
        pull = self._pull("video1", "video3")
        pull._restarted = 100.0
        pull.outputs["video1"]._last_video = 125.0
        pull.outputs["video3"]._last_video = 90.0   # from before this pipeline
        self.assertEqual(pull.silent_channels(30, now=129.0), [])
        self.assertEqual(pull.silent_channels(30, now=131.0), ["video3"])
        self.assertEqual(pull.silent_channels(30, now=156.0), ["video1", "video3"])

    def test_watchdog_replaces_a_pipeline_with_no_output(self):
        # Live 2026-11-03 17:24: gst-launch alive, no output for 2.5 h. No re-send here.
        pull = onvif_push.TranscodePull("rtsp://cam/101", False,
                                        (("video1", 2560, 1440, 20, 4000, 20),))
        with mock.patch.object(onvif_push, "transcode_cmd", return_value=["sleep", "30"]), \
                mock.patch.object(onvif_push, "TRANSCODE_STALL_S", 0.2):
            started = time.monotonic()
            self.assertFalse(pull._pull_once(1))
        self.assertLess(time.monotonic() - started, 10)
        self.assertIsNotNone(pull._proc.poll())

    def test_watchdog_can_be_turned_off(self):
        pull = onvif_push.TranscodePull("rtsp://cam/101", False,
                                        (("video1", 2560, 1440, 20, 4000, 20),))
        with mock.patch.object(onvif_push, "transcode_cmd", return_value=["sleep", "30"]), \
                mock.patch.object(onvif_push, "TRANSCODE_STALL_S", 0):
            runner = threading.Thread(target=pull._pull_once, args=(1,), daemon=True)
            runner.start()
            runner.join(timeout=0.5)
            self.assertTrue(runner.is_alive())
            pull._stop.set()
            pull._proc.terminate()
            runner.join(timeout=10)
        self.assertFalse(runner.is_alive())

    def test_only_video_counts_as_output(self):
        # Silent AAC keeps flowing from its own branch when the video stops.
        pull = onvif_push.SharedPull("rtsp://cam/101")
        pull._frames = 0
        flv = b"FLV\x01\x05\0\0\0\x09\0\0\0\0"
        audio = onvif_push._flv_tag(onvif_push._TAG_AUDIO, bytes([0x70, 0]), 0)
        pull._consume(io.BytesIO(flv + audio).read)
        self.assertIsNone(pull._last_video)
        video = onvif_push._flv_tag(onvif_push._TAG_VIDEO, bytes([0x27, 1, 0, 0, 0]), 50)
        pull._consume(io.BytesIO(flv + video).read)
        self.assertIsNotNone(pull._last_video)

    def test_push_notes_each_video_frame_it_sends(self):
        ours, protect = socket.socketpair()
        self.addCleanup(protect.close)
        push = onvif_push.FlvPush("CAM", "video1", "rtsp://cam/101", "S", "tcp://h:1",
                                  pool=mock.Mock())
        push.STALL_S = 0.05
        push.last_sent = 0.0
        flv = b"FLV\x01\x05\0\0\0\x09\0\0\0\0"
        tag = onvif_push._flv_tag(onvif_push._TAG_AUDIO, bytes([0x70, 0]), 0)
        push.put(("tag", tag[:11], tag[11:]))
        with mock.patch.object(onvif_push.socket, "create_connection", return_value=ours):
            push._push_once(("start", flv, [], 0), 1)
        self.assertEqual(push.last_sent, 0.0)   # audio isn't video
        ours, protect2 = socket.socketpair()
        self.addCleanup(protect2.close)
        tag = onvif_push._flv_tag(onvif_push._TAG_VIDEO, bytes([0x27, 1, 0, 0, 0]), 50)
        push.put(("tag", tag[:11], tag[11:]))
        with mock.patch.object(onvif_push.socket, "create_connection", return_value=ours):
            push._push_once(("start", flv, [], 0), 2)
        self.assertGreater(push.last_sent, 0.0)

    def test_restart_source_only_touches_a_transcode(self):
        push = onvif_push.FlvPush("CAM", "video1", "rtsp://cam/101", "S", "tcp://h:1",
                                  pool=mock.Mock())
        push._pull = mock.Mock(spec=onvif_push.SharedPull)
        push.restart_source("re-send")                   # a plain pull has no pipeline to restart
        owner = mock.Mock(spec=onvif_push.TranscodePull)
        push._pull = mock.Mock(transcode=owner)
        push.restart_source("re-send")
        owner.restart.assert_called_once_with("re-send")


class TestAudioTrackFallback(unittest.TestCase):
    def test_video_without_audio_retries_with_silent_aac(self):
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".flv", delete=False) as f:
            f.write(_flv([(9, 0, _AVC_CONFIG), (9, 0, _KEY)]))
        self.addCleanup(os.unlink, f.name)
        cmd = [sys.executable, "-c",
               "import sys; sys.stdout.buffer.write(open(sys.argv[1], 'rb').read())", f.name]
        pull = onvif_push.SharedPull("rtsp://cam/101", camera_audio=True)
        with mock.patch.object(onvif_push, "ffmpeg_push_cmd", lambda *a, **k: cmd):
            self.assertFalse(pull._pull_once(1))
        self.assertFalse(pull.camera_audio)


class TestPullPool(unittest.TestCase):
    def test_one_pull_per_stream_until_last_user(self):
        with mock.patch.object(onvif_push, "SharedPull") as SharedPull:
            pool = onvif_push.PullPool()
            made = []

            def start_returns_self(url, audio, ffmpeg_bin):
                pull = mock.Mock(source_url=url, camera_audio=audio)
                pull.start.return_value = pull
                made.append(pull)
                return pull

            SharedPull.side_effect = start_returns_self
            a = pool.acquire("rtsp://cam/101", True)
            b = pool.acquire("rtsp://cam/101", True)
            c = pool.acquire("rtsp://cam/102", True)
            self.assertIs(a, b)
            self.assertIsNot(a, c)
            pool.release(a)
            a.stop.assert_not_called()
            pool.release(b)
            a.stop.assert_called_once()
            self.assertIsNot(pool.acquire("rtsp://cam/101", True), a)
            self.assertEqual(len(made), 3)


class _Sink:
    def __init__(self, accept=True):
        self.items, self.accept = [], accept

    def put(self, item):
        if self.accept:
            self.items.append(item)
        return self.accept


class TestFanOut(unittest.TestCase):
    def setUp(self):
        self.pull = onvif_push.SharedPull("rtsp://cam/101")
        self.pull._header = b"FLV\x01\x05\x00\x00\x00\x09\x00\x00\x00\x00"
        self.pull._config = {"video": ("vh", "vb"), "meta": ("mh", "mb")}

    def _tag(self, payload, ts):
        tag = onvif_push._flv_tag(9, payload, ts)
        return tag[:11], tag[11:]

    def test_joins_at_key_frame_with_config_first(self):
        sink = _Sink()
        self.pull.subscribe(sink)
        self.pull._deliver(*self._tag(_INTER, 960), keyframe=False)
        self.assertEqual(sink.items, [])
        key = self._tag(_KEY, 1000)
        self.pull._deliver(*key, keyframe=True)
        self.pull._deliver(*self._tag(_INTER, 1040), keyframe=False)
        self.assertEqual(sink.items[0], ("start", self.pull._header,
                                         [("mh", "mb"), ("vh", "vb")], 1000))
        self.assertEqual(sink.items[1], ("tag",) + key)
        self.assertEqual([i[0] for i in sink.items], ["start", "tag", "tag"])

    def test_sink_that_fell_behind_rejoins_at_key_frame(self):
        sink = _Sink()
        self.pull.subscribe(sink)
        self.pull._deliver(*self._tag(_KEY, 1000), keyframe=True)
        sink.accept = False
        self.pull._deliver(*self._tag(_INTER, 1040), keyframe=False)
        sink.accept = True
        self.pull._deliver(*self._tag(_INTER, 1080), keyframe=False)
        self.assertEqual(len(sink.items), 2)  # nothing until the next key frame
        self.pull._deliver(*self._tag(_KEY, 2000), keyframe=True)
        self.assertEqual(sink.items[2][0], "start")


class TestAudioCopyFallback(unittest.TestCase):
    """Audio Protect won't take copied (ffmpeg 8 writes Opus as Enhanced FLV, sound
    format 9) switches the pull to AAC for its next attempt."""

    def _pull_once(self, source):
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".flv", delete=False) as f:
            f.write(source)
        self.addCleanup(os.unlink, f.name)
        launched = []

        def fake_cmd(ffmpeg_bin, url, name, camera_audio=True, copy_audio=True, wallclock=True):
            launched.append(copy_audio)
            return [sys.executable, "-c",
                    "import sys; sys.stdout.buffer.write(open(sys.argv[1],'rb').read())", f.name]

        pull = onvif_push.SharedPull("rtsp://cam/101")
        with mock.patch.object(onvif_push, "ffmpeg_push_cmd", fake_cmd), \
                self.assertLogs("aiport-onvif", "WARNING"):
            pull._pull_once(1)
            pull._pull_once(2)
        return pull, launched

    def test_enhanced_flv_audio_falls_back_to_aac(self):
        opus = b"\x9f" + b"Opus" + b"\x00"
        pull, launched = self._pull_once(_flv([(9, 0, _AVC_CONFIG), (8, 0, opus), (9, 0, _KEY)]))
        self.assertFalse(pull._copy_audio)
        self.assertEqual(launched, [True, False])

    def test_g711_stays_copied(self):
        mulaw = b"\x82" + b"\x7f" * 160  # sound format 8: G.711 mu-law
        pull, launched = self._pull_once(_flv([(9, 0, _AVC_CONFIG), (8, 0, mulaw), (9, 0, _KEY)]))
        self.assertTrue(pull._copy_audio)
        self.assertEqual(launched, [True, True])


class TestMalformedMetadata(unittest.TestCase):
    def test_truncated_metadata_restarts_instead_of_killing_the_thread(self):
        import tempfile
        payload = b"\x02\x00\x0aonMetaData\x00\x05width\x00\x01"
        with tempfile.NamedTemporaryFile(suffix=".flv", delete=False) as f:
            f.write(_flv([(18, 0, payload)]))
        self.addCleanup(os.unlink, f.name)
        cmd = [sys.executable, "-c",
               "import sys; sys.stdout.buffer.write(open(sys.argv[1], 'rb').read())", f.name]
        pull = onvif_push.SharedPull("rtsp://cam/101")
        with mock.patch.object(onvif_push, "ffmpeg_push_cmd", lambda *a, **k: cmd):
            self.assertFalse(pull._pull_once(1))  # caught and retried, not raised


class TestFlvPushMatches(unittest.TestCase):
    def test_audio_flag_is_part_of_the_match(self):
        push = onvif_push.FlvPush("CAM", "video1", "rtsp://cam/101", "S1",
                                  "tcp://127.0.0.1:7550", camera_audio=True)
        self.assertTrue(push.matches("rtsp://cam/101", "S1", "tcp://127.0.0.1:7550",
                                     None, True))
        self.assertFalse(push.matches("rtsp://cam/101", "S1", "tcp://127.0.0.1:7550",
                                      None, False))


class TestSharedPushes(unittest.TestCase):
    """Two channels on one stream: one ffmpeg, two connections, each under its
    own streamName and starting at t=0 on a key frame."""

    def test_two_pushes_one_ffmpeg(self):
        import socket
        import tempfile
        import threading

        with tempfile.NamedTemporaryFile(suffix=".flv", delete=False) as f:
            f.write(_SOURCE)
        self.addCleanup(os.unlink, f.name)
        cat = [sys.executable, "-c",
               "import sys,time; sys.stdout.buffer.write(open(sys.argv[1],'rb').read());"
               "sys.stdout.flush(); time.sleep(10)", f.name]
        launched = []

        def fake_cmd(*args, **kw):
            launched.append(args)
            return cat

        servers, received = [], {}
        first = {name: threading.Event() for name in ("S1", "S2")}
        for name in ("S1", "S2"):
            srv = socket.socket()
            srv.bind(("127.0.0.1", 0))
            srv.listen(4)
            srv.settimeout(10)
            servers.append((name, srv))
            self.addCleanup(srv.close)

        def serve(name, srv):
            conn, _ = srv.accept()
            conn.settimeout(10)
            data = b""
            while True:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                data += chunk
                tags = _parse_extended(data)
                if len(tags) >= 9 and tags[-1][1] == 1000 and tags[-1][2] == _KEY:
                    first[name].set()  # the whole expected stream has arrived
            conn.close()
            received[name] = data

        threads = [threading.Thread(target=serve, args=s) for s in servers]
        for t in threads:
            t.start()
        with mock.patch.object(onvif_push, "ffmpeg_push_cmd", fake_cmd):
            pool = onvif_push.PullPool()
            pushes = [onvif_push.FlvPush("CAM", f"video{i + 1}", "rtsp://cam/101", name,
                                         f"tcp://127.0.0.1:{srv.getsockname()[1]}",
                                         pool=pool).start()
                      for i, (name, srv) in enumerate(servers)]
            # Stop once both pushes have delivered, not after a fixed source lifetime:
            # the pull would otherwise retry and launch a second ffmpeg.
            for event in first.values():
                self.assertTrue(event.wait(10), "a push delivered no complete stream")
            for push in pushes:
                push.stop()
            for t in threads:
                t.join(15)
        self.assertEqual(len(launched), 1)
        for name in ("S1", "S2"):
            data = received[name]
            self.assertEqual(data[:5], b"FLV\x01\x07")
            tags = _parse_extended(data)
            self.assertEqual(tags[0], (18, 0, _meta(name)))
            self.assertEqual(tags[1:3], [(9, 0, _AVC_CONFIG), (8, 0, _AAC_CONFIG)])
            self.assertEqual(tags[3], (9, 0, _KEY))  # rebased to the key frame it joined at
            # The first clock sync right after the first frame, so ms keeps the recording.
            self.assertIn(b"onClockSync", tags[4][2])
            self.assertIn(b"onMpma", tags[5][2])
            self.assertEqual([t[1] for t in tags[6:]], [10, 40, 1000])


class TestDrainSocket(unittest.TestCase):
    def test_reads_what_protect_sends_until_closed(self):
        # Live 2026-11-01: nothing read Protect's replies, 140 KB piled up per push socket.
        ours, protect = socket.socketpair()
        ours.settimeout(0.05)  # quiet spells (the push's send timeout) don't end it
        thread = onvif_push.drain_socket(ours, "test")
        time.sleep(0.15)
        protect.sendall(b"x" * 300000)  # more than a socket buffer holds unread
        protect.close()
        thread.join(5)
        self.assertFalse(thread.is_alive())
        ours.close()


class SharedDecodeTest(unittest.TestCase):
    """AIPORT_ONVIF_SHARED_DECODE: detection decodes the pushed video1 with Protect's times."""

    @staticmethod
    def _video(ts, key):
        payload = bytes([0x17 if key else 0x27, 1, 0, 0, 0]) + b"x"
        return onvif_push._flv_tag(onvif_push._TAG_VIDEO, payload, ts)

    def _split(self, tag):
        return tag[:11], tag[11:]

    def test_frames_carry_protects_wall_time(self):
        a, b = socket.socketpair()
        pull = mock.Mock()
        clock = iter([1000.0, 1000.5, 1010.0])
        with mock.patch.object(onvif_push.time, "time", lambda: next(clock)):
            client = onvif_push.DetectClient(a, pull)
            config = self._split(onvif_push._flv_tag(onvif_push._TAG_VIDEO,
                                                     bytes([0x17, 0, 0, 0, 0]), 5000))
            client.put(("start", b"FLV\x01\x05" + bytes(8), [config], 5000))
            client.put(("tag", *self._split(self._video(5000, True))))   # sync: 1000 s at 0
            client.put(("tag", *self._split(self._video(5100, False))))  # 100 ms on
            client.put(("tag", *self._split(self._video(5200, True))))   # new sync at 1010 s
            client.put(("end", "done"))
        f = b.makefile("rb")
        records = list(onvif_push.read_detect_records(f.read))
        self.assertEqual([k for k, _, _ in records], [0, 0, 1, 1, 1])
        self.assertEqual([w for k, w, _ in records if k], [1000000.0, 1000100.0, 1010000.0])
        self.assertEqual(onvif_push._tag_timestamp(records[3][2][:11]), 100)  # retimed from base
        pull.unsubscribe.assert_called_once_with(client)

    def test_frames_take_the_wall_time_the_push_sent(self):
        # The push sends a frame 2 s after detection saw it: the frame still gets Protect's time.
        a, b = socket.socketpair()
        pull = mock.Mock(protect_clock=onvif_push.ProtectClock())
        client = onvif_push.DetectClient(a, pull)
        config = self._split(onvif_push._flv_tag(onvif_push._TAG_VIDEO,
                                                 bytes([0x17, 0, 0, 0, 0]), 5000))
        client.put(("start", b"FLV\x01\x05" + bytes(8), [config], 5000))
        client.put(("tag", *self._split(self._video(5000, True))))
        client.put(("tag", *self._split(self._video(5100, False))))
        client.put(("tag", *self._split(self._video(5200, True))))
        client.put(("end", "done"))
        clock = pull.protect_clock
        times = iter([2000.0] * 3 + [2010.0])
        writer = onvif_push.ExtendedFlvWriter(lambda d: None, clock=times.__next__, g3_sync=True)
        writer.on_sync = lambda ts, wall: clock.synced(ts + 5000, wall)
        writer.header(b"FLV\x01\x05" + bytes(8))
        writer.initial_sync()                          # streamClock 0 = pull 5000 at 2000 s
        clock.wrote(5100)
        writer.sync_if_due(200, keyframe=True)         # pull 5200 at 2010 s
        clock.wrote(5200)
        f = b.makefile("rb")
        records = list(onvif_push.read_detect_records(f.read))
        self.assertEqual([w for k, w, _ in records if k], [2000000.0, 2000100.0, 2010000.0])

    def test_no_push_falls_back_to_our_clock(self):
        a, b = socket.socketpair()
        pull = mock.Mock(protect_clock=onvif_push.ProtectClock())
        with mock.patch.object(onvif_push, "PROTECT_CLOCK_WAIT_S", 0.05):
            client = onvif_push.DetectClient(a, pull)
            client.put(("start", b"FLV\x01\x05" + bytes(8), [], 0))
            with mock.patch.object(onvif_push.time, "time", lambda: 1000.0):
                client.put(("tag", *self._split(self._video(0, True))))
            client.put(("end", "done"))
            records = list(onvif_push.read_detect_records(b.makefile("rb").read))
        self.assertEqual([w for k, w, _ in records if k], [1000000.0])

    def test_overflow_drops_the_client(self):
        a, b = socket.socketpair()
        with mock.patch.object(onvif_push, "DETECT_QUEUE", 1), \
                mock.patch.object(onvif_push.threading, "Thread"):
            client = onvif_push.DetectClient(a, mock.Mock())
            client.put(("start", bytes(13), [(bytes(11), bytes(4))], 0))
            self.assertTrue(client.dead)
        a.close()
        b.close()

    def test_socket_named_from_url(self):
        a = onvif_push.detect_socket("rtsp://u:p@cam/1")
        self.assertEqual(a, onvif_push.detect_socket("rtsp://u:p@cam/1"))
        self.assertNotEqual(a, onvif_push.detect_socket("rtsp://u:p@cam/2"))
        self.assertNotIn("u:p", a)
        self.assertEqual(os.stat(os.path.dirname(a)).st_mode & 0o777, 0o700)

    def test_socket_is_namespaced_by_instance(self):
        with mock.patch.dict(os.environ, {"AIPORT_DETECT_NAMESPACE": "/state/a"}):
            a = onvif_push.detect_socket("rtsp://cam/1")
            again = onvif_push.detect_socket("rtsp://cam/1")
        with mock.patch.dict(os.environ, {"AIPORT_DETECT_NAMESPACE": "/state/b"}):
            b = onvif_push.detect_socket("rtsp://cam/1")
        self.assertEqual(a, again)
        self.assertNotEqual(a, b)


if __name__ == "__main__":
    unittest.main()
