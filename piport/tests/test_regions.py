# SPDX-License-Identifier: AGPL-3.0-or-later
"""regions.py (Frigate's region clustering and merging) and avclient's region detection."""
import importlib.util
import os
import sys
import unittest
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import numpy as np  # noqa: E402

os.environ.setdefault("AIPORT_TICK_S", "0.33")       # tests assume macOS's tick and
os.environ.setdefault("AIPORT_DETECT_FPS", "10")    # detection rate on every platform
import avclient as av  # noqa: E402
import regions  # noqa: E402

_CV2 = importlib.util.find_spec("cv2") is not None   # the Frigate motion gate, regions, stationary

SHAPE = (720, 1280, 3)


class TestRegions(unittest.TestCase):
    def test_region_is_square_and_at_least_the_model_size(self):
        x1, y1, x2, y2 = regions.calculate_region(SHAPE, 600, 300, 640, 400, 320)
        self.assertEqual((x2 - x1, y2 - y1), (320, 320))
        self.assertTrue(x1 <= 600 and x2 >= 640 and y1 <= 300 and y2 >= 400)

    def test_region_stays_inside_the_frame(self):
        self.assertEqual(regions.calculate_region(SHAPE, 1250, 700, 1270, 715, 320),
                         (960, 400, 1280, 720))
        x1, y1, x2, y2 = regions.calculate_region(SHAPE, 0, 0, 1200, 700, 320)
        self.assertEqual((x2 - x1, y2 - y1), (720, 720))   # never bigger than the short side

    def test_nearby_boxes_share_a_region_far_ones_dont(self):
        boxes = [(100, 100, 140, 200), (160, 110, 200, 210), (1000, 500, 1040, 600)]
        found = regions.regions_for(SHAPE, boxes)
        self.assertEqual(len(found), 2)
        for b in boxes:
            self.assertTrue(regions.inside_any(b, found))

    def test_startup_regions_cover_the_frame(self):
        found = regions.startup_regions(SHAPE)
        self.assertEqual(found[0][0], 0)
        self.assertEqual(found[-1][2], 1280)
        self.assertTrue(all(r[3] - r[1] == 720 and r[2] - r[0] == 720 for r in found))
        portrait = regions.startup_regions((1280, 956, 3))
        self.assertEqual((portrait[0][1], portrait[-1][3]), (0, 1280))

    def test_same_object_from_two_regions_is_one_detection(self):
        a = {"label": "person", "score": 0.9, "box": (500, 200, 560, 380), "region": (400, 100, 720, 420)}
        b = {"label": "person", "score": 0.8, "box": (502, 198, 561, 382), "region": (450, 150, 770, 470)}
        self.assertEqual(regions.reduce_detections(SHAPE, [a, b]), [a])

    def test_clipped_box_loses_to_a_whole_one(self):
        # Seen whole in one region (0.7); cut at the edge of another (0.9).
        whole = {"label": "car", "score": 0.7, "box": (300, 300, 500, 420), "region": (240, 200, 560, 520)}
        cut = {"label": "car", "score": 0.9, "box": (300, 300, 400, 420), "region": (100, 200, 400, 500)}
        self.assertTrue(regions.clipped(cut, SHAPE))
        self.assertEqual(regions.reduce_detections(SHAPE, [whole, cut]), [whole])

    def test_box_inside_a_bigger_same_label_box_is_dropped(self):
        car = {"label": "car", "score": 0.7, "box": (300, 300, 600, 500), "region": (0, 0, 720, 720)}
        half = {"label": "car", "score": 0.7, "box": (300, 400, 450, 500), "region": (0, 0, 720, 720)}
        self.assertEqual(regions.reduce_detections(SHAPE, [car, half]), [car])

    def test_upper_body_inside_the_same_persons_full_box_is_dropped(self):
        # Live 11:33 at Camera A: the upper-body box was 89% inside the full-body box (its
        # top above it) and became a second person at Frigate's 0.9.
        full = {"label": "person", "score": 0.9, "box": (349, 229, 670, 970), "region": (0, 0, 720, 720)}
        upper = {"label": "person", "score": 0.95, "box": (441, 203, 553, 432), "region": (0, 0, 720, 720)}
        self.assertEqual(regions.reduce_detections(SHAPE, [full, upper]), [full])

    def test_two_people_side_by_side_are_kept(self):
        a = {"label": "person", "score": 0.9, "box": (300, 200, 450, 700), "region": (0, 0, 720, 720)}
        b = {"label": "person", "score": 0.9, "box": (400, 220, 550, 700), "region": (0, 0, 720, 720)}
        self.assertEqual(len(regions.reduce_detections(SHAPE, [a, b])), 2)

    def test_small_car_behind_a_big_one_is_kept(self):
        near = {"label": "car", "score": 0.8, "box": (0, 200, 1000, 719), "region": (0, 0, 720, 720)}
        far = {"label": "car", "score": 0.8, "box": (400, 210, 470, 250), "region": (0, 0, 720, 720)}
        self.assertEqual(len(regions.reduce_detections(SHAPE, [near, far])), 2)


class SpotDetector:
    """Reports white pixels as one person, in the coordinates of the frame it's given, and
    records each frame's shape (a region crop, or the whole frame)."""

    def __init__(self):
        self.calls = []

    def detect(self, frame, min_score=None):
        self.calls.append(frame.shape[:2])
        ys, xs = np.nonzero(frame[:, :, 0] > 200)
        if len(xs) < 4:
            return []
        return [{"objectType": "person", "label": "person", "score": 0.9,
                 "box": (float(xs.min()), float(ys.min()), float(xs.max()), float(ys.max()))}]


