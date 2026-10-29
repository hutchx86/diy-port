# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 hutchx86
"""Unit tests for avclient.py's pure logic: geometry, zone/line
parsing + crossing, snapshots. No sockets/device; ffmpeg tests skipped if absent.
"""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

os.environ.setdefault("AIPORT_TICK_S", "0.33")       # tests assume macOS's tick and
os.environ.setdefault("AIPORT_DETECT_FPS", "10")    # detection rate on every platform
import avclient as av  # noqa: E402
import config  # noqa: E402

_FFMPEG = shutil.which(av.FFMPEG_BIN) or (av.FFMPEG_BIN if os.path.exists(av.FFMPEG_BIN) else None)
_CV2 = importlib.util.find_spec("cv2") is not None   # the Frigate motion gate, regions, stationary

SQUARE = [(0, 0), (10, 0), (10, 10), (0, 10)]  # note: normalized 0-1000 in real use


def make_frame(path, w=1920, h=1080):
    subprocess.run([av.FFMPEG_BIN, "-loglevel", "error", "-f", "lavfi",
                    "-i", f"testsrc=size={w}x{h}", "-frames:v", "1", "-y", path],
                   check=True, capture_output=True)


class Base(unittest.TestCase):
    def setUp(self):
        av._lines.clear()
        av._detect_zones.clear()
        av._detect_zone_types.clear()
        av._detect_zone_ids.clear()
        av._exclude_zones.clear()
        av._snapshot_filename_to_device.clear()
        av._recent_snapshot_announcements.clear()
        av._last_object_coord_by_device.clear()
        av._stream_dims.clear()
        av._active_streams.clear()
        av._pending_leaves.clear()
        av._latest_frame.clear()
        av._latest_frame_time.clear()
        av._analysed_frame_time.clear()
        av._stream_started_monotonic_ms.clear()
        # Apple's vehicle model has its own tests (test_apple_fm); here it would ask the real one.
        patcher = mock.patch.object(av, "VEHICLE_FM", False)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.sent = []
        self._orig_send = av.send_msg
        av.send_msg = lambda ws, fn, payload, **kw: self.sent.append((fn, payload))
        self.addCleanup(lambda: setattr(av, "send_msg", self._orig_send))


class TestGeometry(unittest.TestCase):
    def test_point_in_polygon(self):
        self.assertTrue(av._point_in_polygon(5, 5, SQUARE))
        self.assertFalse(av._point_in_polygon(15, 5, SQUARE))
        self.assertFalse(av._point_in_polygon(-1, 5, SQUARE))
        # concave polygon (an L) -- the ray-cast must not treat the notch as inside
        L = [(0, 0), (10, 0), (10, 4), (4, 4), (4, 10), (0, 10)]
        self.assertTrue(av._point_in_polygon(2, 2, L))
        self.assertFalse(av._point_in_polygon(8, 8, L))

    def test_zone_center_norm(self):
        self.assertEqual(av._zone_center_norm([0, 0, 10, 20]), (5.0, 10.0))
        self.assertEqual(av._zone_center_norm([100, 200, 50, 50]), (125.0, 225.0))

    def test_iou(self):
        self.assertAlmostEqual(av._iou([0, 0, 10, 10], [0, 0, 10, 10]), 1.0)
        self.assertEqual(av._iou([0, 0, 10, 10], [100, 100, 10, 10]), 0.0)
        # half-overlap on x: inter=50, union=150
        self.assertAlmostEqual(av._iou([0, 0, 10, 10], [5, 0, 10, 10]), 50 / 150)

    def test_line_side_sign(self):
        p1, p2 = (0, 500), (1000, 500)
        self.assertLess(av._line_side(p1, p2, (500, 100)), 0)
        self.assertGreater(av._line_side(p1, p2, (500, 900)), 0)
        self.assertEqual(av._line_side(p1, p2, (500, 500)), 0)

    def test_segment_crosses_line(self):
        p1, p2 = (0, 500), (1000, 500)
        self.assertTrue(av._segment_crosses_line((500, 100), (500, 900), p1, p2))
        self.assertTrue(av._segment_crosses_line((500, 900), (500, 100), p1, p2))
        self.assertFalse(av._segment_crosses_line((500, 100), (600, 200), p1, p2))
        self.assertFalse(av._segment_crosses_line((100, 600), (900, 600), p1, p2))
        # movement crosses y=500 but outside the segment's x-span
        self.assertFalse(av._segment_crosses_line((900, 100), (900, 900), (0, 500), (100, 500)))


class TestLateDetector(Base):
    """A detector that loads after the pull started takes a different input size: its
    detect() resizes the frame, so detection doesn't stop (upstream 256ff57)."""

    def tearDown(self):
        av._latest_frame.pop("CAM", None)

    def test_late_detector_resizes_instead_of_skipping(self):
        try:
            import numpy as np
        except ImportError:
            self.skipTest("numpy not installed")
        from detectors import base

        class Det(base.Detector):
            def __init__(self):
                super().__init__()
                self.input_size = 320
                self.seen = None

            def _infer(self, frame):
                self.seen = frame.shape
                return np.array([[160.0, 160.0, 240.0, 320.0]]), np.array([0]), np.array([0.9])

        det = Det()
        av._latest_frame["CAM"] = np.zeros((640, 640, 3), dtype=np.uint8)
        with mock.patch.object(av, "_get_detector", return_value=det), \
                mock.patch.object(av, "REGIONS", False), \
                mock.patch.object(av, "DETECTOR_BACKEND", "edgetpu"):
            out = av._classify_frame_detections("CAM")
        self.assertEqual(det.seen, (320, 320, 3))  # resized to the model input
        self.assertEqual(len(out), 1)
        self.assertEqual([round(v) for v in out[0]["coord"]], [500, 500, 250, 500])


class TestParsers(Base):
    def test_parse_zones(self):
        field = {"1": {"coord": [0, 0, 10, 0, 10, 10, 0, 10]},
                 "2": {"coord": [1, 2, 3, 4]},          # too few points -> skipped
                 "3": {"coord": [0, 0, 1, 1, 2, 2, 3]}}  # odd -> skipped
        polys = av._parse_zones(field)
        self.assertEqual(len(polys), 1)
        self.assertEqual(polys[0], [(0, 0), (10, 0), (10, 10), (0, 10)])
        self.assertEqual(av._parse_zones(None), [])
        self.assertEqual(av._parse_zone_map(field), {"1": [(0, 0), (10, 0), (10, 10), (0, 10)]})

    def test_zone_id_follows_controller_key(self):
        field = {"7": {"coord": [0, 0, 1000, 0, 1000, 1000, 0, 1000]}}
        self.assertEqual(av._parse_zone_ids(field), [7])
        av._send_smart_detect_event(None, "M", edge_type="enter", zones=[7], tracker_id=5)
        payload = self.sent[-1][1]
        self.assertEqual(payload["descriptors"][0]["zones"], [7])
        self.assertIn("7", payload["zonesStatus"])

    def test_parse_lines_coord_and_points(self):
        lines = av._parse_lines({
            "1": {"coord": [0, 500, 1000, 500], "objectTypes": ["person"]},
            "2": {"points": [[100, 0], [100, 1000]]},
            "3": {"coord": [1, 2]},  # skipped
        })
        self.assertEqual([l["id"] for l in lines], ["1", "2"])
        self.assertEqual((lines[0]["p1"], lines[0]["p2"]), ((0, 500), (1000, 500)))
        self.assertEqual(lines[0]["object_types"], ["person"])
        self.assertEqual(lines[1]["p2"], (100, 1000))

    def test_line_ref(self):
        self.assertEqual(av._line_ref("1"), 1)
        self.assertEqual(av._line_ref("a"), "a")


class TestZoneFiltering(Base):
    def test_exclude_and_detect_zones(self):
        av._exclude_zones["M"] = [SQUARE]
        self.assertTrue(av._is_excluded("M", [4, 4, 2, 2]))    # bottom-centre (5,6)
        self.assertFalse(av._is_excluded("M", [40, 40, 2, 2]))  # bottom-centre (41,42)
        self.assertFalse(av._is_excluded("OTHER", [4, 4, 2, 2]))  # no zones -> not excluded

        av._detect_zones["M2"] = [SQUARE]
        self.assertFalse(av._is_outside_detect_zones("M2", [4, 4, 2, 2]))
        self.assertTrue(av._is_outside_detect_zones("M2", [40, 40, 2, 2]))
        self.assertFalse(av._is_outside_detect_zones("M3", [40, 40, 2, 2]))  # unset -> no restriction

    def test_zone_object_types(self):
        # Live 16:47 on Camera B: vehicles were reported from the plants zone, which Protect
        # has set to person/animal/face only.
        other = [(100, 0), (200, 0), (200, 100), (100, 100)]
        av._detect_zones["Z"] = [SQUARE, other]
        av._detect_zone_types["Z"] = [["person", "vehicle"], ["person", "animal", "face"]]
        in_square, in_other = [4, 4, 2, 2], [140, 40, 2, 2]
        self.assertFalse(av._is_outside_detect_zones("Z", in_square, "vehicle"))
        self.assertTrue(av._is_outside_detect_zones("Z", in_other, "vehicle"))
        self.assertFalse(av._is_outside_detect_zones("Z", in_other, "person"))
        self.assertTrue(av._is_outside_detect_zones("Z", in_square, "face"))
        self.assertFalse(av._is_outside_detect_zones("Z", in_square, "licensePlate"))  # rides vehicle
        self.assertTrue(av._is_outside_detect_zones("Z", in_other, "licensePlate"))
        self.assertFalse(av._is_outside_detect_zones("Z", in_other))  # no type: any zone

    def test_settings_carry_zone_types_and_survive_partial_updates(self):
        zones = {"1": {"coord": [0, 0, 10, 0, 10, 10, 0, 10], "objectTypes": ["vehicle"]},
                 "2": {"coord": [100, 0, 200, 0, 200, 100, 100, 100]}}
        av.handle_function(None, {"functionName": "ChangeSmartDetectSettings", "messageId": 1,
                                  "payload": {"deviceID": "Z", "zones": zones}}, {})
        self.assertEqual(av._detect_zone_types["Z"], [["vehicle"], None])
        self.assertEqual(av._detect_zone_ids["Z"], [1, 2])          # Protect's keys
        av.handle_function(None, {"functionName": "ChangeSmartDetectSettings", "messageId": 2,
                                  "payload": {"deviceID": "Z", "isLprCamera": 0}}, {})
        self.assertEqual(len(av._detect_zones["Z"]), 2)  # live 11:47: this wiped the zones
        self.assertEqual(av._detect_zone_types["Z"], [["vehicle"], None])


class TestLineCrossing(Base):
    LINE = {"id": "1", "p1": (0, 500), "p2": (1000, 500),
            "direction": None, "object_types": None}

    def _track(self, prev):
        return {"event_id": 7, "objectType": "person", "coord": [480, 80, 40, 40],
                "score": 0.9, "first_shown_ms": 1000, "miss_count": 0,
                "prev_center": prev, "crossed_lines": {}}

    def test_enter_then_moving_and_counters(self):
        av._lines["M"] = [self.LINE]
        tr = self._track((500, 100))
        av._process_line_crossings(None, "M", 3, tr, (500, 900))  # neg -> pos
        p = self.sent[-1][1]
        self.assertEqual(self.sent[-1][0], "EventSmartDetect")
        self.assertEqual(p["edgeType"], "enter")
        self.assertIn("linesStatus", p)
        self.assertNotIn("zonesStatus", p)
        self.assertEqual(p["descriptors"][0]["crosslineA2B"], 1)
        self.assertEqual(p["descriptors"][0]["crosslineB2A"], 0)
        self.assertEqual(p["descriptors"][0]["crosslineA2BAdd"], 1)
        ev = p["eventId"]

        # _motion_tick advances prev_center between ticks; do the same here
        tr["prev_center"] = (500, 900)
        av._process_line_crossings(None, "M", 3, tr, (500, 100))  # pos -> neg
        p = self.sent[-1][1]
        self.assertEqual(p["edgeType"], "moving")
        self.assertEqual(p["eventId"], ev)  # same track event
        self.assertEqual(p["linesStatus"]["1"]["direction"], "B2A")

    def test_no_crossing_emits_nothing(self):
        av._lines["M"] = [self.LINE]
        tr = self._track((500, 100))
        av._process_line_crossings(None, "M", 3, tr, (520, 110))
        self.assertEqual(self.sent, [])

    def test_object_type_filter(self):
        av._lines["M"] = [dict(self.LINE, object_types=["vehicle"])]
        tr = self._track((500, 100))
        av._process_line_crossings(None, "M", 3, tr, (500, 900))
        self.assertEqual(self.sent, [])

    def test_leave_payload_and_filenames(self):
        av._stream_dims["M"] = (1920, 1080)
        av._send_line_detect_event(None, "M", self.LINE, "leave", 42, "A2B", 1, 2,
                                   "person", [480, 80, 40, 40], 0.9, 1000, 3)
        _, p = self.sent[-1]
        self.assertEqual(p["edgeType"], "leave")
        self.assertEqual(p["trackerIDAttrMap"]["3"], {"objectType": "person", "line": [1]})
        self.assertTrue(p["smartDetectSnapshotFullFoV"].startswith("smartdetectsnap_line_"))
        self.assertTrue(p["smartDetectSnapshotFullFoV"].endswith("_fullfov.jpg"))
        self.assertEqual(p["smartDetectSnapshots"][0]["smartDetectSnapshotWidth"],
                         av.SMART_SNAPSHOT_SIZE)
        for fname in (p["smartDetectSnapshotFullFoV"],
                      p["smartDetectSnapshots"][0]["smartDetectSnapshot"]):
            self.assertEqual(av._snapshot_filename_to_device[fname]["device_id"], "M")
        json.dumps(p)  # must be JSON-serialisable


class TestSnapshotResolution(Base):
    def test_resolve_by_filename(self):
        av._record_snapshot_filename("f.jpg", "AAA", kind="fullfov")
        rec, how = av._resolve_snapshot_device("f.jpg", None)
        self.assertEqual((rec["device_id"], how), ("AAA", "filename"))

    def test_resolve_sole_active_stream(self):
        av._active_streams["AAA"] = object()
        rec, how = av._resolve_snapshot_device(None, "smartDetectZoneSnapshot")
        self.assertEqual((rec["device_id"], how), ("AAA", "sole active stream"))

    def test_resolve_sole_recent_announcement(self):
        av._active_streams["AAA"] = object()
        av._active_streams["BBB"] = object()
        av._record_snapshot_filename("b.jpg", "BBB", kind="object", coord=[1, 2, 3, 4])
        rec, how = av._resolve_snapshot_device(None, "smartDetectZoneSnapshot")
        self.assertEqual((rec["device_id"], how), ("BBB", "sole recent announcement"))
        self.assertEqual(rec["coord"], [1, 2, 3, 4])  # reuses last object coord

    def test_resolve_ambiguous_refuses(self):
        av._active_streams["AAA"] = object()
        av._active_streams["BBB"] = object()
        av._record_snapshot_filename("a.jpg", "AAA", kind="object", coord=[1, 2, 3, 4])
        av._record_snapshot_filename("b.jpg", "BBB", kind="object", coord=[1, 2, 3, 4])
        rec, how = av._resolve_snapshot_device(None, "smartDetectZoneSnapshot")
        self.assertIsNone(rec)
        self.assertIsNone(how)

    def test_fallback_kind_from_what(self):
        self.assertEqual(av._fallback_record("X", "smartDetectZoneSnapshotFullFoV")["kind"], "fullfov")
        self.assertEqual(av._fallback_record("X", "smartDetectZoneSnapshot")["kind"], "object")

    def test_resolve_by_request_deviceid(self):
        av._active_streams["AA:BB:CC"] = object()
        rec, how = av._resolve_snapshot_device(None, "snapshot", "aa:bb:cc")
        self.assertEqual((rec["device_id"], how), ("AA:BB:CC", "request deviceID"))
        self.assertEqual(rec["kind"], "fullfov")

    def test_general_snapshot_not_cropped_by_stale_coord(self):
        # A general "snapshot" must not reuse a stale per-object coord as a crop.
        av._last_object_coord_by_device["AAA"] = [1, 2, 3, 4]
        rec = av._fallback_record("AAA", "snapshot")
        self.assertEqual(rec["kind"], "fullfov")
        self.assertIsNone(rec["coord"])

    def test_wait_for_frame(self):
        with tempfile.TemporaryDirectory() as d:
            missing = os.path.join(d, "no.jpg")
            t = time.monotonic()
            self.assertFalse(av._wait_for_frame(missing, timeout=0.4))
            self.assertGreaterEqual(time.monotonic() - t, 0.3)
            late = os.path.join(d, "late.jpg")
            threading.Timer(0.2, lambda: open(late, "wb").write(b"x")).start()
            self.assertTrue(av._wait_for_frame(late, timeout=2.0))


@unittest.skipUnless(_FFMPEG, "ffmpeg not installed")
class TestSnapshotCrop(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self._dir, True)
        self.path = os.path.join(self._dir, "f.jpg")
        make_frame(self.path, 1504, 2016)

    def test_jpeg_size(self):
        self.assertEqual(av._jpeg_size(self.path), (1504, 2016))
        bogus = os.path.join(self._dir, "x.jpg")
        with open(bogus, "wb") as f:
            f.write(b"not a jpeg")
        self.assertIsNone(av._jpeg_size(bogus))

    def test_crop_uses_real_frame_size(self):
        # Announced 1920x2560 but delivered 1504x2016 (seen live on a doorbell):
        # the crop must fit the real frame instead of failing.
        out = av._crop_snapshot_jpeg(self.path, [308, 346, 306, 620], 1920, 2560)
        self.assertTrue(out and out.startswith(b"\xff\xd8"))


