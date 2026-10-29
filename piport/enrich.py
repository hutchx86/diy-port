#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 hutchx86
"""Second-stage enrichment on top of the detector's person/vehicle/animal boxes.

  * vehicle type + colour -> descriptor ``attributes`` ({val, confidence} per key,
    shape from the Protect backend decompile; values are best guesses)
  * license plates        -> the vehicle descriptor's ``name`` (where Protect reads the
    plate; fast-alpr: YOLOv9 plate detector + CCT OCR, both MIT), read off the
    full-resolution snapshot
  * faces                 -> ``face`` descriptors (OpenCV zoo YuNet detector, MIT) carrying
    ``faceEmbed``, the SFace embedding (Apache-2.0) Protect's face service groups by;
    optionally named against a local gallery: ``<faces dir>/<Name>/*.jpg``

Each stage is optional (AIPORT_VEHICLE_ATTRS / AIPORT_LPR / AIPORT_FACE =
auto|0|1): a missing dependency or model disables it with one warning.
"""
import importlib.util
import logging
import os
import threading

import numpy as np

log = logging.getLogger("aiport-enrich")

HERE = os.path.dirname(os.path.abspath(__file__))
MODELS_DIR = os.path.join(HERE, "models")
YUNET_PATH = os.path.join(MODELS_DIR, "face_detection_yunet_2023mar.onnx")
SFACE_PATH = os.path.join(MODELS_DIR, "face_recognition_sface_2021dec.onnx")

COORD_END = 1000  # descriptor coords are normalized 0-1000

# COCO label -> Protect vehicleType. Protect turns attributes into labels verbatim
# (extractAttributeLabelSet: "vehicleType:<val>") and only knows car/suv/truck/van/bus/
# motorcycle/bike (convertRamTagsToLabels; it relabels "sedan" as "car"). Seen live: "sedan"
# showed no type, "truck" did. COCO's "car" also covers SUVs and vans.
VEHICLE_TYPES = {
    "car": "car",
    "truck": "truck",
    "bus": "bus",
    "motorbike": "motorcycle",
    "motorcycle": "motorcycle",
    "bicycle": "bike",
}
# Protect's colour vocabulary; the classifier's "silver" has no Protect value.
PROTECT_COLORS = {"red", "orange", "yellow", "green", "blue", "purple", "pink", "brown",
                  "black", "white", "gray"}
COLOR_ALIASES = {"silver": "gray"}

FACE_SCORE_THRESH = float(os.environ.get("AIPORT_FACE_THRESH", "0.8"))
# SFace's published cosine threshold for "same person".
FACE_MATCH_THRESH = float(os.environ.get("AIPORT_FACE_MATCH_THRESH", "0.363"))
# Faces smaller than this (the shorter side, in snapshot pixels) aren't matched: SFace works on
# a 112 x 112 face, and an 18 x 23 px face on Camera C scored 0.13 to 1.0 against the same
# person. The AI Port firmware checks crop quality before recognising too.
FACE_MATCH_MIN_PX = int(os.environ.get("AIPORT_FACE_MATCH_MIN_PX") or 40)
# ...nor are faces whose SFace vector is shorter than this: its length falls with a face's
# quality (blur, looking down or away). Live 14:33 on Camera B a 40 px read of a stranger looking
# down (length 6.0) went to Protect, which named her after the person whose weak reference
# (length 3.9) sits 0.3-0.4 from many faces; good reads of him are 8.5-11.6.
FACE_EMBED_MIN_NORM = float(os.environ.get("AIPORT_FACE_EMBED_MIN_NORM") or 7.0)


def weak_embedding(embedding):
    """True if an SFace vector is too short (a poor face) to send or match."""
    return embedding is None or float(np.linalg.norm(np.asarray(embedding, dtype=np.float64))) < FACE_EMBED_MIN_NORM
PLATE_SCORE_THRESH = float(os.environ.get("AIPORT_LPR_THRESH", "0.5"))
# Plates narrower than this (snapshot pixels) aren't read: re-reading the 14:38 clips, every
# Camera B read under 75 px was wrong, from 80 px all but one were right, and Camera D's
# 40 px plate came out as DEPM782 and DEFM782 by turns. AIPORT_LPR_MIN_PX overrides it.
PLATE_MIN_PX = int(os.environ.get("AIPORT_LPR_MIN_PX") or 60)
# Plate/face crops are expanded by this factor around the parent box: the
# full-res snapshot can lag the 640 detection frame by up to a second.
CROP_MARGIN = 1.3


