import io
import json
import os
import sys
import tarfile
import tempfile
import unittest
from unittest import mock

import numpy as np

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import facedb  # noqa: E402

os.environ.setdefault("AIPORT_TICK_S", "0.33")       # tests assume macOS's tick and
os.environ.setdefault("AIPORT_DETECT_FPS", "10")    # detection rate on every platform
try:
    import avclient as av
except Exception:  # pragma: no cover
    av = None

RNG = np.random.default_rng(7)


def vec(scale=10.0):
    v = RNG.normal(size=facedb.DIM)
    return v / np.linalg.norm(v) * scale


def item(*embeddings, garbage=False):
    """One 2 064-byte Annoy node: 16-byte header, then 512 floats holding the embeddings at
    floats 0, 132 and 264; with garbage, the unused floats are NaN/huge like Protect's."""
    v = np.full(facedb.ITEM_DIM, np.nan if garbage else 0.0)
    if garbage:
        v[300:310] = 1e30
    for slot, e in zip(facedb.BLOCKS, embeddings):
        v[slot:slot + facedb.DIM] = e
    return np.array([1, 0, 0, 0], "<i4").tobytes() + np.asarray(v, "<f4").tobytes()


def build(subjects, ignored_subjects=(), tail=0, extra_records=3, items=None):
    """Tar bytes laid out like Protect's: subjects = {subject_id: (name, [vectors])}, one
    instance per vector (items overrides the uiface.db nodes)."""
    to_subject, names, rels, vectors = [], [], [], []
    for sid, (name, vs) in subjects.items():
        names.append([sid, name])
        ids = []
        for v in vs:
            iid = len(vectors)
            vectors.append(v)
            to_subject.append([iid, sid])
            ids.append(iid)
        rels.append([sid, ids])
    db = bytearray()
    for node in (items if items is not None else [item(v) for v in vectors]):
        db += node
    db += bytes(facedb.NODE * extra_records) + bytes(tail)
    doc = {"EnrolledSubjectIds": list(subjects), "EnrolledSubjectRelationMap": rels,
           "IgnoredSubjectIds": list(ignored_subjects), "IngnoredInstanceIds": [],
           "InstanceIdToNameMap": [[i, subjects[s][0]] for i, s in to_subject],
           "InstanceIdToSubjectIdMap": to_subject,
           "InstanceIdToUniqueIdMap": [[i, i + 3] for i, _ in to_subject],
           "SubjectIdToNameMap": names, "VerifiedUniqueIds": []}
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w") as tar:
        for name, data in (("known/uiface.db", bytes(db)),
                           ("known/uiface.json", json.dumps(doc).encode())):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return out.getvalue()


class TestParse(unittest.TestCase):
    def test_instances_and_names(self):
        a, b, c = vec(), vec(), vec()
        db = facedb.parse(build({5: ("Ian Vincent", [a, b]), 6: ("Pat", [c])}))
        self.assertEqual([(i, s, n) for i, s, n, _ in db.instances],
                         [(0, 5, "Ian Vincent"), (1, 5, "Ian Vincent"), (2, 6, "Pat")])
        np.testing.assert_allclose(db.instances[2][3], c, rtol=1e-6)

    def test_layout_is_annoy_nodes_of_2064_bytes(self):
        a, b = vec(), vec()
        data = build({5: ("Ian", [a, b])})
        with tarfile.open(fileobj=io.BytesIO(data)) as tar:
            db_bytes = tar.extractfile("known/uiface.db").read()
        second = np.frombuffer(db_bytes[2064 + 16:2064 + 16 + 512], "<f4")
        np.testing.assert_allclose(second, b, rtol=1e-6)   # instance 1 at byte 2064 + 16

    def test_instance_with_two_embeddings_and_garbage(self):
        # Ian's Oct 18 DB: instance 0 held embeddings at floats 0 and 132, the rest of its
        # item unused memory.
        a, b, c = vec(), vec(), vec()
        data = build({5: ("Ian", [a, c])}, items=[item(a, b, garbage=True), item(c, garbage=True)])
        db = facedb.parse(data)
        self.assertEqual([i for i, *_ in db.instances], [0, 0, 1])
        np.testing.assert_allclose(db.instances[1][3], b, rtol=1e-6)

    def test_instance_without_an_embedding_is_skipped(self):
        db = facedb.parse(build({5: ("Ian", [vec(), vec()])},
                                items=[item(garbage=True), item(vec())]))
        self.assertEqual([i for i, *_ in db.instances], [1])

    def test_ignored_subject_left_out(self):
        db = facedb.parse(build({5: ("Ian", [vec()]), 6: ("Pat", [vec()])}, ignored_subjects=[6]))
        self.assertEqual(set(db.subjects), {5})

    def test_bad_files(self):
        db = facedb.parse(build({5: ("Ian", [np.zeros(facedb.DIM)])}))   # not an embedding
        self.assertEqual(len(db), 0)
        short = build({5: ("Ian", [vec()])}, extra_records=0)[:600]
        with self.assertRaises(Exception):
            facedb.parse(short)

    def test_load_missing_or_corrupt(self):
        self.assertIsNone(facedb.load("/nonexistent/known.db"))
        with tempfile.NamedTemporaryFile(suffix=".db") as f:
            f.write(b"not a tar")
            f.flush()
            self.assertIsNone(facedb.load(f.name))