class TestProtectCanvas(Base):
    """Protect draws 0-1000 coords on a 16:9 canvas; other aspects sit centred in it."""

    def test_16_9_unchanged(self):
        av._stream_dims["W"] = (2560, 1440)
        self.assertEqual(av._to_protect("W", [100, 200, 300, 400]), [100, 200, 300, 400])

    def test_portrait_pillarboxed(self):
        # 1920x2560 doorbell: x squeezes toward the centre by 0.75 / (16/9).
        av._stream_dims["D"] = (1920, 2560)
        x, y, w, h = av._to_protect("D", [0, 244, 1000, 747])
        self.assertAlmostEqual(x, 500 - 500 * 0.421875)
        self.assertAlmostEqual(w, 421.875)
        self.assertEqual((y, h), (244, 747))

    def test_zones_use_the_bottom_centre(self):
        # Camera C's zone 2 (2026-10-17): its top edge runs from (781, 262) to (1000, 356).
        # A person by the hedge: box centre above that edge, feet below it.
        av._stream_dims["B"] = (2688, 1512)
        av._detect_zones["B"] = [[(22, 353), (781, 262), (1000, 356), (1000, 1000), (38, 1000)]]
        person = [930, 220, 40, 153]                 # centre y 296; edge at x 950 is y ~334
        self.assertFalse(av._is_outside_detect_zones("B", person))
        av._exclude_zones["B"] = [[(900, 200), (1000, 200), (1000, 300), (900, 300)]]
        self.assertFalse(av._is_excluded("B", person))  # covers the head, not the feet

    def test_frame_corner(self):
        self.assertEqual(av._frame_corner([787, 397, 213, 603]), "BR")
        self.assertEqual(av._frame_corner([0, 3, 300, 400]), "TL")
        self.assertIsNone(av._frame_corner([0, 300, 300, 400]))       # a side only
        self.assertIsNone(av._frame_corner([300, 0, 300, 1000]))      # top and bottom

    def test_lens_filter_drops_a_full_height_box_in_the_corner(self):
        # Live 2026-10-23 night: a web on Camera C's lens, [768, 3, 231, 949].
        web = [768, 3, 231, 949]
        with mock.patch.object(av, "_LENS_FILTER", set()):
            self.assertFalse(av._on_lens("B", "person", web))
        with mock.patch.object(av, "_LENS_FILTER", {"ALL"}):
            self.assertTrue(av._on_lens("B", "person", web))
            self.assertTrue(av._on_lens("B", "person", [2, 5, 300, 990]))     # left corner
            self.assertFalse(av._on_lens("B", "vehicle", web))
            self.assertFalse(av._on_lens("B", "person", [768, 3, 231, 700]))   # shorter
            self.assertFalse(av._on_lens("B", "person", [768, 40, 231, 930]))  # off top and bottom
            self.assertFalse(av._on_lens("B", "person", [400, 3, 231, 949]))   # off the sides
            # 2026-10-28 07:21: a strand from the bottom-right corner; a corner, not the top.
            strand = [787, 397, 213, 603]
            self.assertFalse(av._on_lens("B", "person", strand))                # under 900 tall
            with mock.patch.object(av, "LENS_MIN_HEIGHT", 550):
                self.assertTrue(av._on_lens("B", "person", strand))
                self.assertTrue(av._on_lens("B", "person", [0, 400, 200, 600]))   # bottom left
                self.assertFalse(av._on_lens("B", "person", [787, 300, 213, 600]))  # no corner
        with mock.patch.object(av, "_LENS_FILTER", {"OTHER"}):
            self.assertFalse(av._on_lens("B", "person", web))

    def test_ultrawide_letterboxed(self):
        av._stream_dims["U"] = (3200, 1200)
        x, y, w, h = av._to_protect("U", [0, 0, 1000, 1000])
        self.assertEqual((x, w), (0, 1000))
        self.assertAlmostEqual(h, 1000 * (16 / 9) / (3200 / 1200))

    def test_zone_check_in_canvas_space(self):
        # A zone on the canvas's middle third covers the whole portrait image.
        av._stream_dims["D"] = (1920, 2560)
        av._detect_zones["D"] = [[(290, 0), (710, 0), (710, 1000), (290, 1000)]]
        self.assertFalse(av._is_outside_detect_zones("D", [0, 400, 100, 100]))
        av._stream_dims["D"] = (2560, 1440)
        self.assertTrue(av._is_outside_detect_zones("D", [0, 400, 100, 100]))


class TestBestFrame(Base):
    def test_leave_uses_best_frame(self):
        av._stream_dims["D"] = (1920, 2560)
        best = {"jpeg": b"\xff\xd8best\xff\xd9", "coord": [500, 300, 100, 100],
                "score": 0.9, "wall": 1234}
        av._send_smart_detect_event(None, "D", edge_type="leave", object_type="face",
                                    coord=[0, 0, 10, 10], confidence=0.5, tracker_id=4,
                                    best=best)
        payload = self.sent[-1][1]
        snap = payload["smartDetectSnapshots"][0]
        self.assertEqual(snap["clockBestWall"], 1234)
        self.assertEqual(snap["smartDetectSnapshot"], "smartdetectsnap_zone_41234.jpg")
        self.assertEqual(snap["coord"], [round(v) for v in av._to_protect("D", best["coord"])])
        record = av._snapshot_filename_to_device["smartdetectsnap_zone_41234.jpg"]
        self.assertEqual(record["coord"], best["coord"])  # crop stays in image space
        self.assertEqual(record["jpeg"], best["jpeg"])
        fullfov = av._snapshot_filename_to_device[payload["smartDetectSnapshotFullFoV"]]
        self.assertEqual(fullfov["jpeg"], best["jpeg"])

    def test_single_vehicle_leave_carries_its_attributes_in_the_attr_map(self):
        # Live 10:32: only a multi leave's vehicles (attributes in trackerIDAttrMap) were
        # searchable by type and colour; one-vehicle events had them on the descriptor only.
        extra = {"attributes": {"vehicleType": {"val": "truck", "confidence": 70},
                                "color": {"val": "black", "confidence": 60}},
                 "name": "ABCD123"}
        av._send_smart_detect_event(None, "D", edge_type="leave", object_type="vehicle",
                                    coord=[0, 0, 10, 10], tracker_id=21, descriptor_extra=extra)
        entry = self.sent[-1][1]["trackerIDAttrMap"]["21"]
        self.assertEqual(entry["vehicleType"], {"val": "truck", "confidence": 70})
        self.assertEqual(entry["color"], {"val": "black", "confidence": 60})
        self.assertEqual(entry["matchedName"], "ABCD123")
        self.assertEqual(entry["objectType"], "vehicle")

    def test_single_leave_attr_map_without_extras_stays_minimal(self):
        av._send_smart_detect_event(None, "D", edge_type="leave", object_type="vehicle",
                                    coord=[0, 0, 10, 10], tracker_id=5)
        self.assertEqual(self.sent[-1][1]["trackerIDAttrMap"]["5"],
                         {"objectType": "vehicle", "zone": [int(av.SMART_DETECT_ZONE_ID)]})
        av._send_smart_detect_event(None, "D", edge_type="leave", object_type="person",
                                    coord=[0, 0, 10, 10], tracker_id=6,
                                    descriptor_extra={"matchedId": 186, "matchedName": "Alex",
                                                      "name": "not a plate"})
        self.assertEqual(self.sent[-1][1]["trackerIDAttrMap"]["6"],
                         {"objectType": "person", "zone": [int(av.SMART_DETECT_ZONE_ID)],
                          "matchedId": 186, "matchedName": "Alex"})

    def test_capture_keeps_best_score(self):
        tr = {}
        av._capture_best("D", tr, {"score": 0.8, "coord": [100, 200, 100, 300], "frame": (1, b"a")})
        av._capture_best("D", tr, {"score": 0.6, "coord": [120, 200, 100, 300], "frame": (2, b"b")})
        self.assertEqual((tr["best"]["jpeg"], tr["best"]["coord"]), (b"a", [100, 200, 100, 300]))
        av._capture_best("D", tr, {"score": 0.9, "coord": [120, 200, 100, 300], "frame": (2, b"b")})
        self.assertEqual(tr["best"]["jpeg"], b"b")

    def test_capture_prefers_a_larger_box(self):
        tr = {}
        av._capture_best("D", tr, {"score": 0.8, "coord": [100, 200, 100, 300], "frame": (1, b"a")})
        av._capture_best("D", tr, {"score": 0.78, "coord": [100, 200, 120, 330], "frame": (2, b"b")})
        self.assertEqual(tr["best"]["jpeg"], b"b")            # 32% larger, score about the same

    def test_capture_avoids_a_box_cut_off_by_the_edge(self):
        # Live 12:00 at Camera A: the top-scoring frame was a shoulder at the lens's edge.
        tr = {}
        av._capture_best("D", tr, {"score": 0.80, "coord": [300, 230, 240, 740], "frame": (1, b"whole")})
        av._capture_best("D", tr, {"score": 0.95, "coord": [2, 219, 637, 741], "frame": (2, b"shoulder")})
        self.assertEqual(tr["best"]["jpeg"], b"whole")
        tr = {}
        av._capture_best("D", tr, {"score": 0.95, "coord": [2, 219, 637, 741], "frame": (2, b"shoulder")})
        av._capture_best("D", tr, {"score": 0.70, "coord": [300, 230, 240, 740], "frame": (1, b"whole")})
        self.assertEqual(tr["best"]["jpeg"], b"whole")         # a whole frame replaces a cut-off one

    def test_jpeg_bytes_bounded(self):
        for i in range(av.BEST_FRAME_RECORDS_MAX + 5):
            av._record_snapshot_filename(f"f{i}.jpg", "D", kind="fullfov", jpeg=b"x")
        held = [r for r in av._snapshot_filename_to_device.values() if r.get("jpeg")]
        self.assertEqual(len(held), av.BEST_FRAME_RECORDS_MAX)


