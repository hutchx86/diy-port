# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 hutchx86
"""Unit tests for enrich.py (vehicle attributes) and avclient's enrichment
plumbing; the plate/face model test runs only when its models are installed
and AIPORT_ENRICH_SAMPLES points at sample images."""
import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import numpy as np  # noqa: E402

import enrich  # noqa: E402


def _solid(rgb, size=64):
    return np.full((size, size, 3), rgb, dtype=np.uint8)


class TestDominantColor(unittest.TestCase):
    FULL = [0, 0, 1000, 1000]

    def check(self, rgb, expected):
        color, conf = enrich.dominant_color(_solid(rgb), self.FULL)
        self.assertEqual(color, expected, rgb)
        self.assertEqual(conf, 100)

    def test_basic_colors(self):
        self.check((200, 20, 20), "red")
        self.check((20, 40, 200), "blue")
        self.check((20, 160, 40), "green")
        self.check((230, 210, 30), "yellow")

    def test_neutrals(self):
        self.check((10, 10, 10), "black")
        self.check((245, 245, 245), "white")
        self.check((170, 170, 175), "silver")
        self.check((90, 90, 90), "gray")

    def test_brown(self):
        self.check((100, 60, 30), "brown")

    def test_empty_crop(self):
        self.assertIsNone(enrich.dominant_color(_solid((1, 2, 3)), [0, 0, 0, 0]))

    def test_uses_box_center_not_edges(self):
        frame = _solid((20, 40, 200), size=100)       # blue background
        frame[30:90, 20:80] = (200, 20, 20)            # red car body
        color, _ = enrich.dominant_color(frame, [200, 200, 600, 700])
        self.assertEqual(color, "red")


class TestVehicleAttributes(unittest.TestCase):
    def test_type_and_color(self):
        a = enrich.vehicle_attributes("car", _solid((200, 20, 20)), [0, 0, 1000, 1000])
        self.assertEqual(a["vehicleType"]["val"], "car")
        self.assertEqual(a["color"], {"val": "red", "confidence": 100})

    def test_protect_vocabulary(self):
        silver = enrich.vehicle_attributes("bicycle", _solid((170, 170, 175)), [0, 0, 1000, 1000])
        self.assertEqual(silver["vehicleType"]["val"], "bike")
        self.assertEqual(silver["color"]["val"], "gray")  # Protect has no "silver"
        for label in enrich.VEHICLE_TYPES:
            self.assertIn(enrich.VEHICLE_TYPES[label],
                          {"car", "suv", "truck", "van", "bus", "motorcycle", "bike"})

    def test_unknown_label_keeps_color(self):
        a = enrich.vehicle_attributes("train", _solid((10, 10, 10)), [0, 0, 1000, 1000])
        self.assertNotIn("vehicleType", a)
        self.assertEqual(a["color"]["val"], "black")

    def test_nothing(self):
        self.assertIsNone(enrich.vehicle_attributes("boat", None, [0, 0, 10, 10]))


class TestCoordHelpers(unittest.TestCase):
    def test_expand_and_back(self):
        x1, y1, x2, y2 = enrich._expand_px([250, 250, 500, 500], 2000, 1000, margin=1.0)
        self.assertEqual((x1, y1, x2, y2), (500, 250, 1500, 750))
        self.assertEqual(enrich._to_norm(x1, y1, x2, y2, 2000, 1000), [250, 250, 500, 500])

    def test_expand_clamps(self):
        self.assertEqual(enrich._expand_px([0, 0, 1000, 1000], 100, 100, margin=2.0),
                         (0, 0, 100, 100))


os.environ.setdefault("AIPORT_TICK_S", "0.33")       # tests assume macOS's tick and
os.environ.setdefault("AIPORT_DETECT_FPS", "10")    # detection rate on every platform
try:
    import avclient
    _AV_ERR = None
except Exception as exc:  # websockets/requests missing
    avclient = None
    _AV_ERR = exc


