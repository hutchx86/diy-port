# Credits

Third-party projects this repository references, derives from, or vendors. See
`LICENSE` for this project's own license (AGPL-3.0-or-later).

## Vendored assets

- **[airockchip/rknn_model_zoo](https://github.com/airockchip/rknn_model_zoo)**
  (Apache-2.0, tag `v2.3.2`) — copied from that project:
  `diyport/models/anchors_yolov5.txt`,
  `diyport/models/coco_80_labels_list.txt`, `rknn_convert/anchors_yolov5.txt`,
  `rknn_convert/coco_80_labels_list.txt` and `rknn_convert/coco_subset_20.txt`.
  `detectors/postprocess.py`'s YOLOv5 post-processing and `rknn_convert/convert.py` are adapted
  from its example. Apache-2.0 text: [`LICENSES/Apache-2.0.txt`](LICENSES/Apache-2.0.txt).

- **[google-coral/test_data](https://github.com/google-coral/test_data)**
  (Apache-2.0) — `diyport/models/coco_90_labels.txt` is its `coco_labels.txt`.

## Adapted code

- **[blakeblackshear/frigate](https://github.com/blakeblackshear/frigate)** (MIT) —
  the motion gate (`diyport/motion.py`, from `frigate/motion/improved_motion.py`), region
  crops (`diyport/regions.py`, from `frigate/util/object.py` and `frigate/util/image.py`),
  the stationary-object classifier (`diyport/stationary.py`, from
  `frigate/track/stationary_classifier.py`) and Norfair matching
  (`diyport/norfair_match.py`, from `frigate/track/norfair_tracker.py`) are adapted from
  Frigate. Each file carries `SPDX-License-Identifier: MIT AND AGPL-3.0-or-later` and points
  at the MIT text in [`LICENSES/MIT-frigate.txt`](LICENSES/MIT-frigate.txt).

## Detection model

- **[Ultralytics YOLOv5](https://github.com/ultralytics/yolov5)** (AGPL-3.0) —
  the detection architecture. The converted `.rknn` is an AGPL-3.0 derivative of
  YOLOv5s, so redistributed model binaries carry AGPL-3.0, the same license as
  this project.

- **Google SSDLite MobileDet COCO Edge TPU model**
  ([google-coral/test_data](https://github.com/google-coral/test_data),
  Apache-2.0) — the `edgetpu` backend's default model, downloaded at install
  time, not stored here.

## Runtime dependencies (installed, not vendored)

- **[feranick/libedgetpu](https://github.com/feranick/libedgetpu)**
  (Apache-2.0) — maintained build of Google's Edge TPU runtime; `install.sh`
  installs its `libedgetpu1-std` package for the `edgetpu` backend.
- **[ai-edge-litert](https://pypi.org/project/ai-edge-litert/)** (Apache-2.0) —
  Google's TFLite runtime, which loads the Edge TPU delegate.
- **onnxruntime** (MIT), **numpy** (BSD-3-Clause), **requests** (Apache-2.0),
  **websockets** (BSD-3-Clause), **paramiko** (LGPL-2.1) — Python runtime
  dependencies. **ffmpeg** (LGPL-2.1-or-later or GPL depending on the build) is
  the video pipeline.
- **OpenCV** (Apache-2.0) — optional; the motion gate, stationary checks, face detection
  and snapshot decoding.
- **[fast-alpr](https://github.com/ankandrew/fast-alpr)** (MIT) — optional license-plate
  detection and OCR; its `fast-plate-ocr` weights are downloaded at first use, not
  stored here.
- **[Norfair](https://github.com/tryolabs/norfair)** (BSD-3-Clause) — optional Kalman
  tracker used when `AIPORT_TRACKER=norfair`.

## Non-free, not distributed

- **Rockchip `rknn-toolkit2` / `rknn-toolkit-lite2`** — installed by the user
  from PyPI under Rockchip's proprietary RKNN SDK license; not covered by this
  repository's license and not redistributed here.

## Protocol research

- **[keshavdv/unifi-cam-proxy](https://github.com/keshavdv/unifi-cam-proxy)**
  (MIT) — the `extendedFlv` push format (`clock_sync.py`) that
  `diyport/onvif_push.py`'s `rewrite_flv()` follows, with corrected trailer
  units. MIT text: [`LICENSES/MIT-unifi-cam-proxy.txt`](LICENSES/MIT-unifi-cam-proxy.txt).

- **[rjmotion/pyunifiwire](https://github.com/rjmotion/pyunifiwire)** — UBNT L2
  discovery field/TLV notes.
- **[danielwoz/ubiquiti-protect-onvif-event-listener](https://github.com/danielwoz/ubiquiti-protect-onvif-event-listener)**
  — event/schema reference. Its direct database-write approach is deliberately
  **not** used here; this project is protocol-only.

## Inspiration

- **[dciancu/unifi-protect-unvr-docker-arm64](https://github.com/dciancu/unifi-protect-unvr-docker-arm64)**
  — the project that got this author into UniFi tinkering, and whose methods
  helped in understanding how parts of the ecosystem work.