class TestCameraEvent(Base):
    """Protect keeps one smart-detect event per camera: tracks share it."""

    PERSON = {"objectType": "person", "score": 0.8, "coord": [400, 200, 200, 600]}
    FACE = {"objectType": "face", "score": 0.9, "coord": [450, 220, 60, 60]}
    CAR = {"objectType": "vehicle", "score": 0.7, "coord": [100, 500, 200, 100]}

    def setUp(self):
        super().setUp()
        from unittest import mock
        self.frames = []
        for target, value in (
                ("_classify_frame_detections",
                 lambda dev: [dict(d) for d in self.frames.pop(0) if d["objectType"] != "face"]),
                ("_enrich_detections",
                 lambda dev, dets: [dict(d) for d in self._faces]),
                ("_scene_changed", lambda dev: True),
                ("_read_snapshot", lambda dev: (1, b"\xff\xd8F\xff\xd9"))):
            patcher = mock.patch.object(av, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        av._motion_state.pop("D", None)
        self.addCleanup(av._motion_state.pop, "D", None)
        self.addCleanup(av._event_max_ms.pop, "D", None)
        # MIN_HITS has its own tests; the rest open on their first frame, as before.
        # TestCameraEventNorfair runs these again with Norfair.
        for name, value in (("LEAVE_FALLBACK_S", 3600), ("STARTUP_WINDOW_MS", -1),
                            ("MIN_HITS", 1), ("MIN_INITIALIZED", 0), ("TRACKER", "builtin")):
            patcher = mock.patch.object(av, name, value)  # start outside the startup window
            patcher.start()
            self.addCleanup(patcher.stop)

    def tick(self, *objs, after_ms=None):
        # Each tick analyses a new frame one report interval after the last (or after_ms).
        self.clock_ms = getattr(self, "clock_ms", 0) + (
            av.REPORT_INTERVAL_MS if after_ms is None else after_ms)
        av._analysed_frame_time["D"] = (1_760_000_000_000 + self.clock_ms, self.clock_ms)
        self._faces = [o for o in objs if o["objectType"] == "face"]
        self.frames.append(list(objs))
        av._motion_tick(None, "D")

    def assertOpened(self, types):
        """One enter, then only moving updates (the back-filled boxes and this frame)."""
        events = self.events()
        self.assertEqual(events[0], ("enter", types, []))
        self.assertTrue(all(e[:2] == ("moving", types) for e in events[1:]), events)

    def events(self):
        return [(p["edgeType"], sorted(d["objectType"] for d in p["descriptors"]),
                 sorted(x["smartDetectSnapshotType"] for x in p.get("smartDetectSnapshots", [])))
                for fn, p in self.sent if fn == "EventSmartDetect"]

    def face_tracks(self):
        return {tid: tr for tid, tr in av._motion_state["D"]["tracks"].items()
                if tr["objectType"] == "face"}

    def test_walking_up_face_stays_one_face_track(self):
        # Live 09:43 on Camera C: a face read once a second grew from 56 to 92 px and moved
        # down and across between reads, and became face tracks 5, 7 and 8.
        # face 20 -> 28 -> 36 units wide, its centre ~200 units further each read: past the
        # nearest-match radius (3x its size + 30), as live
        # (the person track itself stays one track: its boxes overlap read to read)
        steps = [([400, 100, 300, 800], [420, 150, 20, 20]),
                 ([380, 120, 330, 820], [600, 330, 28, 28]),
                 ([350, 140, 360, 840], [420, 560, 36, 36])]
        for person, face in steps:
            self.tick(dict(self.PERSON, coord=person), dict(self.FACE, coord=face))
        self.assertEqual(len(self.face_tracks()), 1)
        (fid, face), = self.face_tracks().items()
        person_tid = face["person_tid"]
        self.assertEqual(av._motion_state["D"]["tracks"][person_tid]["face_tid"], fid)

    def test_one_shrunken_box_does_not_split_a_person(self):
        # Live 16:47 on Camera C: walking, the box halved for a moment (55 high), and the full
        # box after it (120, over 2x) became a second person track.
        walk = [[470 + 10 * i, 110, 34, 124] for i in range(6)]   # ends at x 520
        half = [[524, 109, 34, 55]] * 3   # mostly inside the last box: the same person
        for coord in walk + half + [[511, 114, 36, 120], [513, 112, 34, 121]]:
            self.tick(dict(self.PERSON, coord=coord))
        people = [tid for tid, tr in av._motion_state["D"]["tracks"].items()
                  if tr["objectType"] == "person"]
        self.assertEqual(len(people), 1)

    def test_size_ref_is_the_median_of_recent_boxes(self):
        tr = {"coord": [526, 109, 34, 55], "sizes": [(32, 124), (31, 122), (34, 120), (34, 55)]}
        self.assertEqual(av._size_ref(tr)[2:], [33.0, 121.0])
        self.assertEqual(av._size_ref({"coord": [1, 2, 3, 4]}), [1, 2, 3, 4])   # too few yet

    def test_small_face_is_tracked_but_held_back_until_nameable(self):
        # Live 11:49 on Camera B: a 24 px face showed in Protect as a face no one could name.
        small = dict(self.FACE, small=True)
        for _ in range(3):
            self.tick(self.PERSON, small)
        (fid, face), = self.face_tracks().items()          # tracked...
        self.assertFalse(face["confirmed"])                 # ...but not reported
        self.assertNotIn("best", face)                      # no thumbnail from a small read
        sent = [d["objectType"] for fn, p in self.sent if fn == "EventSmartDetect"
                for d in p["descriptors"]]
        self.assertNotIn("face", sent)
        self.tick(self.PERSON, dict(self.FACE, coord=[460, 230, 70, 70]))   # big enough now
        face = self.face_tracks()[fid]                       # the same track
        self.assertTrue(face["confirmed"] and face["nameable"])
        self.assertEqual(face["best"]["coord"], [460, 230, 70, 70])
        self.assertEqual(face["first_shown_ms"], 1_760_000_000_000 + self.clock_ms)   # starts here
        self.tick(self.PERSON, small)                        # a later small read keeps it
        self.assertEqual(self.face_tracks()[fid]["best"]["coord"], [460, 230, 70, 70])

    def test_face_that_stays_small_is_never_reported(self):
        for _ in range(3):
            self.tick(self.PERSON, dict(self.FACE, small=True))
        for _ in range(40):
            self.tick()                                       # everyone leaves
        self.assertEqual(self.face_tracks(), {})
        sent = [d["objectType"] for fn, p in self.sent if fn == "EventSmartDetect"
                for d in p["descriptors"]]
        self.assertNotIn("face", sent)
        self.assertIn("person", sent)

    def test_face_trailing_its_person_keeps_its_track(self):
        # Live 10:40 on Camera C: the snapshot's face lagged a fast-moving person's live box,
        # fell outside it, and opened a second face track.
        self.tick(dict(self.PERSON, coord=[700, 400, 180, 500]), dict(self.FACE, coord=[750, 420, 50, 50]))
        self.tick(dict(self.PERSON, coord=[150, 400, 180, 500]), dict(self.FACE, coord=[600, 430, 50, 50]))
        self.assertEqual(len(self.face_tracks()), 1)

    def test_same_face_twice_in_one_read_is_one_track(self):
        face = dict(self.FACE, coord=[450, 220, 60, 60])
        self.tick(self.PERSON, face, dict(face, score=0.85))
        self.tick(self.PERSON, face, dict(face, score=0.85))
        self.assertEqual(len(self.face_tracks()), 1)

    def test_face_stays_open_while_its_person_looks_away(self):
        # Live 11:06 on Camera C: a person looked away for 5 s; the face track closed and their
        # face came back as a second face track.
        self.tick(self.PERSON, self.FACE)
        (first, _), = self.face_tracks().items()
        for _ in range(av.MOTION_MISS_TOLERANCE_TICKS + 5):
            self.tick(self.PERSON)
        self.tick(self.PERSON, dict(self.FACE, coord=[520, 320, 60, 60]))   # back elsewhere
        self.assertEqual(list(self.face_tracks()), [first])

    def test_face_closes_once_its_person_is_gone(self):
        self.tick(self.PERSON, self.FACE)
        for _ in range(2 * av.MOTION_MISS_TOLERANCE_TICKS + 2):
            self.tick()
        self.assertEqual(self.face_tracks(), {})

    def zone_left_half(self):
        av._detect_zones["D"] = [[(0, 0), (500, 0), (500, 1000), (0, 1000)]]
        av._detect_zone_types["D"] = [["person", "face"]]

    def person_at(self, x):
        return dict(self.PERSON, coord=[x, 200, 100, 600])   # feet at x + 50

    def test_person_tracked_outside_zones_joins_when_it_enters(self):
        self.zone_left_half()
        for x in (620, 580, 540, 500):                        # feet at 550..670: outside
            self.tick(self.person_at(x))
        self.assertEqual(self.events(), [])                   # tracked, not reported
        self.assertEqual(len(av._motion_state["D"]["tracks"]), 1)
        self.tick(self.person_at(420))                        # feet at 470: in the zone
        self.assertEqual(self.events()[0][0], "enter")
        self.assertEqual(len(av._motion_state["D"]["tracks"]), 1)   # the same track

    def test_person_stays_in_its_event_across_a_gap(self):
        # Live 11:38 on Camera B: the walk to the front door crossed a gap between zones.
        self.zone_left_half()
        self.tick(self.person_at(380))
        self.sent.clear()
        for x in (420, 460, 500, 540, 580):                   # feet 470 -> 630: out of every zone
            self.tick(self.person_at(x))
        self.assertEqual(len(av._motion_state["D"]["tracks"]), 1)
        self.assertTrue(self.events())
        self.assertTrue(all(e[0] == "moving" for e in self.events()))

    def test_events_name_the_zone_the_person_is_in(self):
        # Live 2026-10-24: a lens web whose feet fell in Camera C's lower zone (2) showed in
        # Protect under its upper zone (1): every detection was reported as zone 1.
        av._detect_zones["D"] = [[(0, 0), (1000, 0), (1000, 300), (0, 300)],        # 1 upper
                                 [(0, 300), (1000, 300), (1000, 1000), (0, 1000)]]  # 2 lower
        av._detect_zone_types["D"] = [["person"], ["person"]]
        av._detect_zone_ids["D"] = [1, 2]
        self.tick(self.person_at(400))                       # feet at y 800: the lower zone
        enter = next(p for fn, p in self.sent if fn == "EventSmartDetect" and p["edgeType"] == "enter")
        self.assertEqual(enter["descriptors"][0]["zones"], [2])
        self.assertEqual(list(enter["zonesStatus"]), ["2"])
        for _ in range(40):
            self.tick()
        leave = next(p for fn, p in self.sent if fn == "EventSmartDetect" and p["edgeType"] == "leave")
        self.assertEqual(list(leave["zonesStatus"]), ["2"])
        self.assertEqual([v["zone"] for v in leave["trackerIDAttrMap"].values()], [[2]])

    def test_a_person_that_never_leaves_a_corner_opens_no_event(self):
        # Live 2026-10-28 07:21 on Camera C: a web strand lit by the IR, from the bottom-right
        # corner and 600 tall, under the height the lens filter's box rule needs.
        from unittest import mock
        with mock.patch.object(av, "_LENS_FILTER", {"ALL"}):
            for i in range(30):
                self.tick(dict(self.PERSON, coord=[787 + i % 2, 397, 213, 603]))
            for _ in range(40):
                self.tick()
        self.assertEqual(self.events(), [])

    def test_a_person_from_a_corner_joins_once_they_leave_it(self):
        from unittest import mock
        with mock.patch.object(av, "_LENS_FILTER", {"ALL"}):
            for x in (0, 0, 0):                                  # bottom-left corner
                self.tick(dict(self.PERSON, coord=[x, 400, 200, 600]))
            self.assertEqual(self.events(), [])
            for x in range(40, 400, 40):                         # walks out of it
                self.tick(dict(self.PERSON, coord=[x, 380, 200, 600]))
        self.assertEqual(self.events()[0][:2], ("enter", ["person"]))
        tracks = av._motion_state["D"]["tracks"]
        self.assertEqual(len(tracks), 1)                         # the same track throughout

    def test_corners_are_ordinary_without_the_lens_filter(self):
        from unittest import mock
        with mock.patch.object(av, "_LENS_FILTER", set()):
            self.tick(dict(self.PERSON, coord=[787, 397, 213, 603]))
        self.assertEqual(self.events()[0][:2], ("enter", ["person"]))

    def test_leave_names_every_zone_the_person_was_in(self):
        # Live 10:10 on Camera C: a walk from the lower zone into the upper one closed as zone 1 only.
        av._detect_zones["D"] = [[(0, 0), (1000, 0), (1000, 500), (0, 500)],        # 1 upper
                                 [(0, 500), (1000, 500), (1000, 1000), (0, 1000)]]  # 2 lower
        av._detect_zone_types["D"] = [["person"], ["person"]]
        av._detect_zone_ids["D"] = [1, 2]
        for y in range(300, -200, -40):   # feet from y 900 (zone 2) to 460 (zone 1)
            self.tick(dict(self.PERSON, coord=[400, y, 100, 600]))
        moves = [p["descriptors"][0]["zones"] for fn, p in self.sent
                 if fn == "EventSmartDetect" and p["edgeType"] in ("enter", "moving")]
        self.assertEqual((moves[0], moves[-1]), ([2], [1]))
        for _ in range(40):
            self.tick()
        leave = next(p for fn, p in self.sent if fn == "EventSmartDetect" and p["edgeType"] == "leave")
        self.assertEqual(list(leave["zonesStatus"]), ["2", "1"])
        self.assertEqual([v["zone"] for v in leave["trackerIDAttrMap"].values()], [[2, 1]])

    def test_without_zones_events_name_zone_1(self):
        self.tick(self.person_at(400))
        enter = next(p for fn, p in self.sent if fn == "EventSmartDetect" and p["edgeType"] == "enter")
        self.assertEqual(enter["descriptors"][0]["zones"], [1])
        self.assertEqual(list(enter["zonesStatus"]), ["1"])

    def test_person_never_in_a_zone_is_never_reported(self):
        self.zone_left_half()
        for x in (900, 850, 800, 750):
            self.tick(self.person_at(x))
        self.assertEqual(self.sent, [])

    def test_clipped_box_starts_no_track(self):
        self.tick(dict(self.CAR, clipped=True))
        self.tick(dict(self.CAR, clipped=True))
        self.assertEqual(av._motion_state["D"]["tracks"], {})

    def test_clipped_box_does_not_move_a_moving_track(self):
        self.tick(self.CAR)
        self.tick(dict(self.CAR, coord=[120, 500, 200, 100]))
        self.tick(dict(self.CAR, coord=[300, 400, 150, 300], clipped=True))
        (tid, tr), = av._motion_state["D"]["tracks"].items()
        self.assertEqual(tr["coord"], [120, 500, 200, 100])
        self.assertEqual(tr["miss_count"], 1)

    def test_two_people_keep_their_own_faces(self):
        a, b = dict(self.PERSON, coord=[100, 200, 200, 600]), dict(self.PERSON, coord=[600, 200, 200, 600])
        fa, fb = dict(self.FACE, coord=[150, 220, 60, 60]), dict(self.FACE, coord=[650, 220, 60, 60])
        self.tick(a, b, fa, fb)
        self.tick(a, b, dict(fa, coord=[160, 230, 60, 60]), dict(fb, coord=[640, 230, 60, 60]))
        faces = self.face_tracks()
        self.assertEqual(len(faces), 2)
        self.assertEqual(len({f["person_tid"] for f in faces.values()}), 2)

    # Live 13:33:38 on Camera B: two people crossing; her face sat low in my smaller box.
    ME = {"objectType": "person", "score": 0.9, "coord": [517, 235, 91, 414]}
    HER = {"objectType": "person", "score": 0.9, "coord": [447, 511, 136, 467]}
    HER_FACE = {"objectType": "face", "score": 0.9, "coord": [528, 512, 35, 86]}

    def person_of_face(self):
        (_, face), = self.face_tracks().items()
        return av._motion_state["D"]["tracks"][face["person_tid"]]["coord"]

    def test_face_goes_to_the_person_whose_crop_found_it(self):
        self.tick(self.ME, self.HER, dict(self.HER_FACE, parent_coord=self.HER["coord"]))
        self.assertEqual(self.person_of_face(), self.HER["coord"])

    def test_face_goes_to_its_finder_after_the_finder_has_moved(self):
        # 13:33 clip: read from a snapshot 0.8 s old, the face's centre was 2 px outside my
        # live box, which overlapped my box at the read by IoU 0.22; it opened a second face.
        tracks = {3: {"objectType": "person", "coord": [617, 260, 117, 408]},
                  7: {"objectType": "person", "coord": [467, 511, 108, 487]}}
        self.assertEqual(av._face_owner(tracks, [725, 297, 22, 53],
                                        parent_coord=[688, 293, 119, 388]), 3)
        # ...but with two people overlapping the finder's box, neither is taken on a weak overlap
        tracks[7]["coord"] = [780, 300, 100, 400]
        self.assertIsNone(av._face_owner(tracks, [900, 297, 22, 53],
                                         parent_coord=[688, 293, 119, 388]))

    def test_face_without_its_finder_goes_to_the_head_it_is_at(self):
        self.tick(self.ME, self.HER, self.HER_FACE)
        self.assertEqual(self.person_of_face(), self.HER["coord"])

    def test_face_found_twice_keeps_the_read_at_its_finders_head(self):
        low = dict(self.HER_FACE, score=0.95, parent_coord=self.ME["coord"])
        high = dict(self.HER_FACE, coord=[529, 513, 35, 86], parent_coord=self.HER["coord"])
        self.tick(self.ME, self.HER, low, high)
        self.assertEqual(self.person_of_face(), self.HER["coord"])

    def test_named_person_names_its_face(self):
        self.tick(self.PERSON, self.FACE)
        tracks = av._motion_state["D"]["tracks"]
        pid = next(t for t, tr in tracks.items() if tr["objectType"] == "person")
        tracks[pid].setdefault("extra", {}).update({"matchedId": 178, "matchedName": "Alex"})
        self.tick(self.PERSON, self.FACE)
        (_, face), = self.face_tracks().items()
        self.assertEqual((face["extra"]["matchedId"], face["extra"]["matchedName"]), (178, "Alex"))

    def test_face_embedding_held_until_decided(self):
        emb = [0.1] * 128
        face = dict(self.FACE, scored={"faceEmbed": (emb, 0.9)}, fresh_read=True)
        self.tick(self.PERSON, face)
        def sent_embeds():
            return [("faceEmbed" in d) for fn, p in self.sent if fn == "EventSmartDetect"
                    for d in p["descriptors"] if d["objectType"] == "face"]
        self.assertEqual(sent_embeds(), [False])       # one read: not decided yet
        self.sent.clear()
        self.tick(self.PERSON, face)                    # a second fresh read
        self.assertEqual(sent_embeds(), [True])
        self.sent.clear()
        stale = dict(face, fresh_read=False)            # reused snapshot: not a new read
        tracks = av._motion_state["D"]["tracks"]
        (fid, ftr), = self.face_tracks().items()
        self.assertEqual(ftr["embed_reads"], 2)
        self.tick(self.PERSON, stale)
        self.assertEqual(ftr["embed_reads"], 2)

    def test_face_that_matched_once_waits_for_its_name(self):
        # Live 11:13: read 1 didn't match, read 2 was the first match; the embedding went out
        # unnamed and Protect showed an unknown face.
        tr = {"objectType": "face", "score": 0.9, "coord": [0, 0, 1, 1], "first_shown_ms": 0,
              "extra": {"faceEmbed": [0.1]}, "embed_reads": 2}
        self.assertIn("faceEmbed", av._track_args(1, tr)["descriptor_extra"])   # never matched
        tr["match_hits"] = {(186, "Alex"): 1}
        self.assertNotIn("faceEmbed", av._track_args(1, tr)["descriptor_extra"])
        tr["embed_reads"] = av.FACE_EMBED_HOLD_READS
        self.assertIn("faceEmbed", av._track_args(1, tr)["descriptor_extra"])   # waited long enough
        tr["embed_reads"], tr["extra"]["matchedId"] = 2, 186
        self.assertIn("faceEmbed", av._track_args(1, tr)["descriptor_extra"])   # named

    def test_named_face_sends_its_embedding_at_once(self):
        tr = {"objectType": "face", "score": 0.9, "coord": [0, 0, 1, 1], "first_shown_ms": 0,
              "extra": {"faceEmbed": [0.1], "matchedId": 5}}
        self.assertIn("faceEmbed", av._track_args(1, tr)["descriptor_extra"])
        tr["extra"].pop("matchedId")
        self.assertNotIn("faceEmbed", av._track_args(1, tr)["descriptor_extra"])
        self.assertIn("faceEmbed", av._track_args(1, tr, hold_embed=False)["descriptor_extra"])

    def test_named_face_leave_carries_match_not_embedding(self):
        # Live 11:18: Protect clustered the leave's faceEmbed into unknown group 189, which
        # replaced Alex's group 186.
        named = {"objectType": "face", "score": 0.9, "coord": [0, 0, 1, 1], "first_shown_ms": 0,
                 "extra": {"faceEmbed": [0.1], "matchedId": 186, "matchedName": "Alex"}}
        leave = av._track_args(3, named, hold_embed=False)["descriptor_extra"]
        self.assertNotIn("faceEmbed", leave)
        self.assertEqual(leave["matchedId"], 186)
        entry = av._tracker_attrs(3, named, {})
        self.assertNotIn("faceEmbed", entry)
        self.assertEqual((entry["matchedId"], entry["matchedName"]), (186, "Alex"))
        stranger = {"objectType": "face", "extra": {"faceEmbed": [0.1]}}
        self.assertIn("faceEmbed", av._tracker_attrs(4, stranger, {}))

    def test_person_points_at_its_own_face_track(self):
        person = {"objectType": "person", "coord": [0, 0, 500, 1000], "face_tid": 8}
        old = {"objectType": "face", "coord": [100, 100, 50, 50]}
        own = {"objectType": "face", "coord": [300, 100, 50, 50]}
        self.assertEqual(av._associate_faces([(4, person), (5, old), (8, own)]), {4: 8})

    def test_person_and_face_one_event(self):
        self.tick(self.PERSON, self.FACE)
        self.tick(self.PERSON, self.FACE)
        self.sent.clear()
        for _ in range(av.MOTION_MISS_TOLERANCE_TICKS):
            self.tick()
        ev = self.events()
        # Multi-participant leave: no descriptors, a snapshot per participant.
        self.assertEqual(ev[-1], ("leave", [], ["face", "person"]))
        self.assertEqual([e[0] for e in ev[:-1]], ["moving"] * (len(ev) - 1))
        self.assertIsNone(av._motion_state["D"].get("event"))

    def test_first_tick_is_one_enter(self):
        self.tick(self.PERSON, self.FACE)
        self.assertEqual(self.events(), [("enter", ["face", "person"], [])])
        ids = {p["eventId"] for _, p in self.sent}
        self.assertEqual(len(ids), 1)

    def test_time_limit_closes_and_parks_stationary(self):
        av._event_max_ms["D"] = 0
        self.tick(self.CAR)          # opens
        self.tick(self.CAR)          # time limit: leave, car goes stationary
        self.assertEqual(self.events()[-1], ("leave", ["vehicle"], ["vehicle"]))
        self.sent.clear()
        self.tick(self.CAR)          # still parked: quiet
        self.assertEqual(self.sent, [])
        moved = dict(self.CAR, coord=[300, 500, 200, 100])
        av._event_max_ms["D"] = 10 ** 9
        self.tick(moved)             # drove off: a new event
        self.assertEqual(self.events(), [("enter", ["vehicle"], [])])

    def test_visitor_beside_parked_car(self):
        av._event_max_ms["D"] = 0
        self.tick(self.CAR)
        self.tick(self.CAR)          # car now stationary
        av._event_max_ms["D"] = 10 ** 9
        self.sent.clear()
        self.tick(self.CAR, self.PERSON, self.FACE)
        self.assertEqual(self.events(), [("enter", ["face", "person"], [])])
        for _ in range(av.MOTION_MISS_TOLERANCE_TICKS):
            self.tick(self.CAR)
        self.assertEqual(self.events()[-1], ("leave", [], ["face", "person"]))

    def _clock(self, start=0):
        clock = [start]
        patcher = mock.patch.object(av, "_frame_wall_ms", lambda dev: clock[0])
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(av._event_start_ms.pop, "D", None)
        return clock

    def test_confirmed_and_parked_in_one_tick_opens_nothing(self):
        # ...then it confirmed and parked in the same tick: an event opened with no
        # active track, and every later tick crashed closing it.
        clock = self._clock()
        av._event_start_ms["D"] = (av.STILL_TICKS + 5) * 100
        for _ in range(av.STILL_TICKS + 10):
            self.tick(self.CAR)
            clock[0] += 100
        self.tick()
        self.assertEqual(self.events(), [])
        self.assertIsNone(av._motion_state["D"].get("event"))
        self.assertTrue(av._motion_state["D"]["tracks"][1].get("stationary_coord"))

    def test_event_with_no_tracks_closes_quietly(self):
        self.tick()
        av._motion_state["D"]["event"] = {"id": 1, "started_ms": 0, "finished": []}
        self.tick()
        self.assertIsNone(av._motion_state["D"].get("event"))
        self.assertEqual(self.events(), [])

    def _park_car(self, car):
        av._event_max_ms["D"] = 0
        self.tick(car)
        self.tick(car)                   # time limit: leave, car parks
        av._event_max_ms["D"] = 10 ** 9
        self.sent.clear()

    def _scene(self, car_at=None):
        import numpy as _np
        rng = _np.random.default_rng(3)
        frame = _np.full((640, 640, 3), 90, _np.uint8)
        if car_at is not None:  # a textured car at image coords x 388-532, y 437-608 (0-1000)
            x1 = int(car_at * 0.64)
            frame[280:389, x1:x1 + 92] = (rng.random((109, 92, 1)) * 200 + 30).astype(_np.uint8)
        av._latest_frame["D"] = frame
        self.addCleanup(av._latest_frame.pop, "D", None)

    @unittest.skipUnless(_CV2, "OpenCV not installed (stationary.py)")
    def test_jittering_boxes_dont_move_a_parked_car_its_pixels_stay(self):
        # Live 2026-10-17 12:25 (Camera A): boxes on a parked car shrank onto its lower half.
        self._scene(car_at=388)
        car = dict(self.CAR, coord=[388, 437, 144, 171])
        self._park_car(car)
        self.tick(car)                                       # anchors the parked spot
        self.assertIn("pixel_anchor", av._motion_state["D"]["tracks"][1])
        for box in ([385, 437, 99, 171], [385, 468, 87, 139], [384, 510, 71, 97]):
            for _ in range(av.STATIONARY_RESUME_TICKS + 1):
                self.tick(dict(car, coord=box))
        self.assertEqual(self.events(), [])
        self.assertTrue(av._motion_state["D"]["tracks"][1].get("stationary_coord"))

    def test_parked_car_whose_spot_changed_rejoins(self):
        self._scene(car_at=388)
        car = dict(self.CAR, coord=[388, 437, 144, 171])
        self._park_car(car)
        self.tick(car)
        self._scene(car_at=450)                              # the car really rolled forward
        for _ in range(av.STATIONARY_RESUME_TICKS + 2):
            self.tick(dict(car, coord=[450, 437, 144, 171]))
        self.assertEqual(self.events()[0], ("enter", ["vehicle"], []))

    def test_box_inside_a_parked_car_is_the_same_car(self):
        car = dict(self.CAR, coord=[388, 437, 144, 171])
        self._park_car(car)
        self.tick(car, dict(car, score=0.6, coord=[390, 515, 69, 92]))
        self.assertEqual(self.events(), [])
        self.assertEqual(list(av._motion_state["D"]["tracks"]), [1])

    def test_strong_move_still_wakes_a_parked_car(self):
        car = dict(self.CAR, coord=[388, 437, 144, 171])
        self._park_car(car)
        self.tick(dict(car, coord=[700, 437, 144, 171]))
        self.assertEqual(self.events(), [("enter", ["vehicle"], [])])

    def test_stump_scoring_about_half_never_opens_an_event(self):
        # Live 2026-10-17 13:33, 13:54, 14:31 (Camera C): a tree stump scored "animal"
        # 0.30-0.73, median ~0.55.
        stump = {"objectType": "animal", "coord": [788, 571, 98, 203]}
        for score in [0.5, 0.55, 0.73, 0.52, 0.6, 0.51, 0.58, 0.5, 0.64, 0.55] * 5:
            self.tick(dict(stump, score=score))
        self.assertEqual(self.events(), [])

    def test_median_counts_missed_frames_as_zero(self):
        car = dict(self.CAR, score=0.9)
        self.tick(dict(car, score=0.6))
        for _ in range(6):                     # seen every other frame: median 0.45 at best
            self.tick()
            self.tick(car)
        self.assertEqual(self.events(), [])

    def test_strong_object_opens_at_once(self):
        self.tick(dict(self.PERSON, score=0.85))
        self.assertEqual(self.events(), [("enter", ["person"], [])])

    def test_one_frame_detection_never_opens_an_event(self):
        # Live (Camera B replay): a car scoring 0.9 on a single region crop became an event.
        with mock.patch.object(av, "MIN_HITS", 2):
            self.tick(dict(self.CAR, score=0.9))
            for _ in range(av.MOTION_MISS_TOLERANCE_TICKS):
                self.tick(after_ms=int(av.DETECT_INTERVAL_S * 1000))
        self.assertEqual(self.events(), [])

    def test_second_hit_confirms(self):
        with mock.patch.object(av, "MIN_HITS", 2):
            self.tick(dict(self.CAR, score=0.9))
            self.assertEqual(self.events(), [])
            self.tick(dict(self.CAR, score=0.85, coord=[200, 500, 200, 100]))
        self.assertOpened(["vehicle"])

    def test_median_reaching_threshold_confirms(self):
        self.tick(dict(self.PERSON, score=0.6))
        self.tick(dict(self.PERSON, score=0.75))
        self.assertEqual(self.events(), [])    # median 0.675
        self.tick(dict(self.PERSON, score=0.9))  # median 0.75
        self.assertEqual(self.events()[0], ("enter", ["person"], []))

    def test_true_thresh_per_type(self):
        with mock.patch.object(av, "_TRUE_THRESH", {"animal": 0.9}):
            self.tick({"objectType": "animal", "score": 0.85, "coord": [300, 600, 80, 60]})
            self.assertEqual(self.events(), [])
            self.tick(dict(self.PERSON, score=0.85))
            self.assertEqual(self.events(), [("enter", ["person"], [])])

    def test_parse_true_thresh(self):
        self.assertEqual(av._parse_true_thresh(None), (0.7, {}))
        self.assertEqual(av._parse_true_thresh("0.6, animal=0.8"), (0.6, {"animal": 0.8}))
        self.assertEqual(av._parse_true_thresh("x,person=0.75"), (0.7, {"person": 0.75}))

    def test_sedan_seen_twice_makes_no_event_under_min_initialized(self):
        # Live 14:51 (Camera B): a sedan crossing the far road was detected on 2 frames
        # (0.85, 0.78). With Frigate's min_initialized (0.5 s of detections) that is too few,
        # as in Frigate; region crops now see far-road cars on more frames (9 at 17:20).
        for name, value in (("MIN_HITS", 2), ("MIN_INITIALIZED", 5)):   # the live rules
            patcher = mock.patch.object(av, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        clock = self._clock()
        av._event_start_ms["D"] = 1000
        sedan = dict(self.CAR, coord=[160, 49, 103, 76])
        self.tick(dict(sedan, score=0.85))
        clock[0] += 220
        self.tick(dict(sedan, score=0.78, coord=[126, 58, 105, 80]))
        for _ in range(10):
            clock[0] += 100
            self.tick()
        self.assertEqual(self.events(), [])

    def _live_init(self):
        for name, value in (("MIN_HITS", 2), ("MIN_INITIALIZED", 5)):
            patcher = mock.patch.object(av, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_flicker_never_initializes(self):
        # Live 19:17 on Camera C: a corner box at night, seen on and off, made 4 person events.
        self._live_init()
        av._event_start_ms["D"] = 0
        box = dict(self.PERSON, coord=[884, 667, 115, 331], score=0.8)
        for seen in (1, 1, 0, 1, 0, 0, 1, 1, 0, 1, 0, 1, 0, 0, 1):
            self.tick(box if seen else None, after_ms=100) if seen else self.tick(after_ms=100)
        self.assertEqual(self.events(), [])

    def test_min_initialized_per_camera(self):
        self.assertEqual(av._parse_min_initialized("5,02000000000B=3"), (5, {"02000000000B": 3}))
        self.assertEqual(av._parse_min_initialized("02000000000B=3")[1], {"02000000000B": 3})
        self.assertEqual(av._parse_min_initialized("x,D=y")[1], {})

    def test_road_car_crossing_in_under_a_second_counts(self):
        # Live 08:48 on Camera B: a car on the road strip for ~0.7 s never initialised at 5
        # (counter 1, 2, 3, 2, 3, 4 with one miss); at 3 it does.
        self._live_init()
        av._event_start_ms["D"] = 0
        with mock.patch.object(av, "_MIN_INITIALIZED_PER", {"D": 3}):
            for x in (260, 230, 200, None, 170, 140):   # 6 frames, one missed
                if x is None:
                    self.tick(after_ms=100)
                else:
                    self.tick(dict(self.CAR, coord=[x, 40, 90, 80]), after_ms=100)
        self.assertEqual(self.events()[0][0], "enter")

    def test_same_crossing_does_not_count_at_the_default(self):
        self._live_init()
        av._event_start_ms["D"] = 0
        for x in (260, 230, 200, None, 170, 140):
            if x is None:
                self.tick(after_ms=100)
            else:
                self.tick(dict(self.CAR, coord=[x, 40, 90, 80]), after_ms=100)
        self.assertEqual(self.events(), [])

    def test_steady_object_initializes_after_half_a_second(self):
        self._live_init()
        av._event_start_ms["D"] = 0
        for i in range(5):
            self.tick(self.PERSON, after_ms=100)
        self.assertEqual(self.events(), [])            # counter 5: not above 5 yet
        self.tick(self.PERSON, after_ms=100)
        self.assertEqual(self.events()[0][0], "enter")

    def test_walking_person_still_waits_for_confirm_ms(self):
        clock = self._clock()
        av._event_start_ms["D"] = 1000
        for x in (400, 450, 500):
            self.tick(dict(self.PERSON, coord=[x, 200, 200, 600]))
            clock[0] += 400
        self.assertEqual(self.events(), [])
        self.tick(dict(self.PERSON, coord=[550, 200, 200, 600]))
        self.assertOpened(["person"])

    def test_vehicle_on_its_first_box_waits_to_confirm(self):
        # Live 12:59 on Camera B: a parked car's mirror, found on a zoomed-in crop, joined
        # the passing car's event as a second vehicle.
        clock = self._clock()
        av._event_start_ms["D"] = 1000
        for _ in range(6):
            self.tick(self.CAR)
            clock[0] += 400
        self.assertEqual(self.events(), [])
        self.tick(dict(self.CAR, coord=[200, 500, 200, 100]))
        self.assertOpened(["vehicle"])

    def test_passing_vehicle_confirms_on_time(self):
        clock = self._clock()
        av._event_start_ms["D"] = 1000
        for x in (100, 200, 300, 400):
            self.tick(dict(self.CAR, coord=[x, 500, 200, 100]))
            clock[0] += 400
        self.assertOpened(["vehicle"])

    def test_face_confirms_with_its_person(self):
        # Live 13:54:51: a face found once was dropped before it could confirm on its own.
        clock = self._clock()
        av._event_start_ms["D"] = 1000
        self.tick(self.PERSON, self.FACE)
        for _ in range(5):
            clock[0] += 300
            self.tick(self.PERSON)                    # the face isn't re-found every frame
        self.assertOpened(["person"])
        self.tick(self.PERSON, self.FACE)
        tracks = av._motion_state["D"]["tracks"]
        self.assertTrue(all(tr.get("confirmed") for tr in tracks.values()))

    def test_face_alone_never_confirms(self):
        clock = self._clock()
        av._event_start_ms["D"] = 1000
        for _ in range(5):
            self.tick(self.FACE)
            clock[0] += 500
        self.assertEqual(self.events(), [])

    def test_empty_motion_logged_once_a_minute(self):
        # Live 2026-10-17 (Camera C): leaves and shadows logged this ~40 times a minute.
        av._empty_motion.pop("D", None)
        self.addCleanup(av._empty_motion.pop, "D", None)
        clock = [500.0]
        with mock.patch.object(av.time, "monotonic", lambda: clock[0]), \
                self.assertLogs(av.log, level="INFO") as logs:
            for t in (0, 10, 20, 59, 61, 70):
                clock[0] = 500.0 + t
                av._log_empty_motion("D")
        lines = [r.getMessage() for r in logs.records if "scene changed" in r.getMessage()]
        self.assertEqual(len(lines), 2)
        self.assertIn("4 such frame(s)", lines[1])   # 10, 20, 59 and 61

    def test_dropped_detection_logged_once_a_minute(self):
        # Live 18:10: Camera A's parked cars, outside its person/animal zone, logged a
        # line several times a second.
        for key in [k for k in av._dropped if k[0] == "D"]:
            av._dropped.pop(key)
        clock = [500.0]
        car = {"objectType": "vehicle", "score": 0.77}
        with mock.patch.object(av.time, "monotonic", lambda: clock[0]), \
                self.assertLogs(av.log, level="INFO") as logs:
            for t in (0, 0.1, 0.2, 30, 61):
                clock[0] = 500.0 + t
                av._log_dropped("D", car, "outside all configured detection zones")
            av._log_dropped("D", dict(car, objectType="person"),
                            "outside all configured detection zones")
        lines = [r.getMessage() for r in logs.records if "detection outside" in r.getMessage()]
        self.assertEqual(len(lines), 3)              # vehicle at 0 and 61, person once
        self.assertIn("4 such detection(s)", lines[1])

    def test_empty_static_frame_is_not_logged(self):
        with mock.patch.object(av, "MOTION_GATE", False), \
                mock.patch.object(av, "_scene_changed", lambda dev: False), \
                self.assertNoLogs(av.log, level="INFO"):
            self.tick()

    def test_parked_car_flickering_comes_back_quietly(self):
        # Live 14:57: a car with its hatch open behind people unloading it kept dropping
        # out; each re-detection joined the next event as another vehicle.
        hatch = dict(self.CAR, coord=[180, 3, 470, 680])
        self.tick(self.PERSON, hatch)
        for _ in range(av.MOTION_MISS_TOLERANCE_TICKS):
            self.tick()                                      # all gone: event closes
        self.assertEqual(self.events()[-1][0], "leave")
        self.sent.clear()
        self.tick(self.PERSON, dict(hatch, coord=[177, 1, 472, 743]))
        self.assertEqual(self.events(), [("enter", ["person"], [])])
        self.tick(self.PERSON, dict(hatch, coord=[177, 171, 359, 511]))
        self.assertEqual(self.events()[-1], ("moving", ["person"], []))

    def test_track_dropping_out_rejoins_its_event(self):
        other = dict(self.PERSON, coord=[700, 200, 150, 500])
        self.tick(self.PERSON, other)
        for _ in range(av.MOTION_MISS_TOLERANCE_TICKS):
            self.tick(other)                                 # first person's track closes
        ids = set(av._motion_state["D"]["tracks"])
        self.tick(self.PERSON, other)                        # ...and comes back
        self.assertEqual(len(av._motion_state["D"]["tracks"]), 2)
        self.assertEqual(set(av._motion_state["D"]["tracks"]) - ids, {1})
        self.assertNotIn(1, [m[0] for m in av._motion_state["D"]["event"]["finished"]])

    def test_moving_car_that_left_is_new_next_time(self):
        self.tick(self.CAR)
        self.tick(dict(self.CAR, coord=[200, 500, 200, 100]))   # drove along
        for _ in range(av.MOTION_MISS_TOLERANCE_TICKS):
            self.tick()
        self.sent.clear()
        self.tick(dict(self.CAR, coord=[200, 500, 200, 100]))
        self.assertEqual(self.events(), [("enter", ["vehicle"], [])])

    def test_resume_forgets_after_resume_s(self):
        hatch = dict(self.CAR, coord=[180, 3, 470, 680])
        self.tick(self.PERSON, hatch)
        for _ in range(av.MOTION_MISS_TOLERANCE_TICKS):
            self.tick()
        dev = av._motion_state["D"]
        dev["recent"] = [(tid, tr, closed - av.RESUME_S * 1000 - 1)
                         for tid, tr, closed in dev["recent"]]
        self.sent.clear()
        self.tick(hatch)
        self.assertEqual(self.events(), [("enter", ["vehicle"], [])])

    def test_occlusion_does_not_wake_parked_car(self):
        # Live 10:27:24: someone walked in front of a parked car; its box narrowed
        # (centre shifted) though the car never moved.
        parked = [391, 434, 146, 169]
        tr = {"coord": parked, "stationary_coord": list(parked)}
        for _ in range(av.STATIONARY_RESUME_TICKS * 3):
            tr["coord"] = [391, 437, 88, 165]
            self.assertFalse(av._moved_since(tr))

    def test_driving_off_wakes_immediately(self):
        tr = {"coord": [700, 434, 146, 169], "stationary_coord": [391, 434, 146, 169]}
        self.assertTrue(av._moved_since(tr))

    def test_sustained_same_size_shift_wakes(self):
        parked = [391, 434, 146, 169]
        tr = {"coord": [451, 434, 146, 169], "stationary_coord": list(parked)}  # IoU > 0.3
        results = [av._moved_since(tr) for _ in range(av.STATIONARY_RESUME_TICKS)]
        self.assertEqual(results, [False] * (av.STATIONARY_RESUME_TICKS - 1) + [True])

    def test_brief_shift_resets(self):
        parked = [391, 434, 146, 169]
        tr = {"coord": [451, 434, 146, 169], "stationary_coord": list(parked)}
        for _ in range(av.STATIONARY_RESUME_TICKS - 1):
            self.assertFalse(av._moved_since(tr))
        tr["coord"] = list(parked)  # back in place
        self.assertFalse(av._moved_since(tr))
        tr["coord"] = [451, 434, 146, 169]
        self.assertFalse(av._moved_since(tr))  # count restarted

    def test_visitor_in_front_of_parked_car_closes_promptly(self):
        av._event_max_ms["D"] = 0
        car = dict(self.CAR, coord=[391, 434, 146, 169])
        self.tick(car)
        self.tick(car)                       # parked
        av._event_max_ms["D"] = 10 ** 9
        self.sent.clear()
        occluded = dict(car, coord=[391, 437, 88, 165])
        for _ in range(5):
            self.tick(occluded, self.PERSON, self.FACE)
        for _ in range(av.MOTION_MISS_TOLERANCE_TICKS):
            self.tick(car)
        ev = self.events()
        self.assertEqual(ev[0][0], "enter")
        self.assertNotIn("vehicle", ev[0][1])   # the car stayed parked
        self.assertEqual(ev[-1], ("leave", [], ["face", "person"]))  # closed without the time limit

    def test_single_participant_keeps_proven_leave(self):
        av._event_max_ms["D"] = 0
        self.tick(self.CAR)
        self.tick(self.CAR)
        self.assertEqual(self.events()[-1], ("leave", ["vehicle"], ["vehicle"]))

    def test_present_at_startup_is_parked(self):
        from unittest import mock
        with mock.patch.object(av, "STARTUP_WINDOW_MS", 10 ** 9):
            self.tick(self.CAR)
            self.tick(self.CAR)
        self.assertEqual(self.sent, [])   # the parked car opens no event
        self.assertTrue(av._motion_state["D"]["tracks"][1].get("stationary_coord"))
        self.tick(dict(self.CAR, coord=[600, 500, 200, 100]))  # drives off
        self.assertEqual(self.events(), [("enter", ["vehicle"], [])])

    def test_stream_restart_starts_the_startup_window_again(self):
        # Live 11:47 on Camera B: the stream dropped just after start and the parked car,
        # first seen once frames came back, opened an event.
        from unittest import mock
        with mock.patch.object(av, "STARTUP_WINDOW_MS", 15000):
            self.tick()
            self.tick(after_ms=20000)          # past the window: a new car would open an event
            av._stop_stream("D")
            self.tick(self.CAR, after_ms=1000)  # the new stream's first frames
            self.tick(self.CAR)
        self.assertEqual(self.sent, [])
        self.assertTrue(av._motion_state["D"]["tracks"][1].get("stationary_coord"))

    def test_ticks_without_frames_do_not_use_up_the_window(self):
        # Live 17:02 on Camera B: ticks ran through a 10 s reconnect with no frame.
        from unittest import mock
        with mock.patch.object(av, "STARTUP_WINDOW_MS", 15000):
            self.tick()
            self.tick(after_ms=20000)
            av._stop_stream("D")
            with mock.patch.object(av, "_frame_wall_ms", lambda dev: 1_760_000_000_000 + self.clock_ms):
                for _ in range(3):
                    self.clock_ms += 10000
                    self.frames.append([])
                    av._motion_tick(None, "D")   # no analysed frame yet
            self.assertIsNone(av._motion_state["D"]["started_ms"])
            self.tick(self.CAR, after_ms=1000)
            self.tick(self.CAR)
        self.assertEqual(self.sent, [])
        self.assertTrue(av._motion_state["D"]["tracks"][1].get("stationary_coord"))

    def test_startup_look_runs_on_a_still_scene(self):
        # Live 17:05 on Camera A: no motion at dusk, so the gate skipped the startup look.
        from unittest import mock
        with mock.patch.object(av, "STARTUP_WINDOW_MS", 15000), \
                mock.patch.object(av, "MOTION_GATE", True), \
                mock.patch.object(av, "_scene_changed", lambda dev: False):
            self.tick(self.CAR)
            self.tick(self.CAR)
            self.assertTrue(av._motion_state["D"]["tracks"][1].get("stationary_coord"))
            av._motion_state["D"]["tracks"].clear()
            self.tick(self.CAR, after_ms=20000)   # after the window a still scene is gated
            self.assertEqual(av._motion_state["D"]["tracks"], {})
        self.assertEqual(self.sent, [])

    def test_window_does_not_restart_without_a_stream_restart(self):
        from unittest import mock
        with mock.patch.object(av, "STARTUP_WINDOW_MS", 15000):
            self.tick()
            self.tick(self.CAR, after_ms=20000)
            self.tick(self.CAR)
        self.assertEqual(self.events()[0], ("enter", ["vehicle"], []))

    def test_still_vehicle_parks_and_closes_event(self):
        self.tick(self.CAR)
        for _ in range(av.STILL_TICKS):
            self.tick(self.CAR)
        ev = self.events()
        self.assertEqual(ev[-1], ("leave", ["vehicle"], ["vehicle"]))  # not after 5 minutes
        self.assertIsNone(av._motion_state["D"].get("event"))
        self.sent.clear()
        self.tick(self.CAR)
        self.assertEqual(self.sent, [])

    def test_still_descriptor_is_stationary(self):
        self.tick(self.PERSON)
        for _ in range(av.STILL_TICKS):
            self.tick(self.PERSON)
        d = self.sent[-1][1]["descriptors"][0]
        self.assertEqual((d["objectType"], d["stationary"]), ("person", True))
        tr = av._motion_state["D"]["tracks"][1]
        self.assertEqual(d["idleSinceTimeMs"], tr["still_since_ms"])
        self.assertIsNotNone(av._motion_state["D"].get("event"))  # people are not parked

    def test_jitter_still_counts_as_still(self):
        tr = {"coord": [100, 100, 200, 200]}
        for i in range(av.STILL_TICKS + 1):
            tr["coord"] = [100 + (i % 3), 100, 200 + (i % 2), 200]
            av._update_still(tr, 1000 + i)
        self.assertTrue(tr["still"])
        tr["coord"] = [400, 100, 200, 200]
        av._update_still(tr, 9999)
        self.assertFalse(tr["still"])

    def test_box_display_outlasts_update_interval(self):
        self.tick(self.PERSON)
        self.assertGreater(self.sent[-1][1]["displayTimeoutMSec"],
                           av.MOTION_POLL_INTERVAL_S * 1000)

    def test_missed_track_not_drawn(self):
        self.tick(self.PERSON, self.CAR)
        self.sent.clear()
        self.tick(self.PERSON)            # car missed this tick
        self.assertEqual(self.events(), [("moving", ["person"], [])])
        self.sent.clear()
        self.tick()                       # nothing seen: no update at all
        self.assertEqual(self.sent, [])

    def test_fast_frames_report_once_per_interval(self):
        self.tick(self.PERSON)
        self.assertEqual(self.events(), [("enter", ["person"], [])])
        self.sent.clear()
        step = av.REPORT_INTERVAL_MS // 4
        for _ in range(3):                # frames analysed faster than reports go out
            self.tick(self.PERSON, after_ms=step)
        self.assertEqual(self.sent, [])
        self.tick(self.PERSON, after_ms=av.REPORT_INTERVAL_MS - 3 * step)  # a full interval
        self.assertEqual(self.events(), [("moving", ["person"], [])])

    def test_track_seen_between_reports_is_drawn(self):
        self.tick(self.PERSON, self.CAR)
        self.sent.clear()
        half = av.REPORT_INTERVAL_MS // 2
        self.tick(self.PERSON, self.CAR, after_ms=half)  # car seen mid-interval...
        self.tick(self.PERSON, after_ms=half)            # ...missed on the reporting frame
        self.assertEqual(self.events(), [("moving", ["person", "vehicle"], [])])

    def confirm_after(self, ms):
        av._event_start_ms["D"] = ms
        self.addCleanup(av._event_start_ms.pop, "D", None)

    def test_enter_waits_for_confirmation(self):
        self.confirm_after(1000)
        self.tick(self.PERSON)
        self.tick(self.PERSON, after_ms=500)
        self.assertEqual(self.sent, [])
        first_seen = av._motion_state["D"]["tracks"][1]["first_shown_ms"]
        self.tick(self.PERSON, after_ms=500)
        self.assertOpened(["person"])
        enter = [p for fn, p in self.sent if fn == "EventSmartDetect"][0]
        self.assertEqual(enter["descriptors"][0]["firstShownTimeMs"], first_seen)
        self.assertEqual(enter["clockWall"], first_seen)  # drawn from the first frame

    def test_confirmed_track_is_drawn_from_its_first_frame(self):
        # Live 17:20 on Camera B: a car crossed the road in about a second and its box only
        # appeared, back-dated by the 1 s confirmation, as it left.
        self.confirm_after(1000)
        xs = list(range(100, 211, 10))            # 12 frames, 100 ms apart
        for x in xs:
            self.tick(dict(self.CAR, coord=[x, 500, 200, 100]), after_ms=100)
        msgs = [p for fn, p in self.sent if fn == "EventSmartDetect"]
        first = av._motion_state["D"]["tracks"][1]["first_shown_ms"]
        self.assertEqual(msgs[0]["edgeType"], "enter")
        self.assertEqual(msgs[0]["clockWall"], first)
        walls = [m["clockWall"] for m in msgs]
        self.assertEqual(walls, sorted(walls))
        self.assertTrue(all(b - a >= av.REPORT_INTERVAL_MS for a, b in zip(walls, walls[1:-1])))
        self.assertEqual(walls[-1] - walls[0], 1000)    # up to the frame it confirmed on
        self.assertTrue(all(m["edgeType"] == "moving" for m in msgs[1:]))
        # Each message carries the box from its own frame.
        lefts = [m["descriptors"][0]["coord"][0] for m in msgs]
        self.assertLess(lefts[0], lefts[-1])
        self.assertEqual(av._motion_state["D"]["tracks"][1].get("history"), None)

    def test_track_joining_an_open_event_is_not_back_filled(self):
        self.confirm_after(0)
        self.tick(self.PERSON)                     # opens the event at once
        self.confirm_after(1000)
        for x in (100, 120, 140, 160, 180, 200):
            self.tick(self.PERSON, dict(self.CAR, coord=[x, 500, 200, 100]), after_ms=200)
        walls = [p["clockWall"] for fn, p in self.sent if fn == "EventSmartDetect"]
        self.assertEqual(walls, sorted(walls))       # never back in time within an event

    def test_unconfirmed_track_dropped_without_event(self):
        self.confirm_after(5000)
        self.tick(self.PERSON)
        for _ in range(av.MOTION_MISS_TOLERANCE_TICKS):   # misses at the detection rate
            self.tick(after_ms=int(av.DETECT_INTERVAL_S * 1000))
        self.assertEqual(self.sent, [])
        self.assertEqual(av._motion_state["D"]["tracks"], {})

    def test_unconfirmed_track_survives_brief_miss(self):
        self.confirm_after(1000)
        self.tick(self.PERSON)
        self.tick(after_ms=500)           # one missed frame doesn't reset it
        self.tick(self.PERSON, after_ms=500)
        self.assertOpened(["person"])

    def test_confirm_override_env(self):
        from unittest import mock
        self.confirm_after(5000)
        with mock.patch.dict(os.environ, {"AIPORT_CONFIRM_MS": "0"}):
            self.tick(self.PERSON)
        self.assertEqual(self.events(), [("enter", ["person"], [])])
        with mock.patch.dict(os.environ, {"AIPORT_CONFIRM_MS": "soon"}):
            self.assertEqual(av._confirm_ms("D"), 5000)  # bad value ignored

    def test_fast_small_walker_stays_one_track(self):
        # Live 17:28 on Camera B: a 22-wide person moving 75 then 26 units per tick
        # (no box overlap) became a new track every tick.
        for coord in ([143, 54, 22, 75], [218, 31, 22, 80], [244, 32, 22, 57]):
            self.tick(dict(self.PERSON, coord=coord))
        tracks = av._motion_state["D"]["tracks"]
        self.assertEqual(len(tracks), 1)
        self.assertEqual(tracks[1]["coord"], [244, 32, 22, 57])

    def test_two_walkers_keep_their_tracks(self):
        a, b = dict(self.PERSON, coord=[100, 300, 30, 90]), dict(self.PERSON, coord=[600, 300, 30, 90])
        self.tick(a, b)
        self.tick(dict(a, coord=[150, 300, 30, 90]), dict(b, coord=[650, 300, 30, 90]))
        tracks = av._motion_state["D"]["tracks"]
        self.assertEqual(sorted(t["coord"][0] for t in tracks.values()), [150, 650])
        self.assertEqual(len(tracks), 2)

    def test_missed_track_does_not_jump_across_the_frame(self):
        # Live 09:56 on Camera B: a far-road car's track, missed for a second, took a car
        # 180 units away, then another, then another (one track for four cars).
        car = dict(self.CAR, coord=[631, 54, 66, 117])
        self.tick(car)
        self.tick(dict(car, coord=[650, 54, 66, 117]))
        for _ in range(5):
            self.tick()
        self.tick(dict(car, coord=[469, 26, 60, 143]))
        tracks = av._motion_state["D"]["tracks"]
        self.assertEqual(len(tracks), 2)
        self.assertEqual(tracks[1]["coord"], [650, 54, 66, 117])

    def test_briefly_missed_track_keeps_its_car(self):
        car = dict(self.CAR, coord=[631, 54, 66, 117])
        self.tick(car)
        self.tick(dict(car, coord=[650, 54, 66, 117]))
        self.tick()
        self.tick()
        self.tick(dict(car, coord=[710, 54, 66, 117]))  # where its speed puts it
        tracks = av._motion_state["D"]["tracks"]
        self.assertEqual(len(tracks), 1)
        self.assertEqual(tracks[1]["coord"], [710, 54, 66, 117])

    def test_track_does_not_take_a_box_of_another_size(self):
        # Live 10:20 on Camera B: a bicycle's track took a 49x136 box beside it, then a
        # 107x253 one, and held the event open 16 s after the bicycle left.
        bike = dict(self.CAR)
        for coord in ([139, 85, 46, 57], [165, 76, 45, 57], [212, 61, 43, 57], [239, 54, 40, 57]):
            self.tick(dict(bike, coord=coord))
        self.tick(dict(bike, coord=[265, 22, 49, 136]))
        tracks = av._motion_state["D"]["tracks"]
        self.assertEqual(tracks[1]["coord"], [239, 54, 40, 57])
        self.assertEqual(len(tracks), 2)

    def test_track_follows_a_box_that_grows_gradually(self):
        for coord in ([100, 300, 40, 60], [120, 290, 55, 80], [140, 280, 75, 110]):
            self.tick(dict(self.CAR, coord=coord))
        tracks = av._motion_state["D"]["tracks"]
        self.assertEqual(len(tracks), 1)
        self.assertEqual(tracks[1]["coord"], [140, 280, 75, 110])

    def test_size_jump(self):
        # live 10:20: the bicycle and the bigger box beside it (35% of the bike inside it)
        self.assertTrue(av._size_jump([239, 54, 40, 57], [265, 22, 49, 136]))
        self.assertTrue(av._size_jump([280, 1, 107, 253], [265, 22, 49, 136]))   # 2.2x narrower
        self.assertFalse(av._size_jump([0, 0, 40, 57], [0, 0, 60, 90]))
        # live 11:33: a person up close, the new box holding the old one
        self.assertFalse(av._size_jump([516, 375, 79, 575], [366, 230, 226, 740]))

    def test_nearest_radius_is_capped(self):
        tall = {"objectType": "vehicle", "coord": [280, 96, 90, 264], "miss_count": 20}
        far = {"objectType": "vehicle", "coord": [600, 96, 90, 264]}
        near = {"objectType": "vehicle", "coord": [480, 96, 90, 264]}
        self.assertIsNone(av._nearest_detection(tall, [far], [0]))   # 320 away: past 150 x 1.5
        self.assertEqual(av._nearest_detection(tall, [near], [0]), 0)  # 200 away

    def test_passing_car_does_not_take_over_parked_car(self):
        av._event_max_ms["D"] = 0
        parked = dict(self.CAR, coord=[100, 500, 200, 100])
        self.tick(parked)
        self.tick(parked)               # parked
        av._event_max_ms["D"] = 10 ** 9
        self.sent.clear()
        self.tick(dict(self.CAR, coord=[400, 500, 200, 100]))  # parked car missed, other car nearby
        tracks = av._motion_state["D"]["tracks"]
        self.assertEqual(tracks[1]["coord"], [100, 500, 200, 100])   # parked car untouched
        self.assertEqual(len(tracks), 2)
        self.assertEqual(self.events()[0][0], "enter")              # the passing car is new

    def test_reported_box_is_smoothed(self):
        self.tick(dict(self.PERSON, coord=[400, 200, 200, 600]))
        self.tick(dict(self.PERSON, coord=[420, 200, 200, 600]))
        d = self.sent[-1][1]["descriptors"][0]
        self.assertEqual(d["coord"][0], round(0.6 * 420 + 0.4 * 400))  # between, not the raw box

    def test_parked_track_outlives_active_tolerance(self):
        av._event_max_ms["D"] = 0
        self.tick(self.CAR)
        self.tick(self.CAR)             # parked
        for _ in range(av.MOTION_MISS_TOLERANCE_TICKS + 2):
            self.tick()
        self.assertIn(1, av._motion_state["D"]["tracks"])

    def test_leave_priority(self):
        person = (1, {"objectType": "person", "score": 0.9})
        car = (2, {"objectType": "vehicle", "score": 0.95})
        face = (3, {"objectType": "face", "score": 0.5})
        self.assertEqual(av._leave_track([car, person])[0], 1)
        self.assertEqual(av._leave_track([car, person, face])[0], 3)
        better = (4, {"objectType": "person", "score": 0.1, "best": {"score": 0.99}})
        self.assertEqual(av._leave_track([person, better])[0], 4)

    def test_motion_settings_max_duration(self):
        av.handle_function(None, {"functionName": "ChangeSmartMotionSettings", "messageId": 1,
                                  "payload": {"deviceID": "D", "eventMaxDurationMSec": 300000}},
                           {})
        self.assertEqual(av._event_max_ms["D"], 300000)


class TestMultiLeave(Base):
    """Real-device leave for several participants, with a single-tracker fallback."""

    def members(self):
        person = {"objectType": "person", "coord": [400, 200, 300, 700], "score": 0.85,
                  "first_shown_ms": 90, "extra": {},
                  "best": {"jpeg": b"P", "coord": [400, 200, 300, 700], "score": 0.9, "wall": 111}}
        face = {"objectType": "face", "coord": [520, 230, 50, 60], "score": 0.92,
                "first_shown_ms": 95, "extra": {"faceEmbed": [0.1, 0.2]},
                "best": {"jpeg": b"F", "coord": [520, 230, 50, 60], "score": 0.93, "wall": 222}}
        car = {"objectType": "vehicle", "coord": [0, 500, 200, 100], "score": 0.7,
               "first_shown_ms": 80, "extra": {"name": "ABC123", "attributes": {
                   "vehicleType": {"val": "truck", "confidence": 70},
                   "color": {"val": "white", "confidence": 70}}}}
        return [(4, face), (3, person), (1, car)]

    def setUp(self):
        super().setUp()
        av._stream_dims["D"] = (2560, 1440)
        self.fallbacks = []

    def send(self, wait=30):
        from unittest import mock
        with mock.patch.object(av, "LEAVE_FALLBACK_S", wait):
            return av._send_multi_leave(None, "D", 9, self.members(),
                                        lambda: self.fallbacks.append(1))

    def test_payload_shape(self):
        self.send().cancel()
        payload = self.sent[-1][1]
        self.assertEqual((payload["edgeType"], payload["descriptors"], payload["objectTypes"]),
                         ("leave", [], []))
        self.assertEqual([(x["trackerID"], x["smartDetectSnapshotType"])
                          for x in payload["smartDetectSnapshots"]],
                         [(3, "person"), (1, "vehicle"), (4, "face")])
        attrs = payload["trackerIDAttrMap"]
        self.assertEqual(attrs["3"]["associatedFaceTrackerID"], 4)
        self.assertEqual(attrs["4"]["faceEmbed"], [0.1, 0.2])
        self.assertEqual(attrs["1"]["matchedName"], "ABC123")
        self.assertEqual(attrs["1"]["vehicleType"], {"val": "truck", "confidence": 70})
        self.assertEqual(attrs["1"]["color"]["val"], "white")
        self.assertEqual(av._snapshot_filename_to_device["smartdetectsnap_zone_4222.jpg"]["jpeg"],
                         b"F")
        self.assertEqual(av._snapshot_filename_to_device[payload["smartDetectSnapshotFullFoV"]]
                         ["jpeg"], b"P")  # Full-FoV from the person's best frame

    def test_fallback_when_nothing_requested(self):
        timer = self.send(wait=0.05)
        names = [x["smartDetectSnapshot"] for x in self.sent[-1][1]["smartDetectSnapshots"]]
        timer.join(2)
        self.assertEqual(self.fallbacks, [1])
        self.assertFalse(set(names) & set(av._pending_leaves))  # cleaned up

    def test_no_fallback_once_protect_requests_a_snapshot(self):
        from unittest import mock
        timer = self.send(wait=0.3)
        name = self.sent[-1][1]["smartDetectSnapshots"][0]["smartDetectSnapshot"]
        with mock.patch.object(av.threading, "Thread"):
            av.handle_function(None, {"functionName": "GetRequest", "messageId": 5, "payload": {
                "what": "smartDetectZoneSnapshot", "filename": name, "uri": "https://x"}}, {})
        timer.join(2)
        self.assertEqual(self.fallbacks, [])

    def test_face_outside_person_not_associated(self):
        members = self.members()
        members[0][1]["best"]["coord"] = [900, 900, 20, 20]
        self.assertEqual(av._associate_faces(members), {})


@unittest.skipUnless(_CV2, "OpenCV not installed")
class TestMotionContourArea(Base):
    """AIPORT_MOTION_CONTOUR_AREA sets the smallest motion blob per camera."""

    def area(self, spec, device_id):
        from unittest import mock
        env = {"AIPORT_MOTION_CONTOUR_AREA": spec} if spec is not None else {}
        with mock.patch.dict(os.environ, env, clear=False):
            if spec is None:
                os.environ.pop("AIPORT_MOTION_CONTOUR_AREA", None)
            return av._motion_contour_area(device_id)

    def test_default_is_frigates(self):
        self.assertEqual(self.area(None, "DEV"), 10)

    def test_per_camera(self):
        self.assertEqual(self.area("10,02000000000B=100", "02000000000B"), 100)
        self.assertEqual(self.area("10,02000000000B=100", "02000000000A"), 10)
        self.assertEqual(self.area("02000000000B=100", "OTHER"), 10)  # no default given
        self.assertEqual(self.area("50", "OTHER"), 50)

    def test_detector_uses_it(self):
        import numpy as np
        from unittest import mock
        av._motion_detectors.pop("DEV", None)
        self.addCleanup(av._motion_detectors.pop, "DEV", None)
        with mock.patch.dict(os.environ, {"AIPORT_MOTION_CONTOUR_AREA": "10,DEV=100"}):
            det = av._motion_detector("DEV", np.zeros((360, 640)).shape)
        self.assertEqual(det.contour_area, 100)


class TestFrameStats(Base):
    """Once a minute per camera: frames delivered, analysed, skipped; a warning if behind."""

    def setUp(self):
        super().setUp()
        av._frame_stats.pop("FS", None)
        av._inferred.pop("FS", None)
        av._detector_calls.pop("FS", None)
        self.addCleanup(av._frame_stats.pop, "FS", None)

    def run_minute(self, every_seq, passes):
        t = 1000.0
        av._note_frame("FS", 1, 0.02, now=t)
        seq = 1
        for i in range(passes):
            seq += every_seq
            av._inferred["FS"] += 1
            av._detector_calls["FS"] += 3
            av._note_frame("FS", seq, 0.02, now=t + 60.0 * (i + 1) / passes)

    def test_keeping_up_is_info(self):
        with self.assertLogs(av.log, level="INFO") as logs:
            self.run_minute(1, 600)                 # every frame, 10 a second
        line = [m for m in logs.output if "frames for deviceID=FS" in m][0]
        self.assertTrue(line.startswith("INFO"))
        self.assertIn("600 delivered (10.0/s), 600 analysed (10.0/s), 0 skipped, 600 with inference (3.0 detector calls each)", line)

    def test_falling_behind_warns(self):
        with self.assertLogs(av.log, level="INFO") as logs:
            self.run_minute(2, 300)                 # every other frame: 5 a second
        line = [m for m in logs.output if "frames for deviceID=FS" in m][0]
        self.assertTrue(line.startswith("WARNING"))
        self.assertIn("600 delivered (10.0/s), 300 analysed (5.0/s), 300 skipped", line)
        self.assertIn("falling behind", line)

    def test_line_gives_the_box_delay(self):
        with mock.patch.dict(av._BOX_DELAY_MS, {"FS": 1600}), self.assertLogs(av.log, "INFO") as logs:
            self.run_minute(1, 600)
        line = [m for m in logs.output if "frames for deviceID=FS" in m][0]
        self.assertIn("ms a pass, box delay 1600 ms", line)

    def test_frame_without_a_number_is_ignored(self):
        # Live 12:39: a first pass with no frame number yet crashed every camera's loop.
        av._note_frame("FS", None, 0.02, now=1000.0)
        av._note_frame("FS", 5, 0.02, now=1001.0)
        av._note_frame("FS", 6, 0.02, now=1070.0)     # past the minute: logs, no crash
        self.assertEqual(av._frame_stats["FS"]["first_seq"], 6)

    def test_new_stream_starts_counting_again(self):
        av._note_frame("FS", 500, 0.02, now=1000.0)
        av._note_frame("FS", 1, 0.02, now=1001.0)   # frame numbers restarted: a new pull
        self.assertEqual(av._frame_stats["FS"]["first_seq"], 1)
        self.assertEqual(av._frame_stats["FS"]["analysed"], 0)


class TestPollLoop(Base):
    """The camera loop wakes on each new frame (live 12:43: a fixed 0.1 s wait before each
    pass analysed only 7-8 of 10 frames a second)."""

    def start(self, ticks):
        import time as _t
        patcher = mock.patch.object(av, "_motion_tick",
                                    lambda ws, dev: (ticks.append(av._latest_frame_seq.get(dev)),
                                                     _t.sleep(0.01)))
        patcher.start()
        self.addCleanup(patcher.stop)
        av._start_motion_detector(None, "PL", "ip", 1, "uri", preserve_tracks=True)
        self.addCleanup(av._stop_motion_detector, "PL", True)
        self.addCleanup(av._latest_frame_time.pop, "PL", None)
        self.addCleanup(av._latest_frame_seq.pop, "PL", None)

    def test_every_frame_is_analysed(self):
        import time as _t
        ticks = []
        self.start(ticks)
        for seq in range(1, 21):                      # 20 frames, 40 ms apart
            with av._frame_cond:
                av._latest_frame_seq["PL"] = seq
                av._latest_frame_time["PL"] = (1_000_000 + seq, seq)
                av._frame_cond.notify_all()
            _t.sleep(0.04)
        _t.sleep(0.1)
        self.assertEqual(sorted(set(t for t in ticks if t)), list(range(1, 21)))

    def test_ticks_continue_without_a_stream(self):
        import time as _t
        ticks = []
        self.start(ticks)
        _t.sleep(0.55)                                # no frames at all
        self.assertGreaterEqual(len(ticks), 3)        # ~every DETECT_INTERVAL_S


class TestCameraStatus(Base):
    """EventAIPortStatus follows each paired camera's health, sent only on a change."""

    def setUp(self):
        super().setUp()
        self.now = 10_000.0
        av._stream_points["CS"] = 2.5
        av._stream_started_at["CS"] = self.now
        av._last_status_sent["CS"] = (True, True, True, True)   # sent at UiStreamControl
        for table in (av._stream_points, av._stream_started_at, av._last_status_sent,
                      av._last_analysed, av._latest_frame_time, av._motion_poll_thread,
                      av._last_frame_at):
            self.addCleanup(table.pop, "CS", None)

    def healthy_at(self, t):
        av._latest_frame_time["CS"] = (0, int(t * 1000))
        av._last_frame_at["CS"] = t
        av._last_analysed["CS"] = t

    def statuses(self):
        return [(p["isStreaming"], p["isSmartDetectReady"]) for fn, p in self.sent
                if fn == "EventAIPortStatus"]

    def test_healthy_camera_sends_nothing(self):
        self.healthy_at(self.now + 50)
        av._check_status(None, now=self.now + 55)
        self.assertEqual(self.statuses(), [])

    def test_stream_without_frames_reports_not_streaming_then_recovers(self):
        self.healthy_at(self.now + 5)
        av._check_status(None, now=self.now + 20)              # 15 s quiet: still fine
        self.assertEqual(self.statuses(), [])
        av._check_status(None, now=self.now + 40)              # 35 s: neither frames nor passes
        self.assertEqual(self.statuses(), [(False, False)])
        self.healthy_at(self.now + 45)
        av._check_status(None, now=self.now + 46)
        self.assertEqual(self.statuses(), [(False, False), (True, True)])

    def test_dead_loop_reports_not_ready(self):
        # Live 12:39: every camera's loop had crashed; Protect was still told all was well.
        class Dead:
            def is_alive(self):
                return False
        av._motion_poll_thread["CS"] = Dead()
        self.healthy_at(self.now + 50)
        av._check_status(None, now=self.now + 51)
        self.assertEqual(self.statuses(), [(True, False)])

    def test_grace_after_the_stream_starts(self):
        av._check_status(None, now=self.now + 25)              # no frame yet, 25 s in
        self.assertEqual(self.statuses(), [])

    def test_a_brief_pull_restart_still_counts_as_streaming(self):
        # Live 22:25: a restart cleared the frame time, so 5 s later Protect was told "no
        # frame for 30 s"; it resent settings and dropped the relay again, every 15 s.
        self.healthy_at(self.now + 100)
        av._active_streams["CS"] = None
        av._stop_stream("CS")                                  # the pull died; restarting
        av._last_analysed["CS"] = self.now + 105
        av._check_status(None, now=self.now + 105)
        self.assertEqual(self.statuses(), [])
        av._last_analysed["CS"] = self.now + 135
        av._check_status(None, now=self.now + 135)             # 35 s with no frame
        self.assertEqual(self.statuses(), [(False, True)])


class TestBoxDelay(Base):
    """AIPORT_BOX_DELAY_MS back-dates frame-describing timestamps per camera."""

    def setUp(self):
        super().setUp()
        from unittest import mock
        for name, value in (("_BOX_DELAY_DEFAULT_MS", 1000), ("_BOX_DELAY_MS", {"SLOW": 3000})):
            patcher = mock.patch.object(av, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_parse(self):
        self.assertEqual(av._parse_box_delay("2500, 02000000000B=3000 ,02000000000A=1500"),
                         (2500, {"02000000000B": 3000, "02000000000A": 1500}))
        self.assertEqual(av._parse_box_delay(""), (0, {}))
        self.assertEqual(av._parse_box_delay("x,DEV=y,500"), (500, {}))

    def test_per_camera_delay(self):
        self.assertEqual(av._box_delay_ms("SLOW"), 3000)
        self.assertEqual(av._box_delay_ms("other"), 1000)
        before = int(av.time.time() * 1000)
        self.assertLessEqual(av._frame_wall_ms("SLOW"), before - 3000 + 50)

    def test_event_backdated(self):
        before = int(av.time.time() * 1000)
        av._send_smart_detect_event(None, "SLOW", edge_type="enter", object_type="vehicle",
                                    coord=[1, 2, 3, 4], confidence=0.9)
        p = self.sent[-1][1]
        self.assertTrue(before - 3100 <= p["clockWall"] <= before - 2900)
        self.assertTrue(before - 3100 <= p["descriptors"][0]["firstShownTimeMs"] <= before - 2900)

    def test_stream_clocks_backdated_not_negative(self):
        av._stream_started_monotonic_ms["SLOW"] = int(av.time.monotonic() * 1000) - 10000
        self.addCleanup(av._stream_started_monotonic_ms.pop, "SLOW", None)
        mono, stream = av._stream_clocks("SLOW")
        self.assertTrue(6900 <= mono <= 7100 and mono == stream)
        av._stream_started_monotonic_ms["SLOW"] = int(av.time.monotonic() * 1000)
        self.assertEqual(av._stream_clocks("SLOW"), (0, 0))


class TestSnapshotUpload(Base):
    class _Resp:
        status_code = 200
        text = "ok"

    def _with_stream(self, *devices):
        self._dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self._dir, True)
        for dev in devices:
            with open(os.path.join(self._dir, f"{dev}.jpg"), "wb") as f:
                f.write(b"\xff\xd8\xff\xd9" + dev.encode())
        self._orig_dir = av._STREAM_DIR
        av._STREAM_DIR = self._dir
        self.addCleanup(lambda: setattr(av, "_STREAM_DIR", self._orig_dir))
        self.posted = []
        self._orig_post = av.requests.post
        av.requests.post = lambda uri, headers=None, files=None, **kw: (
            self.posted.append(files["payload"][1]) or self._Resp())
        self.addCleanup(lambda: setattr(av.requests, "post", self._orig_post))

    INFO = {"mac_nosep": "X", "type": "UVC AI Port", "sysid_hex": "0xa5f1"}

    def test_fullfov_uploads_full_frame(self):
        self._with_stream("AAA")
        av._record_snapshot_filename("full.jpg", "AAA", kind="fullfov")
        av._upload_snapshot("https://c/upload/t", self.INFO, "full.jpg", "smartDetectZoneSnapshotFullFoV")
        self.assertEqual(len(self.posted), 1)
        self.assertTrue(self.posted[0].endswith(b"AAA"))

    def test_kept_best_frame_wins_over_current(self):
        self._with_stream("AAA")
        av._record_snapshot_filename("full.jpg", "AAA", kind="fullfov", jpeg=b"\xff\xd8BEST\xff\xd9")
        av._upload_snapshot("https://c/upload/t", self.INFO, "full.jpg", "smartDetectZoneSnapshotFullFoV")
        self.assertEqual(self.posted, [b"\xff\xd8BEST\xff\xd9"])

    @unittest.skipUnless(_FFMPEG, "ffmpeg not installed")
    def test_kept_best_frame_is_cropped(self):
        self._with_stream("AAA")
        frame = os.path.join(self._dir, "best-src.jpg")
        make_frame(frame, 1504, 2016)
        with open(frame, "rb") as f:
            best = f.read()
        av._record_snapshot_filename("obj.jpg", "AAA", kind="object", coord=[400, 300, 100, 100],
                                     jpeg=best)
        av._upload_snapshot("https://c/upload/t", self.INFO, "obj.jpg", "smartDetectZoneSnapshot")
        self.assertEqual(len(self.posted), 1)
        self.assertTrue(self.posted[0].startswith(b"\xff\xd8"))
        self.assertNotEqual(self.posted[0], best)  # cropped, not the whole frame
        self.assertEqual(os.listdir(os.path.join(self._dir, "tmp")), [])  # temp removed

    def test_unknown_ambiguous_refuses(self):
        self._with_stream("AAA", "BBB")
        av._active_streams["AAA"] = object()
        av._active_streams["BBB"] = object()
        av._upload_snapshot("https://c/upload/t", self.INFO, None, "smartDetectZoneSnapshot")
        self.assertEqual(self.posted, [])

    def test_unknown_sole_stream_falls_back(self):
        self._with_stream("AAA")
        av._active_streams["AAA"] = object()
        av._upload_snapshot("https://c/upload/t", self.INFO, None, "smartDetectZoneSnapshot")
        self.assertEqual(len(self.posted), 1)
        self.assertTrue(self.posted[0].endswith(b"AAA"))

    def test_no_uri_noop(self):
        self._with_stream("AAA")
        av._upload_snapshot(None, self.INFO, "x", "snapshot")
        self.assertEqual(self.posted, [])


class TestCrop(unittest.TestCase):
    @unittest.skipUnless(_FFMPEG, "ffmpeg not available")
    def test_crop_produces_square(self):
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "frame.jpg")
            make_frame(src)
            data = av._crop_snapshot_jpeg(src, [400, 400, 200, 200], 1920, 1080)
            self.assertIsNotNone(data)
            self.assertTrue(data.startswith(b"\xff\xd8") and data.endswith(b"\xff\xd9"))
            # Assert the JPEG is non-trivial and exercise crop math for corner
            # cases rather than re-decoding dimensions.
            for coord in ([0, 0, 100, 150], [950, 950, 50, 50], [0, 0, 1000, 1000], [500, 500, 5, 5]):
                self.assertIsNotNone(av._crop_snapshot_jpeg(src, coord, 1920, 1080), coord)

    @unittest.skipUnless(_FFMPEG, "ffmpeg not available")
    def test_crop_bad_input_returns_none(self):
        with tempfile.TemporaryDirectory() as d:
            bad = os.path.join(d, "bad.jpg")
            open(bad, "wb").write(b"not a jpeg")
            self.assertIsNone(av._crop_snapshot_jpeg(bad, [1, 2, 3, 4], 1920, 1080))


class TestConfigHandler(Base):
    INFO = {"mac_nosep": "X", "type": "UVC AI Port", "sysid_hex": "0xa5f1"}

    def test_change_smart_detect_settings(self):
        msg = {"functionName": "ChangeSmartDetectSettings", "messageId": 1,
               "responseExpected": True,
               "payload": {"deviceID": "MAC", "zones": {}, "excludeZones": {},
                           "lines": {"1": {"coord": [0, 250, 1000, 250]},
                                     "2": {"coord": [250, 0, 250, 1000]}}}}
        av.handle_function(None, msg, self.INFO)
        self.assertEqual(len(av._lines["MAC"]), 2)
        self.assertEqual(av._lines["MAC"][0]["p1"], (0, 250))
        self.assertEqual(self.sent[-1][0], "ChangeSmartDetectSettings")


class TestEventDedupe(Base):
    def setUp(self):
        super().setUp()
        av._last_status_sent.clear()

    def test_status_event_sent_only_on_change(self):
        av._send_status_event(None, "D", True, True, True, True)
        av._send_status_event(None, "D", True, True, True, True)
        self.assertEqual(len(self.sent), 1)
        av._send_status_event(None, "D", True, False, True, True)  # stream stopped
        self.assertEqual(len(self.sent), 2)
        self.assertEqual(self.sent[-1][0], "EventAIPortStatus")

    def test_feature_flags_resent_on_every_stream_start(self):
        # Protect re-applies the camera's smart-detect capability on each event;
        # suppressing repeats left a re-paired camera motion-only (lab, 2026-10-26).
        av._send_feature_flags_event(None, "D")
        av._send_feature_flags_event(None, "D")
        self.assertEqual(len(self.sent), 2)
        self.assertEqual([m[0] for m in self.sent], ["EventFeatureFlagsUpdated"] * 2)


class TestStreamClocks(Base):
    def test_stream_clocks_zero_before_start(self):
        self.assertEqual(av._stream_clocks("nope"), (0, 0))

    def test_stream_clocks_advance_after_start(self):
        av._stream_started_monotonic_ms["D"] = int(time.monotonic() * 1000) - 1500
        mono, stream = av._stream_clocks("D")
        self.assertGreaterEqual(mono, 1400)
        self.assertEqual(mono, stream)  # same timebase, like a real camera

    def test_stop_stream_clears_clock_origin(self):
        av._active_streams["D"] = None
        av._stream_started_monotonic_ms["D"] = int(time.monotonic() * 1000)
        av._latest_frame_time["D"] = (1, 1)
        av._analysed_frame_time["D"] = (1, 1)
        av._stop_stream("D")
        self.assertNotIn("D", av._stream_started_monotonic_ms)
        self.assertNotIn("D", av._latest_frame_time)
        self.assertNotIn("D", av._analysed_frame_time)


class FakeProc:
    def __init__(self, rc=None):
        self.rc, self.pid, self.killed = rc, 4242, False

    def poll(self):
        return self.rc

    def kill(self):
        self.killed = True


class TestStalledPulls(Base):
    # Live 17:28: a pull sat 70 min on a dead relay connection at 0% CPU, never exiting.
    START = 1_000_000

    def pull(self, rc=None, last_frame=None):
        proc = FakeProc(rc)
        av._active_streams["D"] = proc
        av._stream_started_monotonic_ms["D"] = self.START
        if last_frame is not None:
            av._latest_frame_time["D"] = (0, last_frame)
        self.addCleanup(av._latest_frame_time.pop, "D", None)
        return proc

    def test_a_pull_with_no_frame_for_30_s_is_killed(self):
        proc = self.pull(last_frame=self.START + 5_000)
        self.assertEqual(av._restart_stalled_pulls(self.START + 34_000), [])
        self.assertEqual(av._restart_stalled_pulls(self.START + 35_000), ["D"])
        self.assertTrue(proc.killed)

    def test_a_new_pull_gets_30_s_for_its_first_frame(self):
        proc = self.pull()
        self.assertEqual(av._restart_stalled_pulls(self.START + 29_000), [])
        self.assertEqual(av._restart_stalled_pulls(self.START + 30_000), ["D"])
        self.assertTrue(proc.killed)

    def test_a_pull_that_already_exited_is_left_to_its_reader(self):
        proc = self.pull(rc=0, last_frame=self.START)
        self.assertEqual(av._restart_stalled_pulls(self.START + 60_000), [])
        self.assertFalse(proc.killed)

    def test_a_streaming_pull_is_left_alone(self):
        proc = self.pull(last_frame=self.START + 59_900)
        self.assertEqual(av._restart_stalled_pulls(self.START + 60_000), [])
        self.assertFalse(proc.killed)


class TestRtspTimeout(Base):
    def args_for(self, help_text):
        result = subprocess.CompletedProcess([], 0, stdout=help_text)
        with mock.patch.object(av, "_rtsp_timeout_flag", []), \
                mock.patch.object(av.subprocess, "run", return_value=result):
            return av._rtsp_timeout_args()

    def test_ffmpeg_5_and_later_use_timeout(self):
        self.assertEqual(self.args_for(
            "  -listen_timeout <int> .D... set maximum timeout (in seconds) to wait for incoming"
            " connections\n  -timeout <int64> .D... set timeout (in microseconds) of socket I/O"
            " operations\n"), ["-timeout", "15000000"])

    def test_older_ffmpeg_uses_stimeout_not_the_listen_timeout(self):
        self.assertEqual(self.args_for(
            "  -timeout <int> .D... set maximum timeout (in seconds) to wait for incoming"
            " connections (-1 is infinite, imply flag listen)\n  -stimeout <int> .D... set"
            " timeout (in microseconds) of socket TCP I/O operations\n"),
            ["-stimeout", "15000000"])

    def test_no_known_option_means_no_timeout(self):
        self.assertEqual(self.args_for(""), [])


class TestDetectionRate(Base):
    def test_ticks_count_analysed_frames(self):
        self.assertEqual(av._ticks(3), max(1, round(3 / av.DETECT_INTERVAL_S)))

    def test_detection_never_slower_than_reporting(self):
        self.assertLessEqual(av.DETECT_INTERVAL_S, av.MOTION_POLL_INTERVAL_S)
        self.assertGreaterEqual(av.DETECT_FPS, round(1 / av.DETECT_INTERVAL_S))

    def test_event_start_ms_from_protect(self):
        self.addCleanup(av._event_start_ms.pop, "CAM", None)
        msg = {"functionName": "ChangeSmartDetectSettings", "messageId": 1,
               "payload": {"deviceID": "CAM", "eventStartMSec": 1000}}
        av.handle_function(None, msg, {})
        self.assertEqual(av._confirm_ms("CAM"), 1000)
        # A partial update without the key (e.g. the LPR flag) keeps the value.
        av.handle_function(None, {"functionName": "ChangeSmartDetectSettings", "messageId": 2,
                                  "payload": {"deviceID": "CAM", "isLprCamera": 0}}, {})
        self.assertEqual(av._confirm_ms("CAM"), 1000)
        self.assertEqual(av._confirm_ms("OTHER"), 0)


class TestFrameTiming(Base):
    """Event times describe the analysed frame (when it left ffmpeg), not the send time."""

    WALL, MONO, STARTED = 1_760_000_000_000, 50_000, 40_000

    def setUp(self):
        super().setUp()
        av._stream_started_monotonic_ms["D"] = self.STARTED
        av._analysed_frame_time["D"] = (self.WALL, self.MONO)

    def delay(self, ms):
        orig = av._BOX_DELAY_MS
        av._BOX_DELAY_MS = {"D": ms}
        self.addCleanup(setattr, av, "_BOX_DELAY_MS", orig)

    def test_clocks_use_frame_time(self):
        self.assertEqual(av._frame_wall_ms("D"), self.WALL)
        self.assertEqual(av._stream_clocks("D"), (self.MONO - self.STARTED,) * 2)

    def test_residual_delay_backdates_frame_time(self):
        self.delay(500)
        self.assertEqual(av._frame_wall_ms("D"), self.WALL - 500)
        self.assertEqual(av._stream_clocks("D"), (self.MONO - self.STARTED - 500,) * 2)

    def test_newer_frame_does_not_move_reported_time(self):
        # The reader keeps going while a tick works on its frame.
        av._latest_frame_time["D"] = (self.WALL + 900, self.MONO + 900)
        self.assertEqual(av._frame_wall_ms("D"), self.WALL)

    def test_falls_back_to_now_before_any_frame(self):
        before = int(time.time() * 1000)
        self.assertGreaterEqual(av._frame_wall_ms("OTHER"), before)

    def test_classify_records_the_frame_stamp(self):
        import numpy as np

        class NoDetections:
            def detect(self, frame, min_score=None):
                return []

        orig = av._get_detector
        av._get_detector = lambda: NoDetections()
        self.addCleanup(setattr, av, "_get_detector", orig)
        av._latest_frame["D"] = np.zeros((av.DETECT_IMG_SIZE, av.DETECT_IMG_SIZE, 3), np.uint8)
        av._latest_frame_time["D"] = (self.WALL + 2000, self.MONO + 2000)
        self.assertEqual(av._classify_frame_detections("D"), [])
        self.assertEqual(av._analysed_frame_time["D"], (self.WALL + 2000, self.MONO + 2000))

    def test_classify_without_frame_keeps_previous_stamp(self):
        self.assertEqual(av._classify_frame_detections("D"), [])
        self.assertEqual(av._analysed_frame_time["D"], (self.WALL, self.MONO))

    def test_best_frame_time_is_frame_time(self):
        self.delay(300)
        tr = {}
        av._capture_best("D", tr, {"score": 0.9, "coord": [1, 2, 3, 4],
                                   "frame": (0, b"\xff\xd8jpeg\xff\xd9")})
        self.assertEqual(tr["best"]["wall"], self.WALL - 300)


class TestDetectionDedupe(Base):
    def test_overlapping_same_type_collapse_to_one(self):
        dets = [
            {"objectType": "person", "score": 0.67, "coord": [100, 100, 200, 400]},
            {"objectType": "person", "score": 0.51, "coord": [110, 110, 195, 390]},
        ]
        out = av._dedupe_detections(dets)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["score"], 0.67)

    def test_distinct_objects_survive(self):
        dets = [
            {"objectType": "person", "score": 0.67, "coord": [0, 0, 100, 100]},
            {"objectType": "person", "score": 0.60, "coord": [500, 500, 100, 100]},
        ]
        self.assertEqual(len(av._dedupe_detections(dets)), 2)

    def test_different_types_not_merged(self):
        # A person box overlapping an animal box is genuinely two objects.
        dets = [
            {"objectType": "person", "score": 0.67, "coord": [100, 100, 200, 400]},
            {"objectType": "animal", "score": 0.51, "coord": [110, 110, 195, 390]},
        ]
        self.assertEqual(len(av._dedupe_detections(dets)), 2)


class TestMotionLoopLifecycle(Base):
    def setUp(self):
        super().setUp()
        av._motion_poll_stop.clear()
        av._motion_poll_thread.clear()
        # Make ticks observable without running the detector.
        self.ticks = []
        self._orig_tick = av._motion_tick
        av._motion_tick = lambda ws, dev: self.ticks.append(dev)
        self.addCleanup(lambda: setattr(av, "_motion_tick", self._orig_tick))
        self._orig_interval = av.MOTION_POLL_INTERVAL_S
        av.MOTION_POLL_INTERVAL_S = 0.05
        self.addCleanup(lambda: setattr(av, "MOTION_POLL_INTERVAL_S", self._orig_interval))
        self.addCleanup(setattr, av, "DETECT_INTERVAL_S", av.DETECT_INTERVAL_S)
        av.DETECT_INTERVAL_S = 0.02
        self.addCleanup(lambda: av._stop_motion_detector("D"))

    def test_each_frame_analysed_once(self):
        av._latest_frame_time["D"] = (1, 1)
        av._start_motion_detector(None, "D", "ip", 1, "uri")
        time.sleep(0.2)
        self.assertEqual(self.ticks, ["D"])        # same frame: analysed once, then waits
        av._latest_frame_time["D"] = (2, 2)
        time.sleep(0.2)
        self.assertEqual(self.ticks, ["D", "D"])   # a new frame is analysed
        av._stop_motion_detector("D")

    def test_restart_does_not_leave_overlapping_loops(self):
        # A stream restart calls _start_motion_detector again; the previous loop
        # must be stopped AND joined, or both mutate the same track state.
        av._start_motion_detector(None, "D", "ip", 1, "uri")
        av._start_motion_detector(None, "D", "ip", 1, "uri")  # simulate restart
        self.assertEqual(len(av._motion_poll_stop), 1)
        self.assertEqual(len(av._motion_poll_thread), 1)
        live = av._motion_poll_thread["D"]
        self.assertTrue(live.is_alive())
        av._stop_motion_detector("D")
        # _stop pops the handle as part of joining it, so assert on the captured
        # thread, not on the (now-absent) dict entry.
        self.assertFalse(live.is_alive())
        self.assertNotIn("D", av._motion_poll_stop)
        self.assertNotIn("D", av._motion_poll_thread)

    def test_superseded_loop_exits(self):
        av._start_motion_detector(None, "D", "ip", 1, "uri")
        first = av._motion_poll_thread["D"]
        av._start_motion_detector(None, "D", "ip", 1, "uri")
        time.sleep(0.2)
        self.assertFalse(first.is_alive())  # old loop bailed out, not still ticking
        av._stop_motion_detector("D")

    def test_preserve_tracks_keeps_open_tracks(self):
        # A UiStreamControl refresh must not abandon an in-flight track, or a
        # line crossing can never accumulate a movement segment.
        av._motion_state["D"] = {"tracks": {1: {"event_id": 1, "objectType": "person",
                                                 "coord": [0, 0, 10, 10], "score": 0.9,
                                                 "first_shown_ms": 1, "miss_count": 0,
                                                 "prev_center": (5, 5), "crossed_lines": {}}},
                                  "next_tracker_id": 2}
        av._start_motion_detector(None, "D", "ip", 1, "uri", preserve_tracks=True)
        self.assertIn("D", av._motion_state)
        self.assertIn(1, av._motion_state["D"]["tracks"])
        av._stop_motion_detector("D")

    def test_discard_emits_leave_for_open_tracks(self):
        sent = []
        # Own attribute: Base's cleanup restores send_msg from self._orig_send.
        self._orig_smart_detect = av._send_smart_detect_event
        av._send_smart_detect_event = lambda ws, dev, **kw: sent.append((ws, dev, kw["edge_type"]))
        self.addCleanup(lambda: setattr(av, "_send_smart_detect_event", self._orig_smart_detect))
        av._motion_state["D"] = {"tracks": {7: {"event_id": 42, "objectType": "person",
                                                 "coord": [0, 0, 10, 10], "score": 0.9,
                                                 "first_shown_ms": 1, "miss_count": 0,
                                                 "prev_center": (5, 5), "crossed_lines": {}}},
                                  "next_tracker_id": 8}
        ws = object()
        av._stop_motion_detector("D", ws=ws)  # preserve_tracks defaults to False
        # The leave must go out on the live connection, not None.
        self.assertEqual(sent, [(ws, "D", "leave")])
        self.assertNotIn("D", av._motion_state)


class TestStreamPoints(Base):
    """Capacity points match the real AI Port's ubnt_onvifd (GetUiStreamPoints, 5.1.12)."""

    def setUp(self):
        super().setUp()
        av._stream_points.clear()
        self.addCleanup(av._stream_points.clear)
        self.stopped = []
        stubs = {
            "_start_stream": lambda *a, **kw: None,
            "_start_motion_detector": lambda *a, **kw: None,
            "_stop_stream": lambda dev: self.stopped.append(("stream", dev)),
            "_stop_motion_detector": lambda dev, preserve_tracks=False, ws=None:
                self.stopped.append(("detector", dev, preserve_tracks)),
            "_send_status_event": lambda *a, **kw: None,
            "_send_feature_flags_event": lambda *a, **kw: None,
        }
        for name, stub in stubs.items():
            orig = getattr(av, name)
            setattr(av, name, stub)
            self.addCleanup(setattr, av, name, orig)

    def call(self, fn, payload, msg_id=1, response_expected=True):
        self.sent.clear()
        av.handle_function(None, {"functionName": fn, "messageId": msg_id,
                                  "responseExpected": response_expected,
                                  "payload": payload}, {})
        return self.sent

    def stream(self, dev, streaming=True, w=1920, h=1080, fps=30):
        sent = self.call("UiStreamControl", {"deviceID": dev, "streaming": streaming,
                                             "ip": "10.0.0.1", "port": "7447", "uri": "abc",
                                             "width": w, "height": h, "fps": fps})
        return sent[0][1]

    def test_measured_tiers(self):
        # Values read from the real device for the same inputs.
        measured = [
            ((640, 360, 30), 2), ((1920, 1080, 30), 2), ((1080, 1920, 30), 2),
            ((2304, 1296, 20), 2), ((2304, 1296, 25), 2.5), ((2560, 1440, 30), 2.5),
            ((2592, 1944, 15), 2.5), ((2592, 1944, 25), 5), ((3840, 2160, 10), 2.5),
            ((3840, 2160, 30), 5), ((2160, 3840, 30), 5), ((4096, 2160, 25), 5),
            ((4096, 2160, 30), 10), ((4000, 3000, 25), 10),
        ]
        for (w, h, fps), points in measured:
            with self.subTest(w=w, h=h, fps=fps):
                self.assertEqual(av._stream_points_for(w, h, fps), points)

    def test_invalid_dimensions(self):
        for args in [(0, 1080, 30), (1920, None, 30), (1920, 1080, "x"), (1920, 1080, -1)]:
            with self.subTest(args=args):
                self.assertIsNone(av._stream_points_for(*args))

    def test_used_points_follow_streams(self):
        self.assertEqual(self.stream("CAM4K", w=3840, h=2160), {"status": "started", "usedPoints": 5})
        self.assertEqual(self.stream("CAMHD")["usedPoints"], 7)
        self.assertEqual(self.stream("CAM4K", streaming=False),
                         {"status": "stopped", "usedPoints": 2})
        self.assertEqual(self.stream("CAMHD", streaming=False)["usedPoints"], 0)

    def test_half_points_kept_as_decimal(self):
        self.assertEqual(self.stream("CAM2K", w=2560, h=1440)["usedPoints"], 2.5)
        self.assertEqual(self.stream("CAM2K-B", w=2560, h=1440)["usedPoints"], 5)

    def test_points_scale_doubles_the_room(self):
        with mock.patch.object(av, "POINTS_SCALE", 0.5):
            self.assertEqual(self.stream("CAM2K", w=2560, h=1440)["usedPoints"], 1.2)
            self.assertEqual(self.stream("CAM2K-B", w=2560, h=1440)["usedPoints"], 2.5)
            self.assertEqual(self.stream("CAM4K", w=3840, h=2160)["usedPoints"], 5)
            self.assertEqual(self.call("GetStreamList", {})[0][1]["list"][0],
                             {"deviceID": "CAM2K", "points": 1.2})
            sent = self.call("GetUiStreamPoints", {"list": [
                {"deviceID": "A", "width": 3840, "height": 2160, "fps": 30}]})
            self.assertEqual(sent[0][1], {"list": [{"deviceID": "A", "points": 2.5}]})

    def test_points_scale_setting(self):
        self.assertEqual(av._parse_points_scale(None), 1.0)
        self.assertEqual(av._parse_points_scale("0.5"), 0.5)
        for bad in ("0", "-1", "half"):
            with self.subTest(bad=bad):
                self.assertEqual(av._parse_points_scale(bad), 1.0)

    def test_failed_start_not_counted(self):
        def boom(*a, **kw):
            raise RuntimeError("no rtsp")
        orig = av._start_stream
        av._start_stream = boom
        self.addCleanup(setattr, av, "_start_stream", orig)
        self.assertEqual(self.stream("CAM"), {"status": "stopped", "usedPoints": 0})
        self.assertEqual(av._stream_points, {})

    def test_get_stream_list(self):
        self.assertEqual(self.call("GetStreamList", {}), [("GetStreamList", {"list": []})])
        self.stream("CAM4K", w=3840, h=2160)
        self.stream("CAM2K", w=2560, h=1440)
        self.assertEqual(self.call("GetStreamList", {})[0][1], {"list": [
            {"deviceID": "CAM4K", "points": 5}, {"deviceID": "CAM2K", "points": 2.5}]})

    def test_get_ui_stream_points(self):
        sent = self.call("GetUiStreamPoints", {"list": [
            {"deviceID": "A", "width": 3840, "height": 2160, "fps": 30},
            {"deviceID": "B", "width": 1920, "height": 1080, "fps": 30},
            {"deviceID": "C", "width": 0, "height": 1080, "fps": 30},
            "junk",
        ]})
        self.assertEqual(sent, [("GetUiStreamPoints", {"list": [
            {"deviceID": "A", "points": 5}, {"deviceID": "B", "points": 2}]})])
        self.assertEqual(av._stream_points, {})  # a query, not a reservation

    def test_reset_stops_all_and_keeps_tracks(self):
        self.stream("A")
        self.stream("B", w=3840, h=2160)
        self.stopped.clear()
        sent = self.call("ResetAIPortStreams", {})
        self.assertEqual(sent, [("ResetAIPortStreams", {})])
        self.assertEqual(av._stream_points, {})
        self.assertEqual(sorted(self.stopped),
                         [("detector", "A", True), ("detector", "B", True),
                          ("stream", "A"), ("stream", "B")])
        self.assertEqual(self.call("GetStreamList", {})[0][1], {"list": []})


class TestFirmwareUpdate(unittest.TestCase):
    """UpdateFirmwareRequest is acked and its target version pinned, never flashed."""

    def setUp(self):
        self._orig_state = av.STATE_DIR
        self._tmp = tempfile.mkdtemp()
        av.STATE_DIR = self._tmp
        self.addCleanup(lambda: setattr(av, "STATE_DIR", self._orig_state))
        self.addCleanup(shutil.rmtree, self._tmp, True)
        self._orig_sleep = av.time.sleep
        av.time.sleep = lambda *_: None
        self.addCleanup(lambda: setattr(av.time, "sleep", self._orig_sleep))

    def test_parse_path_form(self):
        self.assertEqual(
            av._version_from_update_uri(
                "https://10.0.0.1:7444/internal/update/aiport/uvc/firmware/5.2.0/FCECDA000001"),
            "5.2.0")

    def test_parse_query_form(self):
        self.assertEqual(
            av._version_from_update_uri(
                "https://10.0.0.1:7444/internal/update?platform=aiport&product=uvc"
                "&updateType=firmware&version=5.2.0&mac=FCECDA000001"),
            "5.2.0")

    def test_parse_rejects_custom_uri(self):
        self.assertIsNone(av._version_from_update_uri("https://example.com/fw.bin"))
        self.assertIsNone(av._version_from_update_uri(""))

    def test_pin_round_trip(self):
        self.assertIsNone(config.pinned_fw_version(av.STATE_DIR))
        config.pin_fw_version(av.STATE_DIR, "9.9.9")
        self.assertEqual(config.pinned_fw_version(av.STATE_DIR), "9.9.9")

    def test_handler_pins_and_reboots(self):
        ws = _FakeWS()
        av.handle_function(ws, {
            "functionName": "UpdateFirmwareRequest",
            "messageId": "m1",
            "payload": {"uri": "https://c:7444/internal/update/aiport/uvc/firmware/6.0.0/AA",
                        "timeoutMs": 30000},
        }, {})
        self.assertEqual(config.pinned_fw_version(av.STATE_DIR), "6.0.0")
        names = [json.loads(m)["functionName"] for m in ws.sent]
        self.assertEqual(names, ["EventUpdateFirmwareStatus", "EventUpdateFirmwareStatus"])
        statuses = [json.loads(m)["payload"]["status"] for m in ws.sent]
        self.assertEqual(statuses, ["FW_DOWNLOADING", "FW_UPDATING"])
        self.assertTrue(ws.closed)

    def test_handler_with_unparsable_uri_acks_without_pinning(self):
        ws = _FakeWS()
        av.handle_function(ws, {
            "functionName": "UpdateFirmwareRequest",
            "messageId": "m2",
            "responseExpected": True,
            "payload": {"uri": "https://example.com/fw.bin"},
        }, {})
        self.assertIsNone(config.pinned_fw_version(av.STATE_DIR))
        self.assertTrue(ws.closed)
        acks = [json.loads(m) for m in ws.sent
                if json.loads(m)["functionName"] == "UpdateFirmwareRequest"]
        self.assertEqual(len(acks), 1)
        self.assertEqual(acks[0]["inResponseTo"], "m2")

    def test_change_device_settings_persists_name(self):
        ws = _FakeWS()
        av.handle_function(ws, {
            "functionName": "ChangeDeviceSettings",
            "messageId": "m3",
            "responseExpected": True,
            "payload": {"name": "Front Door", "timezone": "Europe/Berlin"},
        }, {})
        self.assertEqual(config.device_name(av.STATE_DIR), "Front Door")
        reply = json.loads(ws.sent[-1])
        self.assertEqual(reply["functionName"], "ChangeDeviceSettings")
        self.assertEqual(reply["payload"]["name"], "Front Door")


class TestStreamControlUsedPoints(unittest.TestCase):
    """Protect reads usedPoints/10 as the AI Port's load and treats a reply
    without it as a full AI Port (pairing then fails: estimated_capacity_exceeded)."""

    def setUp(self):
        # Two 1080p cameras still streaming: 2 points each (TestStreamPoints has the tiers).
        self._points = dict(av._stream_points)
        av._stream_points.clear()
        av._stream_points.update({"A": 2, "B": 2})
        self.addCleanup(lambda: (av._stream_points.clear(), av._stream_points.update(self._points)))

    def _reply(self, fn, payload):
        ws = _FakeWS()
        av.handle_function(ws, {"functionName": fn, "messageId": "m", "payload": payload}, {})
        return next(json.loads(m)["payload"] for m in ws.sent
                    if json.loads(m)["functionName"] == fn)

    def test_onvif_stop_reply_has_used_points(self):
        reply = self._reply("OnvifStreamControl", {"streaming": False})
        self.assertEqual(reply, {"status": "stopped", "usedPoints": 4})

    def test_ui_stop_reply_has_used_points(self):
        reply = self._reply("UiStreamControl", {"streaming": False})
        self.assertEqual(reply["usedPoints"], 4)


class _FakeWS:
    def __init__(self):
        self.sent = []
        self.closed = False

    def send(self, data):
        self.sent.append(data)

    def close(self, *a, **k):
        self.closed = True


class HwDecoderSelection(unittest.TestCase):
    """AIPORT_HWDEC pins the backend so this is device-independent."""

    def setUp(self):
        self._saved = os.environ.get("AIPORT_HWDEC")
        self.addCleanup(self._restore)

    def _restore(self):
        if self._saved is None:
            os.environ.pop("AIPORT_HWDEC", None)
        else:
            os.environ["AIPORT_HWDEC"] = self._saved

    def test_rkmpp(self):
        os.environ["AIPORT_HWDEC"] = "rkmpp"
        self.assertEqual(av._hwdec_args("hevc"), ["-c:v", "hevc_rkmpp"])
        self.assertEqual(av._hwdec_args("h264"), ["-c:v", "h264_rkmpp"])
        self.assertEqual(av._hwdec_args(None), ["-c:v", "h264_rkmpp"])
        self.assertEqual(av._hwdec_args("mjpeg"), [])

    def test_v4l2m2m(self):
        os.environ["AIPORT_HWDEC"] = "v4l2m2m"
        self.assertEqual(av._hwdec_args("h264"), ["-c:v", "h264_v4l2m2m"])
        self.assertEqual(av._hwdec_args("hevc"), ["-c:v", "hevc_v4l2m2m"])
        self.assertEqual(av._hwdec_args("mjpeg"), [])

    def test_videotoolbox(self):
        os.environ["AIPORT_HWDEC"] = "videotoolbox"
        self.assertEqual(av._hwdec_args("hevc"), ["-hwaccel", "videotoolbox"])
        # H.264 stays software: VideoToolbox dropped pictures on a relayed stream.
        self.assertEqual(av._hwdec_args("h264"), [])
        self.assertEqual(av._hwdec_args(None), [])
        self.assertEqual(av._hwdec_args("mjpeg"), [])

    def test_auto_on_macos_is_videotoolbox(self):
        os.environ.pop("AIPORT_HWDEC", None)
        with mock.patch.object(av.sys, "platform", "darwin"), \
                mock.patch.object(av, "_is_pi4", return_value=False):
            self.assertEqual(av._hwdec_args("hevc"), ["-hwaccel", "videotoolbox"])

    def test_auto_on_plain_linux_is_software(self):
        os.environ.pop("AIPORT_HWDEC", None)
        with mock.patch.object(av.sys, "platform", "linux"), \
                mock.patch.object(av, "_is_pi4", return_value=False):
            self.assertEqual(av._hwdec_args("h264"), [])

    def test_none_is_software(self):
        os.environ["AIPORT_HWDEC"] = "none"
        self.assertEqual(av._hwdec_args("hevc"), [])
        self.assertEqual(av._hwdec_args("h264"), [])

    def test_auto_prefers_ffmpeg_rkmpp(self):
        os.environ.pop("AIPORT_HWDEC", None)
        old = av.FFMPEG_BIN
        av.FFMPEG_BIN = "/usr/local/ffmpeg-rkmpp/bin/ffmpeg"
        try:
            self.assertEqual(av._hwdec_args("h264"), ["-c:v", "h264_rkmpp"])
        finally:
            av.FFMPEG_BIN = old


class Pi4Detection(unittest.TestCase):
    """v4l2m2m is Pi 4 (BCM2711) only -- the Pi 5 has no H.264 hardware decoder."""

    def _compat(self, data):
        f = tempfile.NamedTemporaryFile(delete=False)
        f.write(data)
        f.close()
        self.addCleanup(os.unlink, f.name)
        return f.name

    def test_bcm2711_is_pi4(self):
        self.assertTrue(av._is_pi4(self._compat(b"brcm,bcm2711\x00raspberrypi,4-model-b")))

    def test_bcm2712_is_not_pi4(self):
        self.assertFalse(av._is_pi4(self._compat(b"brcm,bcm2712\x00raspberrypi,5-model-b")))

    def test_missing_compatible_is_false(self):
        self.assertFalse(av._is_pi4("/nonexistent/compatible"))


try:
    import numpy as _np
except Exception:  # numpy is a project dep but keep the suite runnable without it
    _np = None


@unittest.skipIf(_np is None, "numpy unavailable")
@unittest.skipUnless(_CV2, "OpenCV not installed (the motion gate falls back to an average diff)")
class MotionGate(unittest.TestCase):
    """_scene_changed asks Frigate's motion detector (motion.py) whether this frame moved."""

    def setUp(self):
        for table in (av._latest_frame, av._motion_detectors, av._motion_settings,
                      av._motion_boxes, av._stream_dims):
            self.addCleanup(table.clear)
            table.clear()

    def frame(self, value=100, square=None):
        f = _np.full((640, 640, 3), value, _np.uint8)
        if square:
            x, y, size = square
            f[y:y + size, x:x + size] = 250
        av._latest_frame["d"] = f
        return av._scene_changed("d")

    def test_static_scene_has_no_motion(self):
        for _ in range(5):
            self.assertFalse(self.frame())

    def test_small_mover_is_motion(self):
        # ~0.5% of the frame, like a person walking through Camera C's background.
        for _ in range(20):
            self.frame()
        self.assertTrue(self.frame(square=(500, 100, 45)))
        (x1, y1, x2, y2), = av._motion_boxes["d"]
        self.assertTrue(x1 <= 500 and x2 >= 545 and y1 <= 100 and y2 >= 145)

    def test_whole_frame_change_is_lighting_not_motion(self):
        for _ in range(5):
            self.frame(60)
        self.assertFalse(self.frame(220))           # recalibrates instead
        self.assertTrue(av._motion_detectors["d"].is_calibrating())

    def test_motion_zones_mask_the_rest(self):
        av._apply_motion_settings({"deviceID": "d", "zones": {"1": {
            "coord": [0, 0, 500, 0, 500, 1000, 0, 1000], "level": 50}}})
        for _ in range(20):
            self.frame()
        self.assertFalse(self.frame(square=(560, 300, 45)))   # right half isn't watched
        self.assertTrue(self.frame(square=(100, 300, 45)))

    def test_protect_sensitivity_sets_threshold(self):
        av._apply_motion_settings({"deviceID": "d", "zones": {"1": {
            "coord": [0, 0, 1000, 0, 1000, 1000, 0, 1000], "level": 80}}})
        self.frame()
        self.assertEqual(av._motion_detectors["d"].threshold, 12)
        self.assertIsNone(av._motion_detectors["d"].mask)   # whole-frame zone: no mask

    def test_no_frame_is_no_change(self):
        self.assertFalse(av._scene_changed("absent"))


def _fake_jpeg(body):
    """SOI, a table segment holding FF D9, a start-of-scan header, `body`, EOI."""
    dqt = b"\xff\xdb\x00\x06\x00\xff\xd9\x01"
    sos = b"\xff\xda\x00\x04\x01\x02"
    return b"\xff\xd8" + dqt + sos + body + b"\xff\xd9"


class _Chunks:
    """A pipe that hands out its bytes `size` at a time."""

    def __init__(self, data, size):
        self.data, self.size = data, size

    def read1(self, _n):
        chunk, self.data = self.data[:self.size], self.data[self.size:]
        return chunk


class TestMotionGateDefault(unittest.TestCase):
    @unittest.skipIf("AIPORT_MOTION_GATE" in os.environ, "set in the environment")
    def test_on_everywhere(self):
        self.assertTrue(av.MOTION_GATE)



try:
    import norfair  # noqa: F401
except ImportError:
    norfair = None


@unittest.skipIf(norfair is None, "norfair not installed")
class TestCameraEventNorfair(TestCameraEvent):
    """Every camera-event test again, with Norfair deciding which detection is which track."""

    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(av, "TRACKER", "norfair")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_two_people_crossing_keep_their_ids(self):
        # The built-in matcher swaps them where their boxes overlap at 20+ units a frame;
        # Norfair's motion prediction carries each one through.
        steps = 24
        for i in range(steps):
            a = dict(self.PERSON, coord=[200 + i * 20, 300, 40, 120])
            b = dict(self.PERSON, coord=[200 + (steps - 1 - i) * 20, 305, 40, 120])
            self.tick(a, b)
            tracks = av._motion_state["D"]["tracks"]
            self.assertEqual(tracks[1]["coord"][0], a["coord"][0])
            self.assertEqual(tracks[2]["coord"][0], b["coord"][0])


class TestFrameJpegs(unittest.TestCase):
    def setUp(self):
        self.addCleanup(av._frame_jpegs.clear)
        self.addCleanup(av._latest_frame.clear)
        self.addCleanup(av._latest_frame_seq.clear)

    def test_split_survives_tables_and_chunking(self):
        a, b = _fake_jpeg(b"\x10\xff\x00\x20"), _fake_jpeg(b"\x30" * 50)
        for size in (1, 3, 7, 1 << 16):
            self.assertEqual(list(av._jpeg_frames(_Chunks(b"junk" + a + b, size))), [a, b])

    def test_split_drops_an_unfinished_tail(self):
        a = _fake_jpeg(b"\x10")
        self.assertEqual(list(av._jpeg_frames(_Chunks(a + a[:-2], 5))), [a])

    def test_frame_jpeg_finds_its_frame(self):
        av._frame_jpegs["D"] = [(4, b"four"), (5, b"five")]
        self.assertEqual(av._frame_jpeg("D", 5, timeout=0), b"five")
        self.assertIsNone(av._frame_jpeg("D", 3, timeout=0))  # already dropped
        self.assertIsNone(av._frame_jpeg("absent", 1, timeout=0))

    def test_frame_jpeg_waits_for_a_late_encode(self):
        jpegs = [(1, b"one")]
        av._frame_jpegs["D"] = jpegs

        def arrive():
            time.sleep(0.05)
            with av._frame_cond:
                jpegs.append((2, b"two"))
                av._frame_cond.notify_all()

        threading.Thread(target=arrive).start()
        self.assertEqual(av._frame_jpeg("D", 2, timeout=2), b"two")

    def test_detections_carry_their_frame(self):
        import numpy as _np
        av._latest_frame["D"] = _np.zeros((640, 640, 3), _np.uint8)
        av._latest_frame_seq["D"] = 7
        av._frame_jpegs["D"] = [(6, b"six"), (7, b"seven")]
        det = mock.Mock()
        det.detect.return_value = [{"objectType": "person", "score": 0.9, "label": "person",
                                    "box": [10, 10, 100, 200]}]
        with mock.patch.object(av, "_get_detector", return_value=det):
            out = av._classify_frame_detections("D")
        self.assertEqual(out[0]["frame"], (7, b"seven"))
        tr = {}
        av._capture_best("D", tr, out[0])
        self.assertEqual(tr["best"]["jpeg"], b"seven")

    def test_parked_objects_alone_dont_wait_for_the_jpeg(self):
        import numpy as _np
        av._latest_frame["D"] = _np.zeros((640, 640, 3), _np.uint8)
        av._latest_frame_seq["D"] = 7
        det = mock.Mock()
        det.detect.return_value = []
        seeded = [{"objectType": "vehicle", "score": 0.9, "box": [10, 10, 100, 200], "seeded": 1}]
        with mock.patch.object(av, "_get_detector", return_value=det), \
                mock.patch.object(av, "REGIONS", True), \
                mock.patch.object(av, "_detect_regions", return_value=seeded), \
                mock.patch.object(av, "_frame_jpeg") as frame_jpeg:
            out = av._classify_frame_detections("D")
        self.assertEqual(out[0]["seeded"], 1)
        frame_jpeg.assert_not_called()

    def test_frames_between_jpegs_have_none(self):
        self.addCleanup(av._frame_jpeg_every.clear)
        av._frame_jpeg_every["D"] = 2
        av._frame_jpegs["D"] = [(5, b"five"), (7, b"seven")]
        self.assertEqual(av._frame_jpeg("D", 7, timeout=0), b"seven")
        start = time.monotonic()
        self.assertIsNone(av._frame_jpeg("D", 8, timeout=2))  # never encoded: no wait
        self.assertLess(time.monotonic() - start, 0.5)

    def test_a_frame_without_a_jpeg_keeps_the_best_one(self):
        av._frame_jpegs["D"] = []
        tr = {}
        av._capture_best("D", tr, {"score": 0.6, "coord": [100, 200, 100, 300], "frame": (1, b"a")})
        with mock.patch.object(av, "_read_snapshot", return_value=(0, b"snap")):
            av._capture_best("D", tr, {"score": 0.9, "coord": [100, 200, 100, 300]})
        self.assertEqual(tr["best"]["jpeg"], b"a")
        with mock.patch.object(av, "_read_snapshot", return_value=(0, b"snap")):
            av._capture_best("D", {}, {"score": 0.9, "coord": [100, 200, 100, 300]})


class TestRedoReuse(unittest.TestCase):
    def setUp(self):
        self.addCleanup(av._redo_cache.clear)
        cut = {"label": "car", "score": 0.9, "box": (100, 0, 200, 80)}
        whole = {"label": "car", "score": 0.9, "box": (100, 0, 200, 150)}
        person = {"label": "person", "score": 0.8, "box": (220, 40, 260, 140)}
        self.cut, self.whole, self.person = cut, whole, person

    def test_only_the_whole_object_is_kept(self):
        self.assertIs(av._whole_of(self.cut, [self.person, self.whole]), self.whole)
        self.assertIsNone(av._whole_of(self.cut, [self.person]))

    def test_a_still_cut_off_box_reuses_its_second_look(self):
        av._redo_cache["D"] = [(100.0, "car", self.cut["box"], [self.whole])]
        self.assertEqual(av._reused_redo("D", self.cut, 105.0, []), [self.whole])

    def test_motion_on_it_or_time_up_looks_again(self):
        av._redo_cache["D"] = [(100.0, "car", self.cut["box"], [self.whole])]
        self.assertIsNone(av._reused_redo("D", self.cut, 105.0, [(150, 20, 170, 60)]))
        self.assertIsNone(av._reused_redo("D", self.cut, 100.0 + av.REDO_REUSE_S, []))
        moved = dict(self.cut, box=(140, 0, 240, 80))
        self.assertIsNone(av._reused_redo("D", moved, 105.0, []))


class TestPullVideoArgs(unittest.TestCase):
    def setUp(self):
        self.addCleanup(av._hw_scale_failed.clear)

    def _args(self, codec, device_id=None):
        with mock.patch.object(av, "HW_SCALE", True), \
                mock.patch.object(av.sys, "platform", "darwin"), \
                mock.patch.object(av, "_detect_hwdec", return_value="videotoolbox"), \
                mock.patch.object(av, "FRAME_JPEG_EVERY", 2):
            return av._pull_video_args(codec, 1280, 720, device_id)

    def test_hevc_is_scaled_on_the_gpu_it_was_decoded_on(self):
        pre, graph, hw, every = self._args("hevc")
        self.assertEqual(pre, ["-hwaccel", "videotoolbox",
                               "-hwaccel_output_format", "videotoolbox_vld"])
        self.assertEqual(hw, "vt")
        self.assertIn("[detin]scale_vt=1280:720,hwdownload,format=nv12[det]", graph)
        self.assertIn("[jpgin]select=not(mod(n\\,2)),hwdownload,format=nv12[jpg]", graph)
        self.assertEqual(every, 2)

    def test_software_decoded_h264_is_uploaded_for_scaling(self):
        pre, graph, hw, _ = self._args("h264")
        self.assertEqual(pre, ["-init_hw_device", "videotoolbox=vt", "-filter_hw_device", "vt"])
        self.assertEqual(hw, "vt_upload")
        self.assertIn("format=nv12,hwupload,scale_vt=1280:720,hwdownload", graph)
        self.assertIn("[jpgin]select=not(mod(n\\,2))[jpg]", graph)

    def test_a_camera_that_failed_on_the_gpu_scales_on_the_cpu(self):
        av._hw_scale_failed.add("D")
        pre, graph, hw, _ = self._args("hevc", "D")
        self.assertEqual(pre, ["-hwaccel", "videotoolbox"])
        self.assertIsNone(hw)
        self.assertIn("[detin]scale=1280:720[det]", graph)

    def test_a_stopped_stream_is_not_restarted(self):
        self.addCleanup(av._stream_points.pop, "S", None)
        av._active_streams.pop("S", None)
        av._stream_points["S"] = 2.5
        self.assertTrue(av._restart_wanted("S"))       # died on its own, still wanted
        av._stream_points.pop("S")                      # Protect stopped it meanwhile
        self.assertFalse(av._restart_wanted("S"))

    def test_only_gpu_errors_turn_gpu_scaling_off(self):
        self.assertTrue(av._gpu_scaling_error(
            "Impossible to convert between the formats supported by the filter "
            "'Parsed_scale_vt_0' and the filter 'auto_scale_0'"))
        for stream_error in ("[in#0/rtsp @ 0x7a] Failed reading RTSP data: End of file",
                             "Connection to tcp://192.168.1.42:554 failed: No route to host",
                             "", None):
            self.assertFalse(av._gpu_scaling_error(stream_error))

    def test_off_elsewhere_and_every_frame_without_a_rate(self):
        with mock.patch.object(av, "HW_SCALE", True), \
                mock.patch.object(av.sys, "platform", "linux"), \
                mock.patch.object(av, "_detect_hwdec", return_value=None):
            pre, graph, hw, _ = av._pull_video_args("h264", 640, 640)
        self.assertEqual((pre, hw), ([], None))
        self.assertEqual(av._detect_filter_graph(640, 640),
                         "[0:v]fps=%d,split=2[jpgin][detin];[jpgin]null[jpg];"
                         "[detin]scale=640:640[det]" % av.DETECT_FPS)


class TestTmpDir(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root)
        patcher = mock.patch.object(av, "_STREAM_DIR", self.root)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_reset_empties_the_whole_stream_dir(self):
        # A restart leaves nothing behind: scratch files and the snapshot of a camera that
        # is no longer paired (a paired camera's is rewritten once its stream starts).
        for path in (os.path.join(self.root, "GONE.jpg"), os.path.join(av._tmp_dir(), "tmpab.jpg")):
            open(path, "wb").close()
        av._reset_stream_dir()
        self.assertEqual(os.listdir(self.root), ["tmp"])
        self.assertEqual(os.listdir(av._tmp_dir()), [])

    def test_reset_creates_the_folder(self):
        shutil.rmtree(self.root)
        av._reset_stream_dir()
        self.assertTrue(os.path.isdir(av._tmp_dir()))

    def test_write_snapshot_leaves_no_temp(self):
        av._reset_stream_dir()
        path = os.path.join(self.root, "D.jpg")
        av._write_snapshot(path, b"one")
        av._write_snapshot(path, b"two")
        with open(path, "rb") as f:
            self.assertEqual(f.read(), b"two")
        self.assertEqual(os.listdir(av._tmp_dir()), [])

    def test_tmp_dir_follows_the_state_dir(self):
        a = config.stream_dir("/srv/aiport-a")
        b = config.stream_dir("/srv/aiport-b")
        self.assertNotEqual(a, b)
        with mock.patch.object(av, "_STREAM_DIR", a):
            self.assertEqual(av._tmp_dir(), os.path.join(a, "tmp"))


@unittest.skipUnless(_FFMPEG, "ffmpeg not installed")
class TestFramePairing(unittest.TestCase):
    def test_jpeg_n_is_raw_frame_n(self):
        """The pipeline's two outputs number frames alike: JPEG n, scaled down, is raw frame n."""
        import numpy as _np
        try:
            import cv2
        except ImportError:
            self.skipTest("OpenCV not installed")
        size, fps = 64, 5
        r, w = os.pipe()
        cmd = [_FFMPEG, "-loglevel", "error", "-f", "lavfi", "-i",
               "testsrc2=size=320x240:rate=25:duration=3",
               "-filter_complex", f"[0:v]fps={fps},split=2[jpg][detin];"
               f"[detin]scale={size}:{size}[det]",
               "-map", "[jpg]", "-c:v", "mjpeg", "-f", "image2pipe", f"pipe:{w}",
               "-map", "[det]", "-pix_fmt", "rgb24", "-f", "rawvideo", "-"]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, pass_fds=(w,))
        os.close(w)
        jpegs = []
        def read_jpegs():
            with os.fdopen(r, "rb") as pipe:
                jpegs.extend(av._jpeg_frames(pipe))

        reader = threading.Thread(target=read_jpegs)
        reader.start()
        raw = proc.stdout.read()
        proc.stdout.close()
        proc.wait()
        reader.join()
        frames = [_np.frombuffer(raw[i:i + size * size * 3], _np.uint8).reshape(size, size, 3)
                  for i in range(0, len(raw), size * size * 3)]
        self.assertEqual(len(jpegs), len(frames))
        self.assertGreater(len(frames), 10)
        for n, jpeg in enumerate(jpegs):
            img = cv2.cvtColor(cv2.imdecode(_np.frombuffer(jpeg, _np.uint8), cv2.IMREAD_COLOR),
                               cv2.COLOR_BGR2RGB)
            small = cv2.resize(img, (size, size), interpolation=cv2.INTER_AREA).astype(int)
            diffs = [_np.abs(small - f.astype(int)).mean() for f in frames]
            self.assertEqual(int(_np.argmin(diffs)), n)


class TestProbeParse(unittest.TestCase):
    def test_full(self):
        self.assertEqual(av._parse_probe("h264,3840,2160,12/1\n"),
                         {"codec": "h264", "width": 3840, "height": 2160, "fps": 12})

    def test_fractional_and_partial(self):
        self.assertEqual(av._parse_probe("HEVC,1920,1080,30000/1001")["fps"], 30)
        self.assertEqual(av._parse_probe("h264,,,0/0"),
                         {"codec": "h264", "width": None, "height": None, "fps": None})
        self.assertEqual(av._parse_probe(""),
                         {"codec": None, "width": None, "height": None, "fps": None})


if __name__ == "__main__":
    unittest.main()
