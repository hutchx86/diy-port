# SPDX-License-Identifier: AGPL-3.0-or-later
"""motion.py: Frigate's motion detector on synthetic frames."""
import os
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

try:
    import numpy as np
    import motion
except ImportError as e:  # no OpenCV/numpy on this host
    motion, _ERR = None, e


def gray(value=100, square=None, size=(360, 640)):
    f = np.full(size, value, np.uint8)
    if square:
        x, y, s = square
        f[y:y + s, x:x + s] = 250
    return f


@unittest.skipIf(motion is None, "motion.py needs OpenCV and numpy")
class TestMotionDetector(unittest.TestCase):
    def setUp(self):
        self.det = motion.MotionDetector((360, 640))
        for _ in range(20):            # it learns the background in ~6 frames (alpha 0.2)
            self.det.detect(gray())

    def test_calibrates_on_a_still_scene(self):
        self.assertFalse(self.det.is_calibrating())
        self.assertEqual(self.det.detect(gray()), [])

    def test_box_around_a_small_mover(self):
        boxes = self.det.detect(gray(square=(400, 100, 20)))
        self.assertEqual(len(boxes), 1)
        x1, y1, x2, y2 = boxes[0]
        self.assertTrue(x1 <= 400 <= 420 <= x2 and y1 <= 100 <= 120 <= y2)

    def test_tiny_flicker_is_ignored(self):
        self.assertEqual(self.det.detect(gray(square=(400, 100, 1))), [])  # under contour_area

    def test_small_brightness_drift_is_ignored(self):
        for _ in range(300):          # calibrated, the background creeps at 1% a frame
            self.det.detect(gray())
        self.assertEqual(self.det.detect(gray(110)), [])   # under the pixel threshold

    def test_lighting_change_recalibrates(self):
        self.det.detect(gray(230))
        self.assertTrue(self.det.is_calibrating())

    def test_background_absorbs_a_lasting_change(self):
        frame = gray(square=(400, 100, 30))
        for _ in range(400):             # an object that stays becomes background
            boxes = self.det.detect(frame)
        self.assertEqual(boxes, [])

    def test_mask(self):
        mask = np.full((360, 640), 255, np.uint8)
        mask[:, 320:] = 0
        self.det.set_mask(mask)
        for _ in range(20):            # it learns the background in ~6 frames (alpha 0.2)
            self.det.detect(gray())
        self.assertEqual(self.det.detect(gray(square=(400, 100, 20))), [])
        self.assertEqual(len(self.det.detect(gray(square=(100, 100, 20)))), 1)


@unittest.skipIf(motion is None, "motion.py needs OpenCV and numpy")
class TestProtectSettings(unittest.TestCase):
    def test_threshold_for_level(self):
        self.assertEqual(motion.threshold_for_level(50), 30)   # Protect's default = Frigate's
        self.assertEqual(motion.threshold_for_level(100), 5)
        self.assertEqual(motion.threshold_for_level(0), 60)
        self.assertEqual(motion.threshold_for_level(None), 30)

    def test_zones_mask(self):
        ident = lambda x, y: (x, y)  # noqa: E731
        self.assertIsNone(motion.zones_mask(None, (100, 100), ident))
        full = {"1": {"coord": [0, 0, 1000, 0, 1000, 1000, 0, 1000]}}
        self.assertIsNone(motion.zones_mask(full, (100, 100), ident))
        left = {"1": {"coord": [0, 0, 500, 0, 500, 1000, 0, 1000]}}
        mask = motion.zones_mask(left, (100, 100), ident)
        self.assertEqual((mask[50, 20], mask[50, 80]), (255, 0))


if __name__ == "__main__":
    unittest.main()