@unittest.skipIf(avclient is None, f"avclient unavailable ({_AV_ERR})")
class TestMergeExtra(unittest.TestCase):
    def test_plate_is_final_only_after_three_agreeing_reads(self):
        # Live 14:38 on Camera B: one confident misread (DERM782 at 0.90 for DEPM782) became
        # the plate, and the car, now "confidently" read, was never read again.
        tr = {"objectType": "vehicle"}
        avclient._merge_extra(tr, {"scored": {"name": ("ABRD123", 0.90)}})
        avclient._merge_extra(tr, {"scored": {"name": ("ABCD123", 0.89)}})
        self.assertNotIn("name", tr.get("extra", {}))          # not final: nothing reported
        self.assertNotIn("name", tr.get("extra_scores", {}))   # nor "confident" (keep reading)
        avclient._merge_extra(tr, {"scored": {"name": ("ABCD123", 0.88)}})
        self.assertEqual(tr["extra"]["name"], "ABCD123")       # the C wins the vote 2 to 1
        self.assertAlmostEqual(tr["extra_scores"]["name"], (0.90 + 0.89 + 0.88) / 3)

    def test_plate_vote_is_per_character(self):
        # Camera D 14:38 at 40 px: the same plate read with a different wrong letter each time.
        reads = [("DEPM782", 0.88), ("DEFM782", 0.87), ("DEPM732", 0.88)]
        text, conf, agreeing = avclient._plate_rep(reads)
        self.assertEqual((text, agreeing), ("DEPM782", 3))
        reads.append(("123", 0.93))                             # a partial read doesn't vote
        self.assertEqual(avclient._plate_rep(reads)[0], "DEPM782")

    def test_one_misread_does_not_replace_the_plate(self):
        # Live 10:14 on Camera B: 'ABED123' at 0.85 among 'ABCD123's; '123' at 0.93.
        tr = {"objectType": "vehicle"}
        for text, conf in [("ABCD123", 0.89), ("ABCD123", 0.90), ("ABED123", 0.85),
                           ("123", 0.93), ("ABCD123", 0.91)]:
            avclient._merge_extra(tr, {"scored": {"name": (text, conf)}})
        self.assertEqual(tr["extra"]["name"], "ABCD123")
        self.assertAlmostEqual(tr["extra_scores"]["name"], (0.89 + 0.90 + 0.85 + 0.91) / 4)

    def test_plate_logged_only_when_it_changes(self):
        tr = {"objectType": "vehicle"}
        with self.assertLogs("aiport-avclient", "INFO") as logs:
            for _ in range(4):
                avclient._merge_extra(tr, {"scored": {"name": ("ABCD123", 0.9)}}, "D", 7)
            avclient._merge_extra(tr, {"scored": {"name": ("ABED123", 0.95)}}, "D", 7)
        plate_lines = [m for m in logs.output if "plate" in m]
        self.assertEqual(len(plate_lines), 1)
        self.assertIn("'ABCD123' for vehicle tracker_id=7 deviceID=D", plate_lines[0])

    def test_jaro_winkler(self):
        jw = avclient._jaro_winkler
        self.assertEqual(jw("ABCD123", "ABCD123"), 1.0)
        self.assertAlmostEqual(jw("MARTHA", "MARHTA"), 0.9611, places=3)
        self.assertGreaterEqual(jw("ABCD123", "ABED123"), 0.85)
        self.assertLess(jw("ABCD123", "XYZ987"), 0.5)
        self.assertEqual(jw("", "A"), 0.0)

    def test_face_embed_keeps_best_face(self):
        tr = {"objectType": "face"}
        avclient._merge_extra(tr, {"extra": {"name": ""}, "scored": {"faceEmbed": ([1.0], 0.8)}})
        avclient._merge_extra(tr, {"extra": {"name": ""}, "scored": {"faceEmbed": ([2.0], 0.7)}})
        self.assertEqual(tr["extra"]["faceEmbed"], [1.0])
        self.assertNotIn("name", tr["extra"])

    def test_name_not_cleared_by_unmatched_tick(self):
        tr = {"objectType": "face", "extra": {"name": "Alex", "tag": "Alex"}}
        avclient._merge_extra(tr, {"extra": {"name": "", "tag": ""}})
        self.assertEqual(tr["extra"]["name"], "Alex")

    def test_attributes_and_plate_coexist(self):
        tr = {"objectType": "vehicle", "extra": {"attributes": {"color": {"val": "red"}}}}
        for _ in range(avclient.PLATE_AGREE_READS):
            avclient._merge_extra(tr, {"scored": {"name": ("XYZ9", 0.8)}})
        self.assertEqual(tr["extra"]["name"], "XYZ9")
        self.assertIn("attributes", tr["extra"])

    def test_descriptor_carries_extra(self):
        sent = []
        orig = avclient.send_msg
        avclient.send_msg = lambda ws, fn, payload, **kw: sent.append(payload)
        try:
            avclient._send_smart_detect_event(
                None, "DEV", edge_type="enter", object_type="vehicle",
                coord=[1, 2, 3, 4], confidence=0.9, descriptor_extra={"name": "ABC123"})
            avclient._send_smart_detect_event(
                None, "DEV", edge_type="enter", object_type="face",
                coord=[1, 2, 3, 4], confidence=0.9, descriptor_extra={"faceEmbed": [0.5, -0.25]})
        finally:
            avclient.send_msg = orig
        vehicle, face = (p["descriptors"][0] for p in sent)
        self.assertEqual((vehicle["objectType"], vehicle["name"]), ("vehicle", "ABC123"))
        self.assertNotIn("licensePlate", vehicle)
        self.assertEqual(face["faceEmbed"], [0.5, -0.25])