def _flag(name):
    """auto (default) -> None, else True/False."""
    v = os.environ.get(name, "auto").strip().lower()
    if v == "auto":
        return None
    return v not in ("0", "false", "no", "off")


def _have(module):
    return importlib.util.find_spec(module) is not None


def vehicle_attrs_enabled():
    # auto: only on hosts that opted into enrichment (plate/face deps installed),
    # so existing deployments keep sending attributes: null.
    f = _flag("AIPORT_VEHICLE_ATTRS")
    return f if f is not None else (lpr_available() or face_available())


def lpr_available():
    f = _flag("AIPORT_LPR")
    return f is not False and _have("fast_alpr") and _have("cv2")


def face_available():
    f = _flag("AIPORT_FACE")
    return f is not False and _have("cv2") and os.path.exists(YUNET_PATH)


def smart_detect_features():
    """Extra smartDetect capability strings to advertise for enabled stages."""
    extra = []
    if lpr_available():
        extra += ["licensePlate", "lprEnhancedByAiKey"]
    if face_available():
        extra += ["face", "faceEnhancedByAiKey"]
    return extra


# --- vehicle type + colour -------------------------------------------------

def _rgb_to_hsv(rgb):
    """uint8 Nx3 RGB -> float Nx3 HSV (h in degrees, s/v in 0-1)."""
    c = rgb.astype(np.float32) / 255.0
    r, g, b = c[:, 0], c[:, 1], c[:, 2]
    v = c.max(axis=1)
    mn = c.min(axis=1)
    d = v - mn
    s = np.where(v > 0, d / np.maximum(v, 1e-6), 0.0)
    h = np.zeros_like(v)
    nz = d > 1e-6
    rm = nz & (v == r)
    gm = nz & (v == g) & ~rm
    bm = nz & ~rm & ~gm
    h[rm] = (60 * ((g[rm] - b[rm]) / d[rm])) % 360
    h[gm] = 60 * ((b[gm] - r[gm]) / d[gm]) + 120
    h[bm] = 60 * ((r[bm] - g[bm]) / d[bm]) + 240
    return np.stack([h, s, v], axis=1)


def _pixel_colors(hsv):
    h, s, v = hsv[:, 0], hsv[:, 1], hsv[:, 2]
    names = np.full(len(h), "gray", dtype=object)
    chroma = s >= 0.25
    names[~chroma & (v >= 0.8)] = "white"
    names[~chroma & (v >= 0.55) & (v < 0.8)] = "silver"
    names[v < 0.2] = "black"
    hue = chroma & (v >= 0.2)
    bins = [(0, 15, "red"), (15, 40, "orange"), (40, 70, "yellow"), (70, 170, "green"),
            (170, 260, "blue"), (260, 330, "purple"), (330, 361, "red")]
    for lo, hi, name in bins:
        names[hue & (h >= lo) & (h < hi)] = name
    # dark, desaturated orange/red reads as brown
    names[hue & (h < 40) & (v < 0.45)] = "brown"
    return names


def dominant_color(frame_rgb, coord):
    """(color, confidence 0-100) for the central body of a box in a frame of
    any size; None if the crop is empty."""
    fh, fw = frame_rgb.shape[:2]
    x, y, w, h = coord
    # Central region: skips road/background at the edges and glass at the top.
    x1 = int((x + 0.2 * w) * fw / COORD_END)
    x2 = int((x + 0.8 * w) * fw / COORD_END)
    y1 = int((y + 0.35 * h) * fh / COORD_END)
    y2 = int((y + 0.85 * h) * fh / COORD_END)
    crop = frame_rgb[max(0, y1):min(fh, y2), max(0, x1):min(fw, x2)]
    if crop.size == 0:
        return None
    names = _pixel_colors(_rgb_to_hsv(crop.reshape(-1, 3)))
    values, counts = np.unique(names, return_counts=True)
    i = int(np.argmax(counts))
    return str(values[i]), int(round(100 * counts[i] / counts.sum()))


def vehicle_attributes(label, frame_rgb, coord):
    """Descriptor ``attributes`` for a vehicle detection, or None."""
    attrs = {}
    vtype = VEHICLE_TYPES.get((label or "").strip())
    if vtype:
        attrs["vehicleType"] = {"val": vtype, "confidence": 70}
    if frame_rgb is not None:
        color = dominant_color(frame_rgb, coord)
        if color:
            name = COLOR_ALIASES.get(color[0], color[0])
            if name in PROTECT_COLORS:
                attrs["color"] = {"val": name, "confidence": color[1]}
    return attrs or None