class TestRegionDetection(unittest.TestCase):
    def setUp(self):
        self.det = SpotDetector()
        for name, value in (("REGIONS", True), ("_detector", self.det),
                            ("_frame_jpeg", lambda *a, **k: None)):
            p = mock.patch.object(av, name, value)
            p.start()
            self.addCleanup(p.stop)
        for table in (av._latest_frame, av._motion_detectors, av._motion_boxes,
                      av._motion_state, av._last_parked_check, av._stream_dims):
            table.pop("R", None)
            self.addCleanup(table.pop, "R", None)
        av._startup_scan["R"] = {"first": -1e9, "last": None}   # startup window over
        self.addCleanup(av._startup_scan.pop, "R", None)

    def frame(self, spot=None):
        f = np.full(SHAPE, 90, np.uint8)
        if spot:
            x, y = spot
            f[y:y + 24, x:x + 10] = 255   # a distant person, ~0.03% of the frame
        av._latest_frame["R"] = f

    @unittest.skipUnless(_CV2, "OpenCV not installed (the motion gate)")
    def test_distant_person_found_in_a_region_at_native_size(self):
        for _ in range(20):
            self.frame()
            av._scene_changed("R")
        self.frame(spot=(1000, 150))
        self.assertTrue(av._scene_changed("R"))
        out = av._classify_frame_detections("R")
        self.assertEqual(len(out), 1)
        self.assertEqual(self.det.calls, [(320, 320)])            # one crop, not the frame
        x, y, w, h = out[0]["coord"]
        self.assertAlmostEqual(x, 1000 / 1280 * 1000, delta=2)    # back in frame coords
        self.assertAlmostEqual(y, 150 / 720 * 1000, delta=3)

    def test_box_cut_off_by_its_crop_is_looked_at_again(self):
        # Live 12:59 on Camera B: a crop placed for a road car cut through a large object at
        # the top, and its cut-off box took over the car's track.
        f = np.full(SHAPE, 90, np.uint8)
        f[100:400, 300:700] = 255                      # a large object
        av._latest_frame["R"] = f

        class Calibrated:
            def is_calibrating(self):
                return False
        av._motion_detectors["R"] = Calibrated()
        av._motion_boxes["R"] = [(640, 200, 660, 220)]   # motion at its right side only
        out = av._detect_regions("R", self.det, f)
        self.assertEqual(len(out), 1)
        x1, y1, x2, y2 = out[0]["box"]
        self.assertEqual((round(x1), round(y1), round(x2), round(y2)), (300, 100, 699, 399))
        self.assertFalse(out[0]["clipped"])
        # the motion crop; a look twice that box's size (still cut: it was cut on two sides);
        # one more, which sees it whole
        self.assertEqual(len(self.det.calls), 3)

    def test_no_motion_no_tracks_no_inference(self):
        self.frame(spot=(1000, 150))
        av._motion_boxes["R"] = []
        self.assertEqual(av._classify_frame_detections("R"), [])
        self.assertEqual(self.det.calls, [])

    def test_startup_looks_at_the_whole_frame(self):
        av._startup_scan.pop("R", None)
        self.frame(spot=(1000, 150))
        out = av._classify_frame_detections("R")
        self.assertEqual(len(out), 1)
        self.assertTrue(all(c == (720, 720) for c in self.det.calls))

    def test_startup_look_repeats_through_the_startup_window(self):
        # One coarse look missed a parked car at Camera A; keep looking for 15 s.
        av._startup_scan.pop("R", None)
        self.frame()
        av._motion_boxes["R"] = []
        clock = [1000.0]
        with mock.patch.object(av.time, "monotonic", lambda: clock[0]):
            looks = []
            for t in (0.0, 0.5, 1.1, 1.5, 2.2, 14.9, 15.5, 30.0):
                clock[0] = 1000.0 + t
                before = len(self.det.calls)
                av._classify_frame_detections("R")
                looks.append(len(self.det.calls) > before)
        self.assertEqual(looks, [True, False, True, False, True, True, False, False])

    def test_parked_object_is_seeded_between_rechecks(self):
        self.frame()
        av._motion_state["R"] = {"tracks": {1: {"objectType": "vehicle", "score": 0.66,
                                                "coord": [300, 400, 200, 150],
                                                "stationary_coord": [300, 400, 200, 150]}}}
        av._last_parked_check["R"] = av.time.monotonic()           # just rechecked
        out = av._classify_frame_detections("R")
        self.assertEqual(self.det.calls, [])
        self.assertEqual(len(out), 1)
        self.assertTrue(out[0]["seeded"])
        self.assertEqual([round(v) for v in out[0]["coord"]], [300, 400, 200, 150])

    def test_detect_dims_keep_the_aspect(self):
        self.assertEqual(av._detect_dims(2560, 1440), (1280, 720))
        self.assertEqual(av._detect_dims(1504, 2016), (954, 1280))
        self.assertEqual(av._detect_dims(640, 360), (640, 360))   # never upscaled
        with mock.patch.object(av, "REGIONS", False):
            self.assertEqual(av._detect_dims(2560, 1440), (640, 640))


if __name__ == "__main__":
    unittest.main()