@unittest.skipIf(avclient is None, f"avclient unavailable ({_AV_ERR})")
class TestEnrichDetections(unittest.TestCase):
    """Plates land on the parent vehicle's `name`; faces become face detections."""

    class _Enricher:
        lpr = face = True

        def plates(self, img, coords):
            return [{"coord": [10, 10, 5, 5], "text": "ABC123", "confidence": 0.9, "parent": 0}]

        def faces(self, img, coords):
            return [{"coord": [20, 20, 5, 5], "score": 0.85, "name": "", "embedding": [8.0, 6.0],
                     "parent": 0}]

    def setUp(self):
        from unittest import mock
        for target, value in (("_get_enricher", lambda: self._Enricher()),
                              ("_load_snapshot_bgr", lambda dev: (np.zeros((4, 4, 3), np.uint8), 1.0))):
            patcher = mock.patch.object(avclient, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        avclient._enrich_cache.pop("DEV", None)
        self.addCleanup(avclient._enrich_cache.pop, "DEV", None)

    def _track(self, **kw):
        tr = {"objectType": "vehicle", "coord": [0, 0, 100, 100]}
        tr.update(kw)
        avclient._motion_state["DEV"] = {"tracks": {1: tr}}
        self.addCleanup(avclient._motion_state.pop, "DEV", None)

    def _plates_read(self):
        calls = []
        enricher = self._Enricher()
        enricher.plates = lambda img, coords: calls.append(coords) or []
        from unittest import mock
        with mock.patch.object(avclient, "_get_enricher", lambda: enricher):
            avclient._enrich_cache.pop("DEV", None)
            avclient._enrich_detections(
                "DEV", [{"objectType": "vehicle", "score": 0.8, "coord": [0, 0, 100, 100]}])
        return calls

    def test_parked_vehicle_not_reread(self):
        self._track(stationary_coord=[0, 0, 100, 100])
        self.assertEqual(self._plates_read(), [])

    def test_box_on_part_of_parked_vehicle_not_reread(self):
        # Live 10:14 on Camera B: a partial box (IoU under 0.5) on a parked car re-read its
        # plate on every snapshot.
        self._track(coord=[0, 0, 300, 300], stationary_coord=[0, 0, 300, 300])
        self.assertEqual(self._plates_read(), [])

    def test_vehicle_beside_parked_one_is_read(self):
        self._track(coord=[200, 0, 300, 300], stationary_coord=[200, 0, 300, 300])
        self.assertEqual(self._plates_read(), [[[0, 0, 100, 100]]])

    def test_just_parked_vehicle_still_read(self):
        # Frigate reads a parked car for 5 s after it stops.
        self._track(stationary_coord=[0, 0, 100, 100], parked_at_ms=1000, seen_ms=5000)
        self.assertEqual(self._plates_read(), [[[0, 0, 100, 100]]])
        self._track(stationary_coord=[0, 0, 100, 100], parked_at_ms=1000, seen_ms=6001)
        self.assertEqual(self._plates_read(), [])

    def test_still_vehicle_not_reread(self):
        self._track(still=True)
        self.assertEqual(self._plates_read(), [])

    def test_confident_plate_not_reread(self):
        self._track(extra_scores={"name": 0.9})
        self.assertEqual(self._plates_read(), [])

    def test_moving_vehicle_without_plate_is_read(self):
        self._track(extra_scores={"name": 0.6})
        self.assertEqual(self._plates_read(), [[[0, 0, 100, 100]]])

    def test_plate_and_face(self):
        vehicle = {"objectType": "vehicle", "score": 0.8, "coord": [0, 0, 100, 100]}
        person = {"objectType": "person", "score": 0.8, "coord": [0, 0, 100, 100]}
        extra = avclient._enrich_detections("DEV", [vehicle, person])
        self.assertEqual(vehicle["scored"]["name"], ("ABC123", 0.9))
        self.assertEqual([e["objectType"] for e in extra], ["face"])
        self.assertEqual(extra[0]["scored"]["faceEmbed"], ([8.0, 6.0], 0.85))


@unittest.skipIf(avclient is None, f"avclient unavailable ({_AV_ERR})")
class TestFaceDb(unittest.TestCase):
    def test_describe_sqlite(self):
        import sqlite3
        import tempfile
        path = os.path.join(tempfile.mkdtemp(), "known.db")
        self.addCleanup(os.remove, path)
        con = sqlite3.connect(path)
        con.execute("CREATE TABLE faces (subject_id INTEGER, embedding BLOB)")
        con.execute("INSERT INTO faces VALUES (7, ?)", (bytes(512),))
        con.commit()
        con.close()
        summary = avclient._describe_face_db(path)
        self.assertIn("faces(1 rows", summary)
        self.assertIn("'embedding': 512", summary)

    def test_describe_tar(self):
        import io
        import tarfile
        import tempfile
        path = os.path.join(tempfile.mkdtemp(), "known.db")
        self.addCleanup(os.remove, path)
        with tarfile.open(path, "w") as tar:
            for name, data in (("known/uiface.json", b'{"SubjectIdToNameMap": [[1, "Alex"]], "IgnoredSubjectIds": []}'),
                               ("known/uiface.db", bytes(8) + b"\x01\x02")):
                info = tarfile.TarInfo(name)
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
        summary = avclient._describe_face_db(path)
        self.assertIn("known/uiface.json (", summary)
        self.assertIn("SubjectIdToNameMap=1", summary)
        self.assertIn("known/uiface.db (10 bytes; 2 non-zero bytes)", summary)

    def test_describe_unknown(self):
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            f.write(b"\x1f\x8b\x08rest")
        self.addCleanup(os.remove, f.name)
        self.assertEqual(avclient._describe_face_db(f.name), "gzip")


_SAMPLES = os.environ.get("AIPORT_ENRICH_SAMPLES")


@unittest.skipUnless(_SAMPLES and enrich.lpr_available() and enrich.face_available(),
                     "set AIPORT_ENRICH_SAMPLES (plate.png, lena.jpg) with fast-alpr + face models")
class TestModels(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import cv2
        cls.cv2 = cv2
        cls.e = enrich.Enricher(faces_dir=None)

    def test_plate(self):
        img = self.cv2.imread(os.path.join(_SAMPLES, "plate.png"))
        plates = self.e.plates(img, [[0, 0, 1000, 1000]])
        self.assertEqual([p["text"] for p in plates], ["5AU5341"])

    def test_face(self):
        img = self.cv2.imread(os.path.join(_SAMPLES, "lena.jpg"))
        faces = self.e.faces(img, [[0, 0, 1000, 1000]])
        self.assertEqual(len(faces), 1)
        self.assertEqual(faces[0]["name"], "")
        self.assertEqual(len(faces[0]["embedding"]), 128)


if __name__ == "__main__":
    unittest.main()
