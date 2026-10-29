# SPDX-License-Identifier: AGPL-3.0-or-later
"""Protect's known-faces DB (UpdateFaceDBRequest), and matching face embeddings against it.

The download is a tar of `known/uiface.json` (subject, instance and name maps) and
`known/uiface.db`. Protect enrols the embedding the device sent, so for pi-port the vectors
are SFace's 128 floats and compare directly with pi-port's own (docs/protect-face-matching.md).

`uiface.db` is an Annoy index (the AI Port firmware's `LoadEmbeddingData` calls Annoy's load)
of 512-float items with the dot metric: 2 064-byte nodes, item i (instance i) at byte i * 2064,
its 512 floats from byte 16. An item holds up to three 128-float embeddings, at floats 0, 132
and 264; the rest of the item is unused memory (NaNs, huge values, text). Every embedding found
in three real DBs was an exact copy of one pi-port sent.

Matching follows the AI Port firmware's ranking: per subject, coverage (the share of its
instances at or above the threshold), then the top similarity.
"""
import io
import json
import logging
import tarfile

import numpy as np

log = logging.getLogger("aiport-facedb")

DIM = 128
ITEM_DIM = 512
NODE = 16 + 4 * ITEM_DIM      # Annoy dot-product node: n_descendants, children[2], dot factor, v
HEADER = 16
BLOCKS = (0, 132, 264)        # float offsets of the embeddings in an item
NORM_RANGE = (1.0, 100.0)     # SFace features are unnormalised, ~5-20


def _embedding(block):
    """The block as an embedding, or None if it's unused memory."""
    if not np.all(np.isfinite(block)) or np.count_nonzero(block) < DIM - 8:
        return None
    n = np.linalg.norm(block)
    return block if NORM_RANGE[0] <= n <= NORM_RANGE[1] else None


def _pairs(doc, key):
    return {k: v for k, v in (doc.get(key) or [])}


class FaceDB:
    def __init__(self, instances):
        """instances: [(instance_id, subject_id, name, vector)], one per embedding (an
        instance can have several)."""
        self.instances = instances
        self.subjects = {}
        for _iid, sid, name, _v in instances:
            self.subjects.setdefault(sid, name)
        self._vecs = (np.array([v / np.linalg.norm(v) for *_, v in instances])
                      if instances else np.zeros((0, DIM)))

    def __len__(self):
        return len(self.instances)

    def match(self, embedding, threshold):
        """(subject_id, name, similarity) of the best subject, or None below threshold."""
        if embedding is None or not self.instances:
            return None
        e = np.asarray(embedding, dtype=np.float64).ravel()
        n = np.linalg.norm(e)
        if e.shape[0] != DIM or not np.isfinite(n) or n == 0:
            return None
        sims = self._vecs @ (e / n)
        per = {}
        for (_iid, sid, _name, _v), sim in zip(self.instances, sims):
            hits, total, top = per.get(sid, (0, 0, -1.0))
            per[sid] = (hits + (sim >= threshold), total + 1, max(top, float(sim)))
        sid, (hits, total, top) = max(per.items(), key=lambda kv: (kv[1][0] / kv[1][1], kv[1][2]))
        if hits == 0:
            return None
        return sid, self.subjects[sid], top


def parse(tar_bytes):
    """FaceDB from the downloaded tar's bytes. Raises ValueError if it doesn't look right."""
    with tarfile.open(fileobj=io.BytesIO(tar_bytes)) as tar:
        files = {m.name.split("/")[-1]: tar.extractfile(m).read()
                 for m in tar.getmembers() if m.isfile()}
    if "uiface.json" not in files or "uiface.db" not in files:
        raise ValueError("no uiface.json/uiface.db in the face DB")
    doc = json.loads(files["uiface.json"])
    data = files["uiface.db"]
    to_subject = _pairs(doc, "InstanceIdToSubjectIdMap")
    names = _pairs(doc, "SubjectIdToNameMap")
    ignored_subjects = set(doc.get("IgnoredSubjectIds") or [])
    ignored = set(doc.get("IngnoredInstanceIds") or doc.get("IgnoredInstanceIds") or [])
    instances = []
    for iid, sid in sorted(to_subject.items()):
        if sid in ignored_subjects or iid in ignored:
            continue
        start = iid * NODE + HEADER
        if iid < 0 or start + 4 * ITEM_DIM > len(data):
            raise ValueError(f"instance {iid} is past the end of uiface.db ({len(data)} bytes)")
        with np.errstate(all="ignore"):
            item = np.frombuffer(data[start:start + 4 * ITEM_DIM], dtype="<f4").astype(np.float64)
            found = [e for e in (_embedding(item[b:b + DIM]) for b in BLOCKS) if e is not None]
        if not found:
            log.warning("face DB instance %s (subject %s) has no usable embedding", iid, sid)
        instances += [(iid, sid, names.get(sid) or "", e) for e in found]
    return FaceDB(instances)


def load(path):
    """FaceDB from a saved download, or None (logged) if it can't be read."""
    try:
        with open(path, "rb") as f:
            db = parse(f.read())
    except FileNotFoundError:
        return None
    except Exception as e:
        log.warning("face DB %s not used: %s", path, e)
        return None
    log.info("face DB %s: %d embedding(s) in %d instance(s) of %d subject(s): %s", path, len(db),
             len({i for i, *_ in db.instances}), len(db.subjects),
             ", ".join(sorted(n or "?" for n in db.subjects.values())))
    return db
