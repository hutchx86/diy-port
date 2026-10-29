# SPDX-License-Identifier: MIT AND AGPL-3.0-or-later
#
# Adapted from Frigate's frigate/util/object.py and frigate/util/image.py (region clustering,
# calculate_region, reduce_detections, clipped), https://github.com/blakeblackshear/frigate
#
# The MIT License
# Copyright (c) 2026 Frigate, Inc. (Frigate™)
#
# Permission is hereby granted, free of charge, to any person obtaining a copy of this
# software and associated documentation files (the "Software"), to deal in the Software
# without restriction, including without limitation the rights to use, copy, modify, merge,
# publish, distribute, sublicense, and/or sell copies of the Software, and to permit persons
# to whom the Software is furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all copies or
# substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED,
# INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR
# PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE
# FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR
# OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
# DEALINGS IN THE SOFTWARE.
#
# Changes for pi-port: detections are dicts ({"label", "score", "box", "region"}); NMS is
# plain numpy rather than cv2.dnn; no learned region grid (a fixed startup scan instead).
"""Where to look: square regions around motion and tracked objects, and merging the
detections found in them.

Boxes and regions are (x1, y1, x2, y2) in detect-frame pixels. Each region is a square at
least `min_region` (the model's input, 320) on a side, so a small or distant object is seen
at close to native resolution instead of squeezed into one frame-sized model input.
"""
from collections import defaultdict

import numpy as np

MIN_REGION = 320
# Per COCO label, as in Frigate's const.py.
# person 0.75, not Frigate's 0.9: live 11:33 at Camera A an upper-body box 89% inside the
# full-body box of the same person, standing close, became a second person.
LABEL_CONSOLIDATION = {"car": 0.8, "face": 0.5, "person": 0.75}
LABEL_CONSOLIDATION_DEFAULT = 0.9
LABEL_NMS = {"car": 0.6}
LABEL_NMS_DEFAULT = 0.4


def area(box):
    return (box[2] - box[0] + 1) * (box[3] - box[1] + 1)


def intersection(a, b):
    if a[2] < b[0] or a[0] > b[2] or a[1] > b[3] or a[3] < b[1]:
        return None
    return (max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3]))


def box_overlaps(a, b):
    return not (a[2] < b[0] or a[0] > b[2] or a[1] > b[3] or a[3] < b[1])


def box_inside(outer, inner):
    return (inner[0] >= outer[0] and inner[1] >= outer[1]
            and inner[2] <= outer[2] and inner[3] <= outer[3])


def inside_any(box, boxes):
    return any(box_inside(b, box) for b in boxes)


def intersects_any(box, boxes):
    return any(box_overlaps(box, b) for b in boxes)