class TestMatch(unittest.TestCase):
    def test_exact_vector_matches(self):
        a = vec()
        db = facedb.parse(build({5: ("Ian", [a, vec()])}))
        sid, name, sim = db.match(list(a * 0.7), 0.363)   # scale doesn't matter (cosine)
        self.assertEqual((sid, name), (5, "Ian"))
        self.assertAlmostEqual(sim, 1.0, places=5)

    def test_below_threshold_is_none(self):
        db = facedb.parse(build({5: ("Ian", [vec()])}))
        self.assertIsNone(db.match(vec(), 0.363))
        self.assertIsNone(db.match(None, 0.363))
        self.assertIsNone(db.match([1.0, 2.0], 0.363))

    def test_coverage_beats_one_high_similarity(self):
        probe = vec()
        def near(sim):  # a vector at cosine `sim` from probe
            other = vec()
            other -= other @ probe / (probe @ probe) * probe
            return (sim * probe / np.linalg.norm(probe)
                    + np.sqrt(1 - sim ** 2) * other / np.linalg.norm(other)) * 10
        db = facedb.parse(build({1: ("Both", [near(0.5), near(0.45)]),
                                 2: ("One", [near(0.9), near(0.1)])}))
        self.assertEqual(db.match(probe, 0.363)[:2], (1, "Both"))   # 2/2 beats 1/2