# --- plates + faces on the full-res snapshot -------------------------------

def _expand_px(coord, img_w, img_h, margin=CROP_MARGIN, top_frac=1.0):
    """Normalized [x,y,w,h] -> clamped pixel (x1,y1,x2,y2), grown by margin.
    top_frac keeps only the upper part (faces sit in the top of a person box)."""
    x, y, w, h = coord
    h = h * top_frac
    cx, cy = x + w / 2, y + h / 2
    w, h = w * margin, h * margin
    x1 = int(max(0, (cx - w / 2) * img_w / COORD_END))
    y1 = int(max(0, (cy - h / 2) * img_h / COORD_END))
    x2 = int(min(img_w, (cx + w / 2) * img_w / COORD_END))
    y2 = int(min(img_h, (cy + h / 2) * img_h / COORD_END))
    return x1, y1, x2, y2


def _to_norm(x1, y1, x2, y2, img_w, img_h):
    sx, sy = COORD_END / img_w, COORD_END / img_h
    return [x1 * sx, y1 * sy, max(1.0, (x2 - x1) * sx), max(1.0, (y2 - y1) * sy)]


def _embedding_list(emb):
    """SFace feature (1x128 float32) -> JSON-safe list for the descriptor's faceEmbed."""
    if emb is None:
        return None
    return [round(float(v), 5) for v in np.asarray(emb).ravel()]