def calculate_region(frame_shape, xmin, ymin, xmax, ymax, model_size, multiplier=2):
    """Square region around a box: `multiplier` x its longest side (a multiple of 4), at
    least model_size and at most the frame's short side, kept inside the frame."""
    size = int((max(xmax - xmin, ymax - ymin) * multiplier) // 4 * 4)
    size = max(size, model_size)
    size = min(size, min(frame_shape[:2]) // 4 * 4)  # a square crop must fit the frame
    x = int((xmax - xmin) / 2.0 + xmin - size / 2.0)
    x = 0 if x < 0 else (max(0, frame_shape[1] - size) if x > frame_shape[1] - size else x)
    y = int((ymax - ymin) / 2.0 + ymin - size / 2.0)
    y = 0 if y < 0 else (max(0, frame_shape[0] - size) if y > frame_shape[0] - size else y)
    return (x, y, x + size, y + size)


def _cluster_boundary(box, min_region):
    """How far a cluster seeded by this box may reach (the box is ~10% of its region)."""
    w, h = box[2] - box[0], box[3] - box[1]
    max_size = max(min_region, int(np.sqrt(abs(w * h) / 0.1)))
    cx, cy = w / 2 + box[0], h / 2 + box[1]
    dx = int(max_size - w / 2 * 1.1)
    dy = int(max_size - h / 2 * 1.1)
    return (int(cx - dx), int(cy - dy), int(cx + dx), int(cy + dy))


def cluster_region(frame_shape, min_region, cluster, boxes):
    """Region holding every box (by index) of a cluster."""
    xs1, ys1, xs2, ys2 = zip(*(boxes[i] for i in cluster))
    return calculate_region(frame_shape, min(xs1), min(ys1), max(xs2), max(ys2), min_region,
                            multiplier=1.35)


def cluster_candidates(frame_shape, min_region, boxes):
    """Group boxes that fit one region; each box in exactly one group (lists of indices)."""
    clusters, used = [], set()
    for i, b in enumerate(boxes):
        if i in used:
            continue
        cluster = [i]
        used.add(i)
        boundary = _cluster_boundary(b, min_region)
        for j, other in enumerate(boxes):
            if j in used or not box_inside(boundary, other):
                continue
            candidate = cluster + [j]
            region = cluster_region(frame_shape, min_region, candidate, boxes)
            # A region bigger than the minimum mustn't leave any box under 5% of it.
            if (region[2] - region[0]) > min_region and any(
                    area(boxes[k]) / area(region) < 0.05 for k in candidate):
                continue
            cluster.append(j)
            used.add(j)
        clusters.append(cluster)
    return [list(c) for c in {tuple(sorted(c)) for c in clusters}]


def regions_for(frame_shape, boxes, min_region=MIN_REGION):
    """Regions covering every box, clustered."""
    if not boxes:
        return []
    return [cluster_region(frame_shape, min_region, c, boxes)
            for c in cluster_candidates(frame_shape, min_region, boxes)]


def startup_regions(frame_shape):
    """Squares the height (or width) of the frame covering all of it: one full look at
    start, so objects already there (parked cars) are found without any motion."""
    h, w = frame_shape[:2]
    size = min(h, w) // 4 * 4
    long_side = max(h, w)
    count = max(1, -(-long_side // size))
    offsets = [0] if count == 1 else [round(i * (long_side - size) / (count - 1))
                                      for i in range(count)]
    if w >= h:
        return [(x, 0, x + size, size) for x in offsets]
    return [(0, y, size, y + size) for y in offsets]


def clipped(det, frame_shape):
    """True if the box touches its region's edge where the region isn't at the frame edge
    (the object probably continues outside the region)."""
    box, region = det["box"], det["region"]
    return ((region[0] > 5 and box[0] - region[0] <= 5)
            or (region[1] > 5 and box[1] - region[1] <= 5)
            or (frame_shape[1] - region[2] > 5 and region[2] - box[2] <= 5)
            or (frame_shape[0] - region[3] > 5 and region[3] - box[3] <= 5))


def _nms(boxes, scores, iou_thresh):
    """Indices kept by non-maximum suppression, best score first."""
    b = np.asarray(boxes, dtype=np.float64)
    order = np.argsort(scores)[::-1]
    keep = []
    while order.size:
        i = order[0]
        keep.append(int(i))
        xx1 = np.maximum(b[i, 0], b[order[1:], 0])
        yy1 = np.maximum(b[i, 1], b[order[1:], 1])
        xx2 = np.minimum(b[i, 2], b[order[1:], 2])
        yy2 = np.minimum(b[i, 3], b[order[1:], 3])
        inter = np.clip(xx2 - xx1, 0, None) * np.clip(yy2 - yy1, 0, None)
        a_i = (b[i, 2] - b[i, 0]) * (b[i, 3] - b[i, 1])
        a_o = (b[order[1:], 2] - b[order[1:], 0]) * (b[order[1:], 3] - b[order[1:], 1])
        iou = inter / np.maximum(a_i + a_o - inter, 1e-9)
        order = order[1:][iou <= iou_thresh]
    return keep


def reduce_detections(frame_shape, detections):
    """Merge detections from overlapping regions: per-label NMS (a box clipped by its
    region competes at 0.6, so a whole view of the object wins), then drop a box mostly
    inside a larger same-label one."""
    groups = defaultdict(list)
    for d in detections:
        groups[d["label"]].append(d)
    selected = []
    for label, group in groups.items():
        confidences = [0.6 if clipped(d, frame_shape) else d["score"] for d in group]
        for i in _nms([d["box"] for d in group], confidences,
                      LABEL_NMS.get(label, LABEL_NMS_DEFAULT)):
            selected.append(group[i])

    groups = defaultdict(list)
    for d in selected:
        groups[d["label"]].append(d)
    out = []
    for label, group in groups.items():
        ordered = sorted(group, key=lambda d: area(d["box"]))
        limit = LABEL_CONSOLIDATION.get(label, LABEL_CONSOLIDATION_DEFAULT)
        for i, d in enumerate(ordered):
            a = area(d["box"])
            inside = False
            for other in ordered[i + 1:]:
                # A box under 5% of a bigger one isn't compared: a car parked in front
                # mustn't hide cars on the street behind it.
                if a / area(other["box"]) < 0.05:
                    continue
                inter = intersection(d["box"], other["box"])
                if inter is not None and area(inter) / a > limit:
                    inside = True
                    break
            if not inside:
                out.append(d)
    return out