@unittest.skipIf(av is None, "avclient unavailable")
class TestAvclientMatching(unittest.TestCase):
    def test_two_reads_name_the_track(self):
        tr = {"objectType": "face"}
        av._merge_extra(tr, {"scored": {"match": ((5, "Ian"), 0.6)}}, "D", 3)
        self.assertNotIn("matchedId", tr["extra"])
        av._merge_extra(tr, {"scored": {"match": ((5, "Ian"), 0.55)}}, "D", 3)
        self.assertEqual((tr["extra"]["matchedId"], tr["extra"]["matchedName"]), (5, "Ian"))

    def test_one_stray_read_does_not_flip_the_name(self):
        tr = {"objectType": "face"}
        for subject in ((5, "Ian"), (5, "Ian"), (6, "Pat")):
            av._merge_extra(tr, {"scored": {"match": (subject, 0.5)}}, "D", 3)
        self.assertEqual(tr["extra"]["matchedName"], "Ian")

    def test_attr_map_carries_the_match(self):
        tr = {"objectType": "face", "extra": {"matchedId": 5, "matchedName": "Ian",
                                              "faceEmbed": [0.1]}}
        entry = av._tracker_attrs(3, tr, {})
        self.assertEqual((entry["matchedId"], entry["matchedName"]), (5, "Ian"))

    def test_face_db_loaded_from_the_state_dir(self):
        data = build({5: ("Ian", [vec()])})
        d = tempfile.mkdtemp()
        os.makedirs(os.path.join(d, "facedb"))
        with open(os.path.join(d, "facedb", "known.db"), "wb") as f:
            f.write(data)
        with mock.patch.object(av, "STATE_DIR", d), mock.patch.object(av, "_face_db", None), \
                mock.patch.object(av, "_face_db_tried", False):
            self.assertEqual(av._get_face_db().subjects, {5: "Ian"})

    def test_enrichment_counts_a_snapshot_once(self):
        ian = vec()
        db = facedb.parse(build({5: ("Ian", [ian])}))

        class Enricher:
            lpr, face = False, True
            def plates(self, img, coords):
                return []
            def faces(self, img, coords):
                return [{"coord": [10, 10, 20, 20], "score": 0.9, "name": "",
                         "similarity": 0.0, "embedding": list(ian), "parent": 0}]
        person = {"objectType": "person", "score": 0.9, "coord": [0, 0, 100, 300]}
        with mock.patch.object(av, "_get_enricher", lambda: Enricher()), \
                mock.patch.object(av, "_get_face_db", lambda: db), \
                mock.patch.object(av, "_load_snapshot_bgr", lambda dev: (np.zeros((9, 9, 3)), 1.0)):
            av._enrich_cache.pop("D", None)
            first = av._enrich_detections("D", [dict(person)])
            again = av._enrich_detections("D", [dict(person)])   # same snapshot
        self.assertIn("match", first[0]["scored"])
        self.assertNotIn("match", again[0]["scored"])
        self.assertIn("faceEmbed", again[0]["scored"])

    def test_small_face_is_kept_without_embedding_or_match(self):
        # Live 09:21 on Camera C: an 18 x 23 px face scored 0.299 against the same person. A
        # small face is still tracked (avclient holds it back from Protect until nameable).
        ian = vec()
        db = facedb.parse(build({5: ("Ian", [ian])}))

        class Enricher:
            lpr, face = False, True
            def plates(self, img, coords):
                return []
            def faces(self, img, coords):
                return [{"coord": [10, 10, 20, 20], "score": 0.9, "name": "", "similarity": 0.0,
                         "embedding": list(ian), "parent": 0, "size_px": size}]
        person = {"objectType": "person", "score": 0.9, "coord": [0, 0, 100, 300]}
        found = {}
        minimum = av.enrich.FACE_MATCH_MIN_PX
        for size, mtime in ((18, 2.0), (minimum, 2.5), (av.FACE_EMBED_MIN_PX, 2.7)):
            with mock.patch.object(av, "_get_enricher", lambda: Enricher()), \
                    mock.patch.object(av, "_get_face_db", lambda: db), \
                    mock.patch.object(av, "_load_snapshot_bgr",
                                      lambda dev, m=mtime: (np.zeros((9, 9, 3)), m)):
                av._enrich_cache.pop("D", None)
                found[size], = av._enrich_detections("D", [dict(person)])
        self.assertTrue(found[18]["small"])
        self.assertNotIn("faceEmbed", found[18].get("scored", {}))
        self.assertNotIn("match", found[18].get("scored", {}))
        self.assertNotIn("small", found[minimum])                     # at the minimum: nameable
        self.assertIn("match", found[minimum]["scored"])
        # ...but its embedding stays here below FACE_EMBED_MIN_PX: live 18:41 a 43 px read went
        # to Protect's own search unnamed and came back as the wrong person.
        self.assertNotIn("faceEmbed", found[minimum]["scored"])
        self.assertIn("faceEmbed", found[av.FACE_EMBED_MIN_PX]["scored"])


    def _enrich_one(self, embedding, db):
        class Enricher:
            lpr, face = False, True
            def plates(self, img, coords):
                return []
            def faces(self, img, coords):
                return [{"coord": [10, 10, 20, 20], "score": 0.9, "name": "", "similarity": 0.0,
                         "embedding": list(embedding), "parent": 0, "size_px": 60}]
        person = {"objectType": "person", "score": 0.9, "coord": [0, 0, 100, 300]}
        with mock.patch.object(av, "_get_enricher", lambda: Enricher()), \
                mock.patch.object(av, "_get_face_db", lambda: db), \
                mock.patch.object(av, "_load_snapshot_bgr", lambda dev: (np.zeros((9, 9, 3)), 3.0)):
            av._enrich_cache.pop("D", None)
            return av._enrich_detections("D", [dict(person)])

    def test_weak_face_is_reported_without_embedding_or_match(self):
        # Live 14:33 on Camera B: a stranger looking down (vector length 6.0) went to Protect,
        # which named her after a person whose weak reference was close to many faces.
        alex = vec()
        db = facedb.parse(build({5: ("Alex", [alex])}))
        faces = self._enrich_one(alex / np.linalg.norm(alex) * 6.0, db)   # same direction, short
        self.assertEqual(faces[0]["objectType"], "face")
        self.assertNotIn("faceEmbed", faces[0].get("scored", {}))
        self.assertNotIn("match", faces[0].get("scored", {}))

    def test_strong_face_keeps_its_embedding_and_match(self):
        alex = vec()
        db = facedb.parse(build({5: ("Alex", [alex])}))
        faces = self._enrich_one(alex / np.linalg.norm(alex) * 8.0, db)
        self.assertIn("faceEmbed", faces[0]["scored"])
        self.assertIn("match", faces[0]["scored"])

if __name__ == "__main__":
    unittest.main()
