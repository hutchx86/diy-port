# SPDX-License-Identifier: AGPL-3.0-or-later
"""stationary.py: Frigate's pixel check for parked objects, on synthetic frames."""
import os
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

try:
    import numpy as np
    import stationary
except ImportError:  # no OpenCV/numpy on this host
    stationary = None

BOX = (200, 200, 360, 300)


def scene(car_at=200, noise=0, seed=1):
    """Gray street with a textured 'car' whose left edge is at x=car_at."""
    rng = np.random.default_rng(seed)
    f = np.full((640, 640), 90, np.uint8)
    car = (rng.random((100, 160)) * 200 + 30).astype(np.uint8)
    f[200:300, car_at:car_at + 160] = car
    if noise:
        f = np.clip(f.astype(int) + rng.integers(-noise, noise + 1, f.shape), 0, 255).astype(np.uint8)
    return f


@unittest.skipIf(stationary is None, "stationary.py needs OpenCV and numpy")
class TestStillThere(unittest.TestCase):
    def setUp(self):
        self.state = stationary.anchor(scene(), BOX)

    def test_same_spot_stays(self):
        for _ in range(5):
            self.assertTrue(stationary.still_there(self.state, scene()))

    def test_sensor_noise_stays(self):
        for seed in range(5):
            self.assertTrue(stationary.still_there(self.state, scene(noise=6, seed=1)))

    def test_car_gone_flips(self):
        empty = np.full((640, 640), 90, np.uint8)
        results = [stationary.still_there(self.state, empty) for _ in range(2)]
        self.assertFalse(results[-1])

    def test_car_moved_flips(self):
        moved = scene(car_at=240)
        results = [stationary.still_there(self.state, moved) for _ in range(2)]
        self.assertFalse(results[-1])

    def test_one_frame_of_occlusion_doesnt_flip(self):
        # Someone passing in front of a quarter of the car for a frame, then gone again.
        occluded = scene()
        occluded[200:300, 200:240] = 60
        self.assertTrue(stationary.still_there(self.state, occluded))
        self.assertTrue(stationary.still_there(self.state, scene()))
        self.assertEqual(self.state["changed"], 0)
        self.assertTrue(stationary.still_there(self.state, occluded))   # counting from 0 again

if __name__ == "__main__":
    unittest.main()
