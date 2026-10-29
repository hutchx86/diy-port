import os
import struct
import sys
import unittest
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import camera_clock  # noqa: E402

os.environ.setdefault("AIPORT_TICK_S", "0.33")       # tests assume macOS's tick and
os.environ.setdefault("AIPORT_DETECT_FPS", "10")    # detection rate on every platform
try:
    import avclient as av
except Exception as exc:  # pragma: no cover
    av = None
    _AV_ERR = exc


def sender_report(ntp_s, rtp):
    sec = int(ntp_s) + camera_clock.NTP_UNIX_OFFSET
    frac = int((ntp_s - int(ntp_s)) * 2 ** 32)
    sr = struct.pack(">BBHIIII", 0x80, 200, 6, 0x1234, sec, frac, rtp) + b"\0" * 8
    sdes = struct.pack(">BBH", 0x81, 202, 1) + b"\0" * 4
    return sr + sdes


class TestParse(unittest.TestCase):
    def test_sender_report(self):
        # Seen live from Camera A: ntp 339676.832, rtp 506143830.
        (ntp, rtp), = camera_clock.parse_sender_reports(sender_report(339676.832, 506143830))
        self.assertAlmostEqual(ntp, 339676.832, places=3)
        self.assertEqual(rtp, 506143830)

    def test_no_sender_report(self):
        self.assertEqual(camera_clock.parse_sender_reports(struct.pack(">BBH", 0x81, 202, 0)), [])
        self.assertEqual(camera_clock.parse_sender_reports(b""), [])


class TestReader(unittest.TestCase):
    def test_offset_is_the_median_of_recent_reports(self):
        r = camera_clock.Reader("CAM", "rtsp://h:1/x")
        self.assertIsNone(r.offset_ms())
        for received, ntp in ((1000.010, 10.0), (1005.012, 15.0), (1010.500, 20.0)):
            r.add_report(ntp, received)
        self.assertAlmostEqual(r.offset_ms(), 990012.0, places=3)  # the 0.5 s outlier ignored
        for i in range(30):
            r.add_report(100.0 + i, 1100.0 + i)
        self.assertEqual(len(r.offsets), camera_clock.OFFSETS_KEPT)


@unittest.skipIf(av is None, "avclient unavailable")
class TestAvclientClocks(unittest.TestCase):
    def setUp(self):
        av._analysed_frame_time["CC"] = (1_792_000_000_000, 5_000)
        av._stream_started_monotonic_ms["CC"] = 1_000
        self.addCleanup(av._analysed_frame_time.pop, "CC", None)
        self.addCleanup(av._stream_started_monotonic_ms.pop, "CC", None)
        self.addCleanup(av._camera_clocks.pop, "CC", None)

    def test_camera_clock_used_when_known(self):
        reader = camera_clock.Reader("CC", "rtsp://h:1/x")
        reader.add_report(339_676.0, 1_791_999_999.0)   # local - camera = 1 791 660 323 s
        av._camera_clocks["CC"] = reader
        mono, stream = av._stream_clocks("CC")
        self.assertEqual(mono, stream)
        self.assertEqual(mono, 1_792_000_000_000 - 1_791_660_323_000)  # 339 677 000 ms

    def test_falls_back_without_a_report(self):
        av._camera_clocks["CC"] = camera_clock.Reader("CC", "rtsp://h:1/x")
        with mock.patch.object(av, "_box_delay_ms", lambda dev: 0):
            self.assertEqual(av._stream_clocks("CC"), (4_000, 4_000))

    def test_wall_skew(self):
        with mock.patch.object(av, "_WALL_SKEW_MS", {"CC": 5000}):
            self.assertEqual(av._clock_wall("CC", 1000), 6000)
            self.assertEqual(av._clock_wall("OTHER", 1000), 1000)

    def test_reader_only_for_listed_cameras(self):
        with mock.patch.object(av, "CAMERA_CLOCK_DEVICES", set()), \
                mock.patch.object(camera_clock.Reader, "start") as start:
            av._start_camera_clock("CC", "rtsp://h:1/x")
            start.assert_not_called()
        self.assertNotIn("CC", av._camera_clocks)


if __name__ == "__main__":
    unittest.main()
