import os
import shutil
import stat
import sys
import tempfile
import unittest
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

os.environ.setdefault("AIPORT_TICK_S", "0.33")       # tests assume macOS's tick and
os.environ.setdefault("AIPORT_DETECT_FPS", "10")    # detection rate on every platform
try:
    import cv2
    import numpy as np
    import avclient as av
except Exception as exc:  # pragma: no cover
    av = None
    _ERR = exc


def jpeg(w=400, h=300):
    img = np.zeros((h, w, 3), np.uint8)
    img[100:200, 150:250] = 255           # the "face": a white square
    return cv2.imencode(".jpg", img)[1].tobytes()


@unittest.skipIf(av is None, "cv2/avclient unavailable")
class TestFacesSeen(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.seen = os.path.join(self.dir, "faces-seen")
        self.faces = os.path.join(self.dir, "faces")
        patcher = mock.patch.dict(os.environ, {"AIPORT_FACES_SEEN_DIR": self.seen,
                                               "AIPORT_FACES_DIR": self.faces})
        patcher.start()
        self.addCleanup(patcher.stop)

    def face_track(self, score=0.88, coord=(375, 333, 250, 333)):
        return {"objectType": "face", "best": {"jpeg": jpeg(), "coord": list(coord),
                                               "score": score, "wall": 1_792_500_000_000}}

    def test_best_crop_is_saved_with_a_margin(self):
        av._save_face_crop("CAM", 7, self.face_track())
        files = os.listdir(self.seen)
        self.assertEqual(len(files), 1)
        self.assertRegex(files[0], r"^\d{8}-\d{6}_CAM_t7_0\.88\.jpg$")
        crop = cv2.imread(os.path.join(self.seen, files[0]))
        # face box 100 x 100 px plus half its size on each side
        self.assertEqual(crop.shape[:2], (200, 200))
        self.assertTrue(os.path.isdir(self.faces))     # the gallery folder is made too
        for path in (self.seen, self.faces):
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o777)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.seen, files[0])).st_mode), 0o666)

    def test_crop_is_clamped_to_the_frame(self):
        av._save_face_crop("CAM", 1, self.face_track(coord=(0, 0, 200, 200)))
        crop = cv2.imread(os.path.join(self.seen, os.listdir(self.seen)[0]))
        self.assertEqual(crop.shape[:2], (90, 120))    # 0..300*400/1000, 0..300*300/1000

    def test_only_faces_with_a_best_frame(self):
        av._save_face_crop("CAM", 1, {"objectType": "person", "best": {"jpeg": jpeg()}})
        av._save_face_crop("CAM", 2, {"objectType": "face"})
        self.assertFalse(os.path.exists(self.seen))

    def test_oldest_crops_pruned(self):
        with mock.patch.object(av, "FACES_SEEN_MAX", 3):
            for i in range(5):
                tr = self.face_track()
                tr["best"]["wall"] += i * 1000
                av._save_face_crop("CAM", i, tr)
        self.assertEqual(sorted(f.split("_t")[1][0] for f in os.listdir(self.seen)),
                         ["2", "3", "4"])


if __name__ == "__main__":
    unittest.main()