class Enricher:
    """Lazily-loaded plate + face models, shared across camera threads."""

    def __init__(self, faces_dir=None):
        self._lock = threading.Lock()
        self._alpr = None
        self._yunet = None
        self._sface = None
        self.lpr = lpr_available()
        self.face = face_available()
        self.faces_dir = faces_dir
        self._gallery = []          # [(name, embedding)]
        self._gallery_stamp = None  # (path, mtime) tuple the gallery was built from
        if _flag("AIPORT_LPR") and not self.lpr:
            log.warning("AIPORT_LPR=1 but fast-alpr/opencv are not installed -- plates disabled")
        if _flag("AIPORT_FACE") and not self.face:
            log.warning("AIPORT_FACE=1 but opencv or %s is missing -- faces disabled", YUNET_PATH)
        log.info("enrichment: vehicle attrs=%s, plates=%s, faces=%s (gallery=%s)",
                 vehicle_attrs_enabled(), self.lpr, self.face, faces_dir)

    def _get_alpr(self):
        if self._alpr is None:
            from fast_alpr import ALPR
            # Both on CPU: the CCT OCR graph doesn't compile for Core ML MLProgram, and the
            # YOLOv9 end2end detector's NMS output has a dynamic shape ({-1}) that Core ML
            # fails on at inference (seen live on passing cars; fast-alpr then returns no
            # plates). ~33 ms per read on an M1 Pro CPU, only for moving vehicles.
            self._alpr = ALPR(detector_providers=["CPUExecutionProvider"],
                              ocr_providers=["CPUExecutionProvider"])
        return self._alpr

    def _get_face_models(self):
        import cv2
        if self._yunet is None:
            self._yunet = cv2.FaceDetectorYN.create(YUNET_PATH, "", (320, 320), FACE_SCORE_THRESH)
        if self._sface is None and os.path.exists(SFACE_PATH):
            self._sface = cv2.FaceRecognizerSF.create(SFACE_PATH, "")
        return self._yunet, self._sface

    def plates(self, img_bgr, vehicle_coords):
        """Plates inside each vehicle box: [{coord, text, confidence(0-1), parent, width_px}].
        Plates narrower than PLATE_MIN_PX are left out: too small to read reliably."""
        if not self.lpr or img_bgr is None:
            return []
        ih, iw = img_bgr.shape[:2]
        out = []
        with self._lock:
            try:
                alpr = self._get_alpr()
            except Exception:
                log.exception("plate model load failed -- plates disabled")
                self.lpr = False
                return []
            for parent, coord in enumerate(vehicle_coords):
                x1, y1, x2, y2 = _expand_px(coord, iw, ih)
                crop = img_bgr[y1:y2, x1:x2]
                if crop.shape[0] < 16 or crop.shape[1] < 16:
                    continue
                for r in alpr.predict(crop):
                    if r.detection.confidence < PLATE_SCORE_THRESH or not r.ocr or not r.ocr.text:
                        continue
                    conf = r.ocr.confidence
                    conf = float(np.mean(conf)) if isinstance(conf, (list, tuple, np.ndarray)) else float(conf)
                    text = r.ocr.text.replace("_", "").strip().upper()
                    if len(text) < 3:
                        continue
                    b = r.detection.bounding_box
                    if b.x2 - b.x1 < PLATE_MIN_PX:
                        log.debug("plate %r skipped: %d px wide, under %d px", text,
                                  b.x2 - b.x1, PLATE_MIN_PX)
                        continue
                    out.append({"coord": _to_norm(x1 + b.x1, y1 + b.y1, x1 + b.x2, y1 + b.y2, iw, ih),
                                "text": text,
                                "confidence": min(float(r.detection.confidence), conf),
                                "parent": parent, "width_px": int(b.x2 - b.x1)})
        return out

    def faces(self, img_bgr, person_coords):
        """Faces inside each person box: [{coord, score, embedding, name, similarity,
        parent, size_px}]. embedding is SFace's 128-float vector (None without the SFace
        model); size_px is the face's shorter side in pixels. Faces under FACE_MATCH_MIN_PX
        aren't matched against the gallery."""
        if not self.face or img_bgr is None:
            return []
        ih, iw = img_bgr.shape[:2]
        out = []
        with self._lock:
            try:
                yunet, sface = self._get_face_models()
            except Exception:
                log.exception("face model load failed -- faces disabled")
                self.face = False
                return []
            self._refresh_gallery(yunet, sface)
            for parent, coord in enumerate(person_coords):
                x1, y1, x2, y2 = _expand_px(coord, iw, ih, top_frac=0.6)
                crop = img_bgr[y1:y2, x1:x2]
                if crop.shape[0] < 20 or crop.shape[1] < 20:
                    continue
                yunet.setInputSize((crop.shape[1], crop.shape[0]))
                _, found = yunet.detect(crop)
                if found is None:
                    continue
                for f in found:
                    fx, fy, fw, fh = [float(v) for v in f[:4]]
                    emb = sface.feature(sface.alignCrop(crop, f)) if sface is not None else None
                    size_px = int(min(fw, fh))
                    name, sim = (self._match(sface, emb)
                                 if size_px >= FACE_MATCH_MIN_PX and not weak_embedding(emb)
                                 else ("", 0.0))
                    out.append({"coord": _to_norm(x1 + fx, y1 + fy, x1 + fx + fw, y1 + fy + fh, iw, ih),
                                "score": float(f[14]), "name": name, "similarity": sim,
                                "embedding": _embedding_list(emb), "parent": parent,
                                "size_px": size_px})
        return out

    def _match(self, sface, emb):
        if emb is None or not self._gallery:
            return "", 0.0
        import cv2
        best_name, best = "", 0.0
        for name, ref in self._gallery:
            sim = float(sface.match(emb, ref, cv2.FaceRecognizerSF_FR_COSINE))
            if sim > best:
                best_name, best = name, sim
        return (best_name, best) if best >= FACE_MATCH_THRESH else ("", best)

    def _refresh_gallery(self, yunet, sface):
        """(Re)load <faces_dir>/<Name>/*.jpg when anything in it changed."""
        if sface is None or not self.faces_dir or not os.path.isdir(self.faces_dir):
            self._gallery = []
            return
        files = []
        for name in sorted(os.listdir(self.faces_dir)):
            d = os.path.join(self.faces_dir, name)
            if not os.path.isdir(d):
                continue
            for fn in sorted(os.listdir(d)):
                if fn.lower().endswith((".jpg", ".jpeg", ".png")):
                    p = os.path.join(d, fn)
                    files.append((name, p, os.path.getmtime(p)))
        stamp = tuple((p, m) for _, p, m in files)
        if stamp == self._gallery_stamp:
            return
        import cv2
        gallery = []
        for name, path, _ in files:
            img = cv2.imread(path)
            if img is None:
                continue
            yunet.setInputSize((img.shape[1], img.shape[0]))
            _, found = yunet.detect(img)
            if found is None:
                log.warning("face gallery: no face found in %s", path)
                continue
            best = max(found, key=lambda f: f[2] * f[3])
            gallery.append((name, sface.feature(sface.alignCrop(img, best))))
        self._gallery, self._gallery_stamp = gallery, stamp
        log.info("face gallery: %d embedding(s) for %d name(s) from %s", len(gallery),
                 len({n for n, _ in gallery}), self.faces_dir)
