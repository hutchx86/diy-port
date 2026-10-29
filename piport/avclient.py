#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 hutchx86
"""Second WebSocket connection AI Port needs alongside ucp4: the classic camera
"avclient" protocol.

The controller's pairing/stream pool only includes connections that do NOT
negotiate "ucp4" and that carry camera-mac plus camera-model = literal hex
sysid "0xa5f1". Wire protocol: wss://<host>:7442/camera/1.0/ws[?token=],
subprotocol secure_transfer over mTLS, headers camera-mac/camera-ip/
camera-model/camera-firmware/device-id/x-guid/adopted plus
Origin: http://ws_camera_proto_secure_transfer, and plain JSON in BINARY
frames starting with "ubnt_avclient_hello".
"""
import argparse
import collections
import contextlib
import itertools
import json
import logging
import math
import os
import queue
import shutil
import ssl
import statistics
import subprocess
import tempfile
import sys
import threading
import time
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlsplit

import requests
import urllib3

import facedb
from websockets.sync.client import connect

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

log = logging.getLogger("aiport-avclient")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config
import apple_fm
import enrich
import hoststats
import onvif_push
from procutil import start_stderr_drain

HERE = os.path.dirname(os.path.abspath(__file__))
ADOPT_STATE_FILE = os.path.join(HERE, "adopt_state.json")
CERT_PATH = os.path.join(HERE, "server.crt")
KEY_PATH = os.path.join(HERE, "server.key")
_PROCESS_START_MS = int(time.time() * 1000)

_msg_id_counter = itertools.count(1)

# deviceID -> subprocess.Popen pulling its RTSP stream from Protect's relay (not the camera).
_active_streams = {}
# tmpfs dir ffmpeg writes each camera's snapshot JPEG into (never persistent).
_STREAM_DIR = config.stream_dir()
# --state-dir (or None for the shared piport/ path); the firmware pin lives here.
STATE_DIR = None
# deviceID -> consecutive unexpected-death count since the last UiStreamControl start.
_stream_restart_count = {}
STREAM_RETRY_MAX_S = 30.0   # longest wait between pulls of a source that keeps failing
# deviceID -> time.monotonic()*1000 when its RTSP pull started; the origin for
# clockMonotonic/clockStream on that device's EventSmartDetect payloads.
_stream_started_monotonic_ms = {}

# Hardware video decode (AIPORT_HWDEC=auto|rkmpp|v4l2m2m|videotoolbox|none): rkmpp on the
# RK3588, v4l2m2m on a Pi 4 (/dev/video10 + /dev/vchiq), videotoolbox on macOS; else software.
FFMPEG_BIN = ("/usr/local/ffmpeg-rkmpp/bin/ffmpeg"
              if os.path.exists("/usr/local/ffmpeg-rkmpp/bin/ffmpeg") else "ffmpeg")
# Backend -> codec -> ffmpeg input args. VideoToolbox is a hwaccel on ffmpeg's own decoder
# (frames come back to system memory for the filters): ~4x less CPU than software HEVC.
# HEVC only: on a Protect-relayed H.264 stream it failed ~14 pictures in 20 s (software: 0).
_HWDEC_BACKENDS = {
    "rkmpp": {"h264": ["-c:v", "h264_rkmpp"], "hevc": ["-c:v", "hevc_rkmpp"]},
    "v4l2m2m": {"h264": ["-c:v", "h264_v4l2m2m"], "hevc": ["-c:v", "hevc_v4l2m2m"]},
    "videotoolbox": {"hevc": ["-hwaccel", "videotoolbox"]},
}
_FFPROBE_BIN = os.path.join(os.path.dirname(FFMPEG_BIN), "ffprobe") \
    if FFMPEG_BIN != "ffmpeg" and os.path.exists(os.path.join(os.path.dirname(FFMPEG_BIN), "ffprobe")) \
    else "ffprobe"


def _is_pi4(compatible_path="/proc/device-tree/compatible"):
    """True on the Raspberry Pi 4 family (BCM2711). The Pi 5 (BCM2712) has NO
    hardware H.264 decoder, so v4l2m2m must not be selected there."""
    try:
        with open(compatible_path, "rb") as f:
            return b"bcm2711" in f.read()
    except OSError:
        return False


def _detect_hwdec():
    """Active hardware-decode backend name, or None for software."""
    forced = os.environ.get("AIPORT_HWDEC", "auto").strip().lower()
    if forced in ("none", "sw", "software"):
        return None
    if forced in _HWDEC_BACKENDS:
        return forced
    if FFMPEG_BIN != "ffmpeg":
        return "rkmpp"
    # Pi 4 only (flaky under load); the Pi 5 has no H.264 hardware decoder.
    if _is_pi4() and os.path.exists("/dev/video10") and os.path.exists("/dev/vchiq"):
        return "v4l2m2m"
    if sys.platform == "darwin":
        return "videotoolbox"
    return None


def _parse_probe(out):
    """ffprobe `codec_name,width,height,avg_frame_rate` csv -> dict; missing
    fields are None."""
    info = {"codec": None, "width": None, "height": None, "fps": None}
    fields = out.strip().splitlines()[0].split(",") if out.strip() else []
    if fields:
        info["codec"] = fields[0].strip().lower() or None
    try:
        info["width"], info["height"] = int(fields[1]), int(fields[2])
    except (IndexError, ValueError):
        pass
    try:
        num, _, den = fields[3].partition("/")
        fps = float(num) / float(den or 1)
        info["fps"] = round(fps) if fps > 0 else None
    except (IndexError, ValueError, ZeroDivisionError):
        pass
    return info


def _probe_stream(url, timeout=8):
    """Codec ('h264', 'hevc'), width, height and fps of the RTSP stream's first
    video track; fields None if unknown."""
    try:
        out = subprocess.run(
            [_FFPROBE_BIN, "-v", "error", "-rtsp_transport", "tcp", "-select_streams", "v:0",
             "-show_entries", "stream=codec_name,width,height,avg_frame_rate",
             "-of", "csv=p=0", url],
            capture_output=True, timeout=timeout, text=True).stdout
    except (OSError, subprocess.SubprocessError):
        out = ""
    return _parse_probe(out)


# An RTSP pull ffmpeg gives up on after this long with no data, so a dead relay connection
# exits and is restarted (live 17:28 a pull sat 70 min on one, at 0% CPU, with no frames).
RTSP_TIMEOUT_S = float(os.environ.get("AIPORT_RTSP_TIMEOUT_S") or 15)
_rtsp_timeout_flag = []   # cached: [flag or None]


def _rtsp_timeout_args():
    """ffmpeg input args for RTSP_TIMEOUT_S. ffmpeg 5+ calls the socket I/O timeout
    "-timeout"; before that it was "-stimeout", and "-timeout" made ffmpeg listen instead."""
    if RTSP_TIMEOUT_S <= 0:
        return []
    if not _rtsp_timeout_flag:
        try:
            help_text = subprocess.run([FFMPEG_BIN, "-hide_banner", "-h", "demuxer=rtsp"],
                                       capture_output=True, text=True, timeout=10).stdout
        except (OSError, subprocess.SubprocessError):
            help_text = ""
        flag = None
        for line in help_text.splitlines():
            words = line.split()
            if words and words[0] in ("-timeout", "-stimeout") and "socket" in line:
                flag = words[0]
                break
        _rtsp_timeout_flag.append(flag)
    flag = _rtsp_timeout_flag[0]
    return [flag, str(int(RTSP_TIMEOUT_S * 1_000_000))] if flag else []


def _hwdec_args(codec):
    """Input decoder args for the active hardware backend, or empty (ffmpeg's
    auto-selected software decoder). Unknown/failed probe assumes h264, the
    historical default."""
    backend = _detect_hwdec()
    if not backend:
        return []
    return list(_HWDEC_BACKENDS[backend].get(codec or "h264", []))

# Real detection: per-tick YOLOv5s/RKNN classify the latest frame; objects are
# IOU-tracked per objectType with their own trackerID/eventId and enter/move/leave.
_motion_state = {}   # deviceID -> {"tracks": {trackerID: {...}}, "next_tracker_id": int}
_motion_lock = threading.Lock()
_motion_poll_stop = {}  # deviceID -> threading.Event, signals the poll loop to stop
_motion_poll_thread = {}  # deviceID -> threading.Thread, joined on stop
_event_id_counter = itertools.count(int(time.time()))  # monotonic, avoids same-second collisions across tracks
# Seconds between classification ticks (AIPORT_TICK_S). 0.33 s on macOS, where Core ML runs
# a frame in ~8 ms: a G5 Bullet sends a moving update every ~330 ms (median of 44, 249-600 ms)
# with displayTimeoutMSec ~326; at 1.5 s a car crossing a far road was classified once or not at all.
# 1.5 s elsewhere (Pi/RK3588 CPU budget). The detection stream runs at least this fast.
MOTION_POLL_INTERVAL_S = float(os.environ.get("AIPORT_TICK_S")
                               or (0.33 if sys.platform == "darwin" else 1.5))
# How often `moving` updates go to Protect: once per AIPORT_TICK_S.
REPORT_INTERVAL_MS = int(MOTION_POLL_INTERVAL_S * 1000)
# How many frames per second each camera's detector analyses (AIPORT_DETECT_FPS). More frames
# mean objects move less between checks, so tracks match more reliably, and enter/leave
# land closer to the truth; reports still go out once per tick. 10 on macOS (Core ML runs
# a frame in ~8 ms); elsewhere one frame per tick, as before. Never slower than reporting.
DETECT_INTERVAL_S = min(MOTION_POLL_INTERVAL_S,
                        1 / float(os.environ.get("AIPORT_DETECT_FPS")
                                  or (10 if sys.platform == "darwin"
                                      else 1 / MOTION_POLL_INTERVAL_S)))
DETECT_FPS = max(2, math.ceil(1 / DETECT_INTERVAL_S))


def _ticks(seconds):
    """Analysed-frame count for a duration. Track counters advance once per analysed
    frame, so time-based thresholds don't change with the detection rate."""
    return max(1, round(seconds / DETECT_INTERVAL_S))


# How long Protect shows each box: until just past the next update (a G5 Bullet's
# displayTimeoutMSec ~= its update gap). At 1.5 s per update a 200 ms box flashed on and off.
BOX_DISPLAY_MS = int(MOTION_POLL_INTERVAL_S * 1000) + 100
# Seconds with no detection before a track closes (AIPORT_MISS_S, default 3): the event
# ends about this long after the object was last seen. A G5 Bullet sends leave ~2 s after
# the object was last seen; 3 s allows for our detector missing a frame or two. At 30 s a
# car passing in a few seconds made a 30 s event. Needs track state to survive
# UiStreamControl refreshes. Parked tracks flicker in and out of detection, so they keep
# 30 s (dropping one would re-create it as a new arrival).
MOTION_MISS_TOLERANCE_S = float(os.environ.get("AIPORT_MISS_S") or 3)
MOTION_MISS_TOLERANCE_TICKS = _ticks(MOTION_MISS_TOLERANCE_S)
PARKED_MISS_TOLERANCE_TICKS = _ticks(30)
# Reported boxes are smoothed (exponential, this weight on the newest detection) so per-frame
# detector jitter doesn't make them jump; matching and crops use the raw box.
BOX_SMOOTHING = 0.6
# A moving track that no box overlaps takes the nearest same-type detection within this many
# times its size (+ margin, 0-1000 units) of where its last movement predicts it: small, fast
# objects (far-road cars and walkers) moved further than their width between ticks and were
# re-created as a new track every tick. The radius is capped at NN_DIST_MAX and widens only
# by NN_MISS_GROWTH per missed tick, to at most NN_MISS_GROWTH_MAX x: it used to multiply by
# every missed tick, so a track missed for a second took any same-type box in the frame (live
# 09:56 on Camera B: one vehicle track hopped between four cars over 24 s).
NN_DIST_FACTOR = 3.0
NN_DIST_MARGIN = 30
NN_DIST_MAX = 150
NN_MISS_GROWTH = 0.25
NN_MISS_GROWTH_MAX = 1.5
# A moving track never takes a box more than this many times wider or taller (or narrower or
# shorter) than its own, from Norfair or the nearest rule: live 10:20 on Camera B a bicycle's
# track (40x57) took a 49x136 box beside it, then a 107x253 one, and held the event 16 s.
NN_SIZE_RATIO_MAX = 2.0
SIZE_JUMP_INSIDE = 0.8
# The size a box is compared with is the median of the track's last SIZE_HISTORY boxes, not
# its last one: live 16:47 on Camera C a person's box halved for a moment (bending, or half
# hidden), the full box after it was over 2x that, and the same person became a second track.
SIZE_HISTORY = 8
# Min IOU (same objectType) to match a detection to an existing track.
IOU_MATCH_THRESHOLD = 0.3
# Which detection belongs to which track (AIPORT_TRACKER): "norfair", Frigate's Kalman-filtered
# Norfair tracker (norfair_match.py) with pi-port's parked and fast-mover rules for what it
# leaves, or "builtin", IoU then the nearest detection where a track's motion predicts it.
# Default: norfair when the package is installed (two people crossing kept their IDs where
# the built-in matcher swapped them), else builtin.
TRACKER = (os.environ.get("AIPORT_TRACKER") or "").strip().lower()
if TRACKER in ("", "norfair"):
    try:
        import norfair  # noqa: F401
        TRACKER = "norfair"
    except ImportError:
        if TRACKER == "norfair":
            log.warning("AIPORT_TRACKER=norfair but the norfair package isn't installed; "
                        "using the built-in matcher")
        TRACKER = "builtin"

# A track is a true positive once the median of its last SCORE_HISTORY scores (0.0 for each
# analysed frame it was missed in) reaches AIPORT_TRUE_THRESH -- Frigate's false-positive
# filter (frigate/track/tracked_object.py, MIT). Every detection already needs
# AIPORT_OBJ_THRESH (detectors/postprocess.py). Seen 2026-10-17: a tree stump on Camera C and a pot by
# Camera A scored "animal" ~0.5-0.6 for a minute at a time; a passing sedan scored
# 0.85 and 0.78 on its only two frames.
SCORE_HISTORY = 10
DEFAULT_TRUE_THRESH = 0.7
# ...and only once it has been detected on at least MIN_HITS frames (Frigate's tracker
# initialises an object after 2+ hits): a car scoring 0.9 on a single region crop became an
# event on its own, confirmed a second later with nothing else seen.
MIN_HITS = 2
# ...and once it is initialised as Frigate's Norfair tracker does (detect.min_initialized,
# default half the detection rate, ~0.5 s): a counter starts at 1 on the first detection, goes
# up 1 per detection and down 1 per miss, and the track counts once it is above
# MIN_INITIALIZED. Live 19:17 on Camera C four flickering night-time boxes in one corner each
# became a person event. AIPORT_MIN_INITIALIZED overrides it in frames, per camera like the box
# delay ("5,02000000000B=3"): Camera B's road strip is in view for ~0.7 s (7 frames), so a
# passing car with a miss or two never initialised (live 08:48).
_MIN_INIT_DEFAULT = max(int(DETECT_FPS / 2), 2)
_MIN_INIT_SPEC = os.environ.get("AIPORT_MIN_INITIALIZED")


def _parse_min_initialized(spec):
    """(default frames, {deviceID: frames}) from AIPORT_MIN_INITIALIZED."""
    per = {}
    default = _MIN_INIT_DEFAULT
    for part in (spec or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            if "=" in part:
                dev, n = part.split("=", 1)
                per[dev.strip().upper()] = int(n)
            else:
                default = int(part)
        except ValueError:
            log.warning("ignoring bad AIPORT_MIN_INITIALIZED entry %r", part)
    return default, per


MIN_INITIALIZED, _MIN_INITIALIZED_PER = _parse_min_initialized(_MIN_INIT_SPEC)


def _min_initialized(device_id):
    return _MIN_INITIALIZED_PER.get(str(device_id).upper(), MIN_INITIALIZED)


def _parse_true_thresh(spec):
    """AIPORT_TRUE_THRESH: "<default>[,<objectType>=<value>...]" -> (default, {type: value})."""
    default, per = DEFAULT_TRUE_THRESH, {}
    for part in (spec or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            if "=" in part:
                name, value = part.split("=", 1)
                per[name.strip()] = float(value)
            else:
                default = float(part)
        except ValueError:
            log.warning("ignoring bad AIPORT_TRUE_THRESH entry %r", part)
    return default, per


_TRUE_THRESH_DEFAULT, _TRUE_THRESH = _parse_true_thresh(os.environ.get("AIPORT_TRUE_THRESH"))


def _true_thresh(object_type):
    return _TRUE_THRESH.get(object_type, _TRUE_THRESH_DEFAULT)


def _record_score(tr, score, device_id=None):
    """Add this frame's score (0.0 if missed) and latch true_positive once the median of the
    last SCORE_HISTORY passes the type's threshold."""
    history = tr.setdefault("score_history", [])
    history.append(score)
    del history[:-SCORE_HISTORY]
    if score > 0:
        tr["hits"] = tr.get("hits", 0) + 1
    tr["init_count"] = max(0, tr.get("init_count", 0) + (1 if score > 0 else -1))
    if tr["init_count"] > _min_initialized(device_id):
        tr["initialized"] = True
    if (not tr.get("true_positive") and tr.get("hits", 0) >= MIN_HITS
            and tr.get("initialized")):
        tr["true_positive"] = statistics.median(history) >= _true_thresh(tr["objectType"])


# A same-type detection where a track closed within RESUME_S (AIPORT_RESUME_S) resumes it
# (IoU >= RESUME_IOU with its last or first box): back into its event while that is still
# open, or quietly as parked for a vehicle that never moved -- the unloading car came back
# as a "new" vehicle three times. Moved = centre shifted > RESUME_MOVE_FRACTION of its size.
RESUME_S = float(os.environ.get("AIPORT_RESUME_S") or 60)
RESUME_IOU = 0.4
RESUME_MOVE_FRACTION = 0.25
RESUME_MAX = 32
# A face or plate is only looked for inside a person or vehicle box, so it confirms with
# that parent (it's seen once per snapshot, too rarely to confirm on its own).
CONFIRM_WITH = {"face": "person", "licensePlate": "vehicle"}
INSIDE_PARKED_FRACTION = 0.7
# Motion/scene gate: skip the classifier on a near-static frame with no open track.
# The gate asks motion.py (Frigate's motion detector) for motion boxes against a running
# background, so a person walking through Camera C's background (~0.5% of the frame) counts;
# the old whole-frame average diff missed them. AIPORT_MOTION_GATE=0 disables the gate.
# Protect's motion sensitivity and motion zones set its pixel threshold and mask;
# AIPORT_MOTION_THRESHOLD (1-255) overrides the threshold. Without OpenCV the old average
# diff is the fallback (AIPORT_MOTION_GATE_THRESH, default 0.02).
MOTION_GATE = os.environ.get("AIPORT_MOTION_GATE", "1").strip().lower() not in (
    "0", "false", "no", "off")
MOTION_GATE_THRESH = float(os.environ.get("AIPORT_MOTION_GATE_THRESH", "0.02"))
_GATE_STRIDE = 8  # sample every Nth pixel -> 80x80 grayscale for the diff
# The zone id reported when a track has none of its own (no zones set up, or it was never
# seen inside one, e.g. a parked object). Otherwise each track reports the zones it's in:
# live 2026-10-24 a web on Camera C's lens, whose feet fell in its lower zone (2), showed in
# Protect under the upper zone's name (zone 1) because every detection was reported as zone 1.
SMART_DETECT_ZONE_ID = "1"


def _track_zones(zone_ids):
    return list(zone_ids) if zone_ids else [int(SMART_DETECT_ZONE_ID)]


def _zones_visited(tr):
    """Every zone a track has been in, in order of first visit (its current zones if
    none were collected). A leave closes the event, so it names them all: live 10:10 on
    Camera C a walk from zone 2 through zone 3 into zone 1 closed as zone 1 only."""
    return tr.get("zones_seen") or tr.get("zone_ids")


def _zones_status(zone_lists, level, status):
    """zonesStatus for every zone the message's tracks name."""
    ids = list(dict.fromkeys(z for zones in zone_lists for z in _track_zones(zones)))
    return {str(z): {"level": level, "status": status} for z in ids}

# Detector frames (detectors/), latest only: ffmpeg decodes to the detector frame size as raw RGB.
_latest_frame = {}   # deviceID -> HxWx3 uint8 numpy array (RGB)
_latest_frame_seq = {}  # deviceID -> number of the frame in _latest_frame (1 = first of the pull)
# deviceID -> recent (seq, full-res JPEG) of the same frames, numbered alike: a track's best
# frame is the one it was detected in, not the next snapshot (a passing bus had moved on).
_frame_jpegs = {}
# deviceID -> (wall ms, monotonic ms) when _latest_frame was read from ffmpeg.
_latest_frame_time = {}
# deviceID -> monotonic s of its last frame from any pull. Unlike _latest_frame_time it
# survives a pull restart, so a brief reconnect isn't reported to Protect as not streaming:
# live 22:25 each such report made Protect resend settings and drop the relay again, every 15 s.
_last_frame_at = {}
# deviceID -> (wall ms, monotonic ms) of the frame the detector last analysed; every
# event time describes that frame, not the moment the event is sent.
_analysed_frame_time = {}
_frame_lock = threading.Lock()
_frame_cond = threading.Condition(_frame_lock)  # notified when a frame or its JPEG arrives
FRAME_JPEG_KEEP_S = 2.0
FRAME_JPEG_WAIT_S = 0.5  # how long a detection waits for its frame's JPEG
# Full-res JPEGs a second of the detection frames (AIPORT_FRAME_JPEG_FPS, 5 by default): one
# for every Nth detection frame, so a track's best frame is picked from those. Encoding one
# for every frame at 10 fps cost each camera's ffmpeg about a quarter of a core.
FRAME_JPEG_FPS = float(os.environ.get("AIPORT_FRAME_JPEG_FPS") or 5)
FRAME_JPEG_EVERY = max(1, round(DETECT_FPS / FRAME_JPEG_FPS)) if FRAME_JPEG_FPS > 0 else 1
_frame_jpeg_every = {}  # deviceID -> N of the running pull (a JPEG for detection frame 1, N+1, ...)
_stream_dims = {}    # deviceID -> (width, height) of the real camera stream
# deviceID -> capacity points of each stream UiStreamControl started. Kept apart from
# _active_streams, which briefly drops a device while its ffmpeg restarts.
_stream_points = {}
_stream_probe = {}   # deviceID -> {"codec", "width", "height", "fps"} from ffprobe at pull start
# deviceID -> exclusion polygons in the normalized 0-1000 space.
_exclude_zones = {}
# Prototype app exposes `zones` (not excludeZones): when non-default, a detection must be inside one.
_detect_zones = {}
# deviceID -> each detection zone's id (Protect's key), parallel to _detect_zones.
_detect_zone_ids = {}
# deviceID -> each detection zone's objectTypes (None = any), parallel to _detect_zones.
_detect_zone_types = {}
# deviceID -> Line Crossing segments parsed from ChangeSmartDetectSettings.lines.
_lines = {}
# Global monotonic counter for fullfov snapshot filenames. It must be global, not
# per-camera: two cameras' first leave would otherwise announce the same filename.
_snapshot_seq = itertools.count(1)
# Filename -> {"device_id", "kind", "coord"} so an upload maps back to its camera; bounded
# against leaks.
_snapshot_filename_to_device = collections.OrderedDict()
_SNAPSHOT_FILENAME_MAP_MAX = 500
# (monotonic_time, deviceID) per announced snapshot; fallback only when the GetRequest has
# no/unknown filename.
SNAPSHOT_FALLBACK_WINDOW_S = 60
_recent_snapshot_announcements = collections.deque(maxlen=32)
# deviceID -> last announced per-object coord, so a filename-less fallback can still crop.
_last_object_coord_by_device = {}
# Side of the square frames ffmpeg feeds the detector; _get_detector() sets it from
# the loaded backend's input_size (640 RKNN/CPU, typically 320 Edge TPU).
DETECT_IMG_SIZE = 640
# Per-object snapshot: square crop centered on the detection, ffmpeg-scaled to this size.
SMART_SNAPSHOT_SIZE = 512
SMART_SNAPSHOT_MARGIN = 1.6
# descriptors[].coord and derived geometry are normalized 0-1000, not stream pixels (controller clamps to [0,1000]).
CAMERA_COORD_END = 1000
_detector = None
_detector_lock = threading.Lock()
_enricher = None
_snapshot_cache = {}  # deviceID -> (mtime, BGR image) of the last decoded full-res snapshot
_snapshot_bytes = {}  # deviceID -> (mtime, JPEG bytes) of the latest full-res snapshot
# Records still holding a best-frame JPEG; older ones drop their bytes (memory bound).
BEST_FRAME_RECORDS_MAX = 40
_enrich_cache = {}    # deviceID -> (snapshot mtime, plate/face detections read from it)
# Third-party (ONVIF) cameras: Protect hands their video ingest to the AI Port,
# which must push each channel to :7550 (see onvif_push.py).
_onvif = onvif_push.PushManager(ffmpeg_bin=FFMPEG_BIN)
# [hardware] detector / detector_model, set by main(); see detectors/__init__.py. The env
# defaults also serve replay.py and the tests, which don't run main().
DETECTOR_BACKEND = (os.environ.get("AIPORT_DETECTOR") or os.environ.get("AIPORT_BACKEND")
                    or "auto").strip().lower()
DETECTOR_MODEL = os.environ.get("AIPORT_MODEL_PATH") or None


def _get_detector():
    global _detector, DETECT_IMG_SIZE
    with _detector_lock:
        if _detector is None:
            import detectors
            _detector = detectors.create(DETECTOR_BACKEND, DETECTOR_MODEL)
            # Streams started from here on decode at the backend's input size.
            DETECT_IMG_SIZE = _detector.input_size
        return _detector


def build_ssl_context():
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    ctx.load_cert_chain(CERT_PATH, KEY_PATH)
    return ctx


def send_msg(ws, functionName, payload, to="ubnt_avclient", in_response_to=None,
             response_expected=False, from_="UniFiVideo"):
    msg = {
        "from": from_,
        "to": to,
        "functionName": functionName,
        "messageId": next(_msg_id_counter),
        # ds's serde requires `timeStamp` as an RFC3339 string (a unix-ms integer is rejected).
        "timeStamp": datetime.now(timezone.utc).isoformat(),
        "payload": payload,
        "responseExpected": response_expected,
        # ds requires inResponseTo on every message (it drops EventSmartDetect
        # without it); 0 = "not a response".
        "inResponseTo": in_response_to if in_response_to is not None else 0,
    }
    # Controller sends/expects plain JSON as BINARY frames (opcode 0x2), not text.
    ws.send(json.dumps(msg).encode())
    log.info(">>> sent %s (id=%s) payload=%s", functionName, msg["messageId"], payload)


def _stop_stream(device_id):
    proc = _active_streams.pop(device_id, None)
    if proc and proc.poll() is None:
        log.info("stopping RTSP pull for deviceID=%s (pid=%s)", device_id, proc.pid)
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    _stream_started_monotonic_ms.pop(device_id, None)
    # Not _stream_restart_count: a pull that dies restarts through here, and clearing it kept
    # the backoff at its first step. UiStreamControl resets it.
    for table in (_stream_dims, _stream_probe):
        table.pop(device_id, None)
    try:
        os.remove(os.path.join(_STREAM_DIR, f"{device_id}.jpg"))
    except OSError:
        pass
    with _frame_lock:
        _latest_frame.pop(device_id, None)
        _latest_frame_seq.pop(device_id, None)
        _frame_jpegs.pop(device_id, None)
        _frame_jpeg_every.pop(device_id, None)
        _latest_frame_time.pop(device_id, None)
        _analysed_frame_time.pop(device_id, None)
    _motion_detectors.pop(device_id, None)  # a new stream starts a new background
    _startup_scan.pop(device_id, None)
    _last_parked_check.pop(device_id, None)
    _redo_cache.pop(device_id, None)
    # The startup window starts again on the new stream's first frame: live 11:47 on
    # Camera B the RTSP pull dropped 3 s in and the window ran out before frames came back,
    # so the parked car opened an event instead of parking.
    with _motion_lock:
        dev = _motion_state.get(device_id)
        if dev is not None:
            dev["started_ms"] = None


def _jpeg_end(buf):
    """End offset of the JPEG starting at buf[0], or -1 while it is incomplete. Header
    segments are skipped by length (a table can hold FF D9); in the scan data 0xFF is
    byte-stuffed, so there FF D9 is the end marker."""
    pos = 2
    while pos + 4 <= len(buf):
        if buf[pos] != 0xFF:
            raise ValueError("bad JPEG marker at offset %d" % pos)
        marker = buf[pos + 1]
        if marker == 0xFF:  # fill byte
            pos += 1
            continue
        seg = (buf[pos + 2] << 8) | buf[pos + 3]
        if marker == 0xDA:  # start of scan
            end = buf.find(b"\xff\xd9", pos + 2 + seg)
            return -1 if end < 0 else end + 2
        pos += 2 + seg
    return -1


def _jpeg_frames(stream):
    """Split ffmpeg's image2pipe MJPEG output into one JPEG per frame."""
    buf = bytearray()
    while True:
        chunk = stream.read1(1 << 16)
        if not chunk:
            return
        buf += chunk
        while True:
            start = buf.find(b"\xff\xd8")
            if start < 0:
                del buf[:-1]  # the last byte may be the first half of the SOI
                break
            if start:
                del buf[:start]
            try:
                end = _jpeg_end(buf)
            except ValueError:
                del buf[:2]  # resync on the next SOI
                continue
            if end < 0:
                break
            yield bytes(buf[:end])
            del buf[:end]


def _tmp_dir():
    """This instance's scratch folder, inside its own stream dir (one per --state-dir, so
    AI Ports sharing a host never touch each other's files); emptied at startup with the
    rest of that dir (_reset_stream_dir). Made on
    use: macOS's periodic /tmp cleanup can remove it while pi-port runs."""
    path = os.path.join(_STREAM_DIR, "tmp")
    os.makedirs(path, exist_ok=True)
    return path


def _write_snapshot(path, data):
    """Replace the camera's snapshot file in one step, so a reader never sees half of it."""
    fd, tmp = tempfile.mkstemp(suffix=".jpg", dir=_tmp_dir())
    try:
        os.fchmod(fd, 0o644)  # mkstemp's 0600; ffmpeg wrote the snapshot world-readable
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        os.remove(tmp)
        raise


def _reset_stream_dir():
    """Empty this instance's stream dir at startup: its scratch folder (whatever a crash left
    there: a half-written snapshot, a crop input) and every camera's snapshot, including those
    of cameras no longer paired. A paired camera's is rewritten once its stream starts."""
    shutil.rmtree(_STREAM_DIR, ignore_errors=True)
    _tmp_dir()


def _frame_jpeg(device_id, seq, timeout=FRAME_JPEG_WAIT_S):
    """Full-res JPEG of detection frame `seq` (the same decoded frame the detector saw),
    waiting briefly for the encoder to catch up; None once it is gone or never comes."""
    deadline = time.monotonic() + timeout
    with _frame_cond:
        if (seq - 1) % _frame_jpeg_every.get(device_id, 1):
            return None  # no JPEG is made of this frame
        while True:
            jpegs = _frame_jpegs.get(device_id) or ()
            for s, data in jpegs:
                if s == seq:
                    return data
            if jpegs and jpegs[-1][0] >= seq:
                return None
            left = deadline - time.monotonic()
            if left <= 0:
                return None
            _frame_cond.wait(left)


# Region detection (Frigate's): the detector frame keeps the camera's aspect ratio, its long
# side AIPORT_DETECT_SIZE, and the model runs on square crops around motion and tracks
# (regions.py). Otherwise the whole frame is squeezed into DETECT_IMG_SIZE x DETECT_IMG_SIZE
# and detected at once (the Pi/RK3588 NPU path). On by default on macOS.
REGIONS = os.environ.get("AIPORT_REGIONS", "1" if sys.platform == "darwin" else "0"
                         ).strip().lower() not in ("0", "false", "no", "off")
DETECT_SIZE = int(os.environ.get("AIPORT_DETECT_SIZE") or 1280)
PARKED_RECHECK_S = 10.0  # a parked object is re-detected this often (Frigate: 10 s of frames)


def _detect_dims(width=None, height=None):
    """(width, height) of the detector frame for a stream of this size."""
    if not REGIONS or not width or not height:
        return DETECT_IMG_SIZE, DETECT_IMG_SIZE
    scale = min(1.0, DETECT_SIZE / float(max(width, height)))
    return (max(2, int(round(width * scale / 2)) * 2), max(2, int(round(height * scale / 2)) * 2))


# Scaling detector frames on the GPU (AIPORT_HW_SCALE, on by default on macOS): VideoToolbox's
# scale_vt instead of ffmpeg's CPU scaler. A VideoToolbox-decoded stream stays on the GPU until
# it is scaled; a software-decoded one is uploaded for it. Measured on a 2560x1440 camera at
# 10 fps: the detector branch went from ~12% of a core to ~2% (HEVC) and ~6% (H.264).
HW_SCALE = os.environ.get("AIPORT_HW_SCALE", "1" if sys.platform == "darwin" else "0"
                          ).strip().lower() not in ("0", "false", "no", "off")
_hw_scale_failed = set()  # deviceIDs whose pull died with GPU scaling before any frame


# What ffmpeg says when GPU scaling itself fails, as opposed to the stream: live 2026-10-28
# Protect's relay ended two fresh pulls ("Failed reading RTSP data: End of file") and an
# unreachable ONVIF camera refused them ("No route to host"), and each such camera was moved to
# CPU scaling for good.
_GPU_SCALING_ERRORS = ("scale_vt", "hwupload", "hwdownload", "videotoolbox", "hw_frames",
                       "Impossible to convert", "reinitializing filters",
                       "Failed to configure")


def _gpu_scaling_error(stderr):
    """True if a pull's stderr shows the GPU scaling path failing."""
    return any(marker in (stderr or "") for marker in _GPU_SCALING_ERRORS)


def _restart_wanted(device_id):
    """A pull that died restarts only while Protect still wants the stream. Live 2026-10-28
    Protect ended its relays, then stopped every camera while their pulls waited to restart:
    the stop found no pull to stop, and each camera was pulled again every 20 s after that."""
    return device_id not in _active_streams and device_id in _stream_points


def _hw_scale_mode(hwdec_args, device_id=None):
    """'vt' (decoded on the GPU), 'vt_upload' (decoded in software) or None (CPU scaling)."""
    if not HW_SCALE or sys.platform != "darwin" or device_id in _hw_scale_failed:
        return None
    return "vt" if "videotoolbox" in hwdec_args else "vt_upload"


def _pull_video_args(codec, det_w, det_h, device_id=None):
    """(ffmpeg args before -i, filter graph, GPU scaling mode, JPEG every Nth frame) for a
    stream of this codec. replay.py builds its pull from this too."""
    hwdec = _hwdec_args(codec)
    hw = _hw_scale_mode(hwdec, device_id)
    pre = list(hwdec)
    if hw == "vt":
        pre += ["-hwaccel_output_format", "videotoolbox_vld"]
    elif hw == "vt_upload":
        pre += ["-init_hw_device", "videotoolbox=vt", "-filter_hw_device", "vt"]
    return pre, _detect_filter_graph(det_w, det_h, hw, FRAME_JPEG_EVERY), hw, FRAME_JPEG_EVERY


def _detect_filter_graph(width=DETECT_IMG_SIZE, height=None, hw=None, jpeg_every=1):
    """ffmpeg filter graph for the detection pipeline: [jpg] full-res frames (every
    `jpeg_every`th one), [det] all of them scaled for the detector, on the GPU for hw
    'vt'/'vt_upload'. replay.py runs clips through this same graph."""
    height = height or width
    jpg = f"select=not(mod(n\\,{jpeg_every}))" if jpeg_every > 1 else "null"
    if hw == "vt":
        jpg += ",hwdownload,format=nv12"
        det = f"scale_vt={width}:{height},hwdownload,format=nv12"
    elif hw == "vt_upload":
        det = f"format=nv12,hwupload,scale_vt={width}:{height},hwdownload,format=nv12"
    else:
        det = f"scale={width}:{height}"
    return f"[0:v]fps={DETECT_FPS},split=2[jpgin][detin];[jpgin]{jpg}[jpg];[detin]{det}[det]"


# One ffmpeg pulls RTSP and splits post-decode into a full-res JPEG and an RGB
# copy of each detection frame; decoding twice was wasteful (CPU-side NAL parsing
# dominates, not the NPU). Both outputs come after one fps filter, so frame n of
# each is the same picture.
def _start_stream(device_id, ip, port, uri, width=None, height=None, url=None):
    """Pull from Protect's relay (ip/port/uri), or from `url` directly -- an
    ONVIF camera's own RTSP URL."""
    _stop_stream(device_id)
    os.makedirs(_STREAM_DIR, exist_ok=True)
    _stream_dims[device_id] = (width or DETECT_IMG_SIZE, height or DETECT_IMG_SIZE)
    url = url or f"rtsp://{ip}:{port}/{uri}"
    out_path = os.path.join(_STREAM_DIR, f"{device_id}.jpg")
    probe = _probe_stream(url)
    _stream_probe[device_id] = probe
    if probe["width"] and probe["height"]:
        # What the stream really is beats what the controller announced.
        _stream_dims[device_id] = (probe["width"], probe["height"])
    codec = probe["codec"]
    det_w, det_h = _detect_dims(width, height)
    pre_args, graph, hw, jpeg_every = _pull_video_args(codec, det_w, det_h, device_id)
    encode_args = ["-c:v", "mjpeg_rkmpp"] if FFMPEG_BIN != "ffmpeg" else ["-c:v", "mjpeg"]
    # Plate/face reads use the snapshot file, so refresh it every second when they're on.
    snap_interval = 1.0 if enrich.smart_detect_features() else 2.0
    jpeg_r, jpeg_w = os.pipe()
    cmd = [
        FFMPEG_BIN, "-loglevel", "warning", "-rtsp_transport", "tcp", *_rtsp_timeout_args(),
        # Low-latency decode (no B-frame reorder delay). Not "-fflags nobuffer": with this
        # split/fps pipeline it left the rawvideo detection output empty (ffmpeg 8).
        "-flags", "low_delay",
        *pre_args, "-i", url,
        "-filter_complex", graph,
        # passthrough on both: each output gets exactly the graph's frames. Otherwise each
        # pads or drops frames to its own constant rate (image2pipe repeated the ones `select`
        # drops), and live the two numberings drifted apart: JPEG n came 0.7 s before
        # detection frame n, or with `select` 2.6 s after it (each detection then waited out
        # FRAME_JPEG_WAIT_S, and Porch fell to 3 analysed frames a second).
        "-map", "[jpg]", *encode_args, "-fps_mode", "passthrough", "-f", "image2pipe",
        f"pipe:{jpeg_w}",
        "-map", "[det]", "-fps_mode", "passthrough", "-pix_fmt", "rgb24", "-f", "rawvideo", "-",
    ]
    log.info("starting merged RTSP pull for deviceID=%s: %s (codec=%s, GPU scaling %s, "
             "JPEG every %d frames)", device_id, url, codec, hw or "off", jpeg_every)
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                pass_fds=(jpeg_w,))
    finally:
        os.close(jpeg_w)
    jpeg_pipe = os.fdopen(jpeg_r, "rb")
    stderr_tail = start_stderr_drain(proc)
    _active_streams[device_id] = proc
    _start_camera_clock(device_id, url)
    # Origin for this device's clockMonotonic/clockStream (ms since the pull began).
    _stream_started_monotonic_ms[device_id] = int(time.monotonic() * 1000)
    frame_bytes = det_w * det_h * 3
    jpegs = collections.deque(maxlen=max(2, int(DETECT_FPS * FRAME_JPEG_KEEP_S / jpeg_every)))
    with _frame_lock:
        _frame_jpegs[device_id] = jpegs
        _frame_jpeg_every[device_id] = jpeg_every
        _latest_frame_seq.pop(device_id, None)

    def _jpeg_reader():
        seq, last_write = 0, None
        try:
            for data in _jpeg_frames(jpeg_pipe):
                seq += 1 if seq == 0 else jpeg_every  # numbered as the detection frame it is
                with _frame_cond:
                    jpegs.append((seq, data))
                    _frame_cond.notify_all()
                now = time.monotonic()
                if last_write is None or now - last_write >= snap_interval:
                    _write_snapshot(out_path, data)
                    last_write = now
        except Exception:
            log.exception("JPEG reader crashed for deviceID=%s", device_id)
        finally:
            jpeg_pipe.close()

    def _reader():
        import numpy as np
        seq = 0
        try:
            while True:
                buf = proc.stdout.read(frame_bytes)
                if len(buf) < frame_bytes:
                    break
                # Stamp the frame as it leaves ffmpeg: events describe this moment, not
                # the later tick that analyses it.
                stamp = _now_ms()
                frame = np.frombuffer(buf, dtype=np.uint8).reshape((det_h, det_w, 3))
                seq += 1
                with _frame_cond:
                    _latest_frame[device_id] = frame
                    _latest_frame_seq[device_id] = seq
                    _latest_frame_time[device_id] = stamp
                    _last_frame_at[device_id] = stamp[1] / 1000.0
                    _frame_cond.notify_all()   # wakes the camera's poll loop
        except Exception:
            log.exception("frame reader crashed for deviceID=%s", device_id)
        rc = proc.wait()
        stderr = stderr_tail()
        log.info("merged RTSP pull for deviceID=%s exited rc=%s stderr_tail=%s",
                  device_id, rc, stderr[-500:])
        if hw and seq == 0 and _active_streams.get(device_id) is proc \
                and _gpu_scaling_error(stderr):
            log.warning("deviceID=%s: no frames with GPU scaling, scaling on the CPU from now on",
                        device_id)
            _hw_scale_failed.add(device_id)
        # Only restart on an unexpected death: _stop_stream() pops the device first,
        # so finding `proc` here means it died on its own. No retry cap.
        if _active_streams.get(device_id) is proc:
            _active_streams.pop(device_id, None)
            # 0.5s for the first attempts: Protect rebuilds its ingest in ~2-3s and
            # a fixed 2s backoff lost the race. Counter resets on UiStreamControl.
            # Consecutive pulls without a frame: one that delivered video starts the count
            # again, so a relay drop now and then keeps the quick restart. A source that keeps
            # refusing backs off to STREAM_RETRY_MAX_S: live 2026-10-28 an unreachable ONVIF
            # camera was retried every 2 s, 108 times in 4 minutes.
            count = (1 if seq else _stream_restart_count.get(device_id, 0) + 1)
            _stream_restart_count[device_id] = count
            backoff = 0.5 if count <= 6 else min(STREAM_RETRY_MAX_S, 2.0 ** (count - 6))
            log.warning("deviceID=%s RTSP pull died unexpectedly (attempt %d), restarting in %.1fs",
                        device_id, count, backoff)
            time.sleep(backoff)
            if _restart_wanted(device_id):
                _start_stream(device_id, ip, port, uri, width, height, url=url)
            else:
                log.info("deviceID=%s: stream stopped by Protect during the restart wait; "
                         "not restarting", device_id)

    threading.Thread(target=_jpeg_reader, daemon=True).start()
    threading.Thread(target=_reader, daemon=True).start()


def _point_in_polygon(x, y, polygon):
    """Ray-casting point-in-polygon test (zone coord is 4 free, non-axis-aligned points)."""
    inside = False
    n = len(polygon)
    x1, y1 = polygon[-1]
    for i in range(n):
        x2, y2 = polygon[i]
        if ((y1 > y) != (y2 > y)) and (x < (x2 - x1) * (y - y1) / (y2 - y1) + x1):
            inside = not inside
        x1, y1 = x2, y2
    return inside


def _zone_center_norm(coord):
    """Center of a [x, y, w, h] box in normalized 0-1000 space."""
    x, y, w, h = coord
    return x + w / 2, y + h / 2


# Protect draws 0-1000 coords (and the user's zones/lines) on a 16:9 canvas the height
# of the video; a camera of another aspect sits centred in it. Seen on a 1920x2560
# doorbell: boxes normalised to the portrait image landed ~2.4x too far from centre.
PROTECT_CANVAS_ASPECT = 16 / 9


def _canvas_scale(device_id):
    """(sx, sy) mapping image-normalised coords onto Protect's 16:9 canvas."""
    w, h = _stream_dims.get(device_id, (0, 0))
    if not w or not h or abs(w / h - PROTECT_CANVAS_ASPECT) < 0.02:
        return 1.0, 1.0
    a = w / h
    if a < PROTECT_CANVAS_ASPECT:  # narrower (portrait, 4:3): pillarboxed
        return a / PROTECT_CANVAS_ASPECT, 1.0
    return 1.0, PROTECT_CANVAS_ASPECT / a  # wider: letterboxed


def _to_protect(device_id, coord):
    """Image-normalised [x, y, w, h] -> Protect canvas [x, y, w, h] (0-1000)."""
    sx, sy = _canvas_scale(device_id)
    x, y, w, h = coord
    half = CAMERA_COORD_END / 2
    return [half + (x - half) * sx, half + (y - half) * sy, w * sx, h * sy]


def _protect_center(device_id, coord):
    """Box center in Protect's canvas space, where zones and lines are drawn."""
    return _zone_center_norm(_to_protect(device_id, coord))


def _protect_foot(device_id, coord):
    """Bottom-centre of the box in Protect's canvas space: where the object stands, which
    zones are tested against (as in Frigate). Line crossing keeps using the centre. Seen
    live: a person walking along Camera C's hedge had their box centre near the edge of
    zone 2 while their feet were well inside it."""
    x, y, w, h = _to_protect(device_id, coord)
    return x + w / 2.0, y + h


def _is_excluded(device_id, coord):
    """True if the detection's bottom-centre falls inside any configured excludeZones."""
    polygons = _exclude_zones.get(device_id)
    if not polygons:
        return False
    cx, cy = _protect_foot(device_id, coord)
    return any(_point_in_polygon(cx, cy, poly) for poly in polygons)


# Something on the lens: live 2026-10-23 18:58 to 04:15 a spider web on Camera C
# gave 218 person events, every box against the right edge and the top and nearly the full
# height ([768, 3, 231, 949] in 0-1000 image space); 2026-10-28 07:21 a strand lit by the IR
# came from the bottom-right corner instead (about [787, 397, 213, 603]). A person box in a
# corner of the frame -- touching two adjacent edges, the top or the bottom and a side -- and
# spanning LENS_MIN_HEIGHT of it is dropped on the cameras named in AIPORT_LENS_FILTER
# (deviceIDs, or "all"; unset = off).
LENS_EDGE = 10
LENS_MIN_HEIGHT = int(os.environ.get("AIPORT_LENS_MIN_HEIGHT", "900"))
_LENS_FILTER = {v.strip().upper() for v in os.environ.get("AIPORT_LENS_FILTER", "").split(",")
                if v.strip()}


def _lens_filter_on(device_id):
    return bool(_LENS_FILTER) and ("ALL" in _LENS_FILTER
                                   or str(device_id).upper() in _LENS_FILTER)


def _frame_corner(coord):
    """The frame corner a 0-1000 box touches ("TL", "TR", "BL", "BR": two adjacent edges), or
    None."""
    x, y, w, h = coord
    v = "T" if y <= LENS_EDGE else "B" if y + h >= CAMERA_COORD_END - LENS_EDGE else None
    s = "L" if x <= LENS_EDGE else "R" if x + w >= CAMERA_COORD_END - LENS_EDGE else None
    return v + s if v and s else None


def _on_lens(device_id, object_type, coord):
    if object_type != "person" or not _lens_filter_on(device_id):
        return False
    return _frame_corner(coord) is not None and coord[3] >= LENS_MIN_HEIGHT


# A person track that starts in a corner of the frame is held out of the event, as a track
# outside the zones is, until one of its boxes leaves that corner; then it joins with its
# history. On AIPORT_LENS_FILTER's cameras. A web on the lens never leaves its corner,
# whatever its height: in the logs from 2026-10-16 to 10-27 that held all 218 of Camera C's
# web tracks (24 of them under LENS_MIN_HEIGHT), and one track each on two other cameras.
# A person who starts in a corner and walks out joins once they do (90% within 3 s).
def _corner_held(device_id, tr, d):
    """True while this person track has only been seen in the frame corner it started in."""
    if tr["objectType"] != "person" or d.get("seeded") or not _lens_filter_on(device_id):
        return bool(tr.get("corner_hold"))
    if "corner_hold" not in tr:
        tr["corner_hold"] = _frame_corner(d["coord"])
    elif tr["corner_hold"] and _frame_corner(d["coord"]) != tr["corner_hold"]:
        tr["corner_hold"] = None   # latched: it has left the corner
        tr["left_corner"] = True
    return bool(tr["corner_hold"])


# A detection counts in a zone listing what it confirms with (a plate rides its vehicle).
ZONE_TYPE_ALIASES = {"licensePlate": ("licensePlate", "vehicle")}


def _zone_allows(types, object_type):
    if types is None or object_type is None:
        return True
    return any(t in types for t in ZONE_TYPE_ALIASES.get(object_type, (object_type,)))


def _zones_of(device_id, coord, object_type=None):
    """Ids of the zones listing object_type that hold the detection's bottom-centre; [] if
    none (or no zones set up)."""
    polygons = _detect_zones.get(device_id)
    if not polygons:
        return []
    types = _detect_zone_types.get(device_id) or [None] * len(polygons)
    ids = _detect_zone_ids.get(device_id) or list(range(1, len(polygons) + 1))
    cx, cy = _protect_foot(device_id, coord)
    return [zid for poly, t, zid in zip(polygons, types, ids)
            if _zone_allows(t, object_type) and _point_in_polygon(cx, cy, poly)]


def _is_outside_detect_zones(device_id, coord, object_type=None):
    """True if the detection's bottom-centre is outside every configured `zones` polygon
    that lists its objectType (no zones means no restriction): live 16:47 on Camera B two
    vehicles were reported from a zone set to person/animal/face."""
    polygons = _detect_zones.get(device_id)
    if not polygons:
        return False
    types = _detect_zone_types.get(device_id) or [None] * len(polygons)
    cx, cy = _protect_foot(device_id, coord)
    return not any(_zone_allows(t, object_type) and _point_in_polygon(cx, cy, poly)
                   for poly, t in zip(polygons, types))


CLIPPED_REDETECT_MAX = 3   # extra looks a frame may take at boxes cut off by their crop
# deviceID -> {"first", "last"}: monotonic s of the stream's first frame and last full look.
_startup_scan = {}
STARTUP_SCAN_INTERVAL_S = 1.0
_last_parked_check = {}    # deviceID -> monotonic s when parked objects were last re-detected
# A cut-off box that hasn't moved gets the same answer from its second look, frame after frame:
# on an empty road at Camera B a box on something fixed at the top and two parked cars took
# 2.8 extra looks a frame. The whole object it found is reused for REDO_REUSE_S, unless motion
# touches the box. Reusing everything in the crop replayed a person beside a parked car in
# their old spot, as a second track.
REDO_REUSE_S = PARKED_RECHECK_S
REDO_REUSE_IOU = 0.8
_redo_cache = {}   # deviceID -> [(monotonic s, label, cut-off box, detections of its second look)]


def _box_iou(a, b):
    """IoU of two (x1, y1, x2, y2) boxes."""
    import regions
    inter = regions.intersection(a, b)
    if inter is None:
        return 0.0
    i = regions.area(inter)
    return i / float(regions.area(a) + regions.area(b) - i)


def _whole_of(d, got):
    """The detection in a second look that is the cut-off box's whole object: same label, the
    cut-off box mostly inside it. Only it is reused; anything else in that crop may move."""
    import regions
    best = None
    for g in got:
        inter = regions.intersection(d["box"], g["box"])
        if g["label"] != d["label"] or inter is None:
            continue
        cover = regions.area(inter) / float(regions.area(d["box"]))
        if cover >= REDO_REUSE_IOU and (best is None or g["score"] > best["score"]):
            best = g
    return best


def _reused_redo(device_id, d, now, motion):
    """Detections of an earlier second look at this same cut-off box, or None."""
    import regions
    if regions.intersects_any(d["box"], motion):
        return None
    for t, label, box, got in _redo_cache.get(device_id, ()):
        if (now - t < REDO_REUSE_S and label == d["label"]
                and _box_iou(box, d["box"]) >= REDO_REUSE_IOU):
            return got
    return None


def _detect_regions(device_id, detector, frame):
    """Detections for this frame from square regions around motion and tracked objects
    (Frigate's process_frames), in detector-frame pixels. Parked objects are re-detected
    every PARKED_RECHECK_S or when motion touches them; otherwise they are passed on as
    their last box ("seeded"). The first frame of a stream gets a full startup look."""
    import regions
    shape = frame.shape
    now = time.monotonic()
    with _motion_lock:
        tracks = list(((_motion_state.get(device_id) or {}).get("tracks") or {}).items())
    det = _motion_detectors.get(device_id)
    motion = [] if det is None or det.is_calibrating() else list(_motion_boxes.get(device_id, []))
    recheck = now - _last_parked_check.get(device_id, 0.0) >= PARKED_RECHECK_S
    if recheck:
        _last_parked_check[device_id] = now
    object_boxes, seeded = [], []
    for tid, tr in tracks:
        box = _coord_px(tr["coord"], shape)
        if tr.get("stationary_coord") and not recheck and not regions.intersects_any(box, motion):
            # Its parked spot, not its last box: repeating a recheck's slightly different box
            # for seconds counted as the object moving.
            seeded.append((tid, tr, _coord_px(tr["stationary_coord"], shape)))
        else:
            object_boxes.append(box)
    found = regions.regions_for(shape, object_boxes)
    found += regions.regions_for(shape, [b for b in motion if not regions.inside_any(b, found)])
    # A full look about once a second through the startup window, not just on the first
    # frame: on one coarse look a parked car at Camera A scored too low, so it wasn't
    # parked at start and later opened an event as an arrival.
    scan = _startup_scan.setdefault(device_id, {"first": now, "last": None})
    if (now - scan["first"] <= STARTUP_WINDOW_MS / 1000.0
            and (scan["last"] is None or now - scan["last"] >= STARTUP_SCAN_INTERVAL_S)):
        scan["last"] = now
        found += regions.startup_regions(shape)
    dets = []

    def run(region):
        x1, y1, x2, y2 = (int(v) for v in region)
        got = []
        _detector_calls[device_id] += 1
        for r in detector.detect(frame[y1:y2, x1:x2]):
            bx1, by1, bx2, by2 = r["box"]
            got.append(dict(r, label=r.get("label") or r["objectType"], region=region,
                            box=(bx1 + x1, by1 + y1, bx2 + x1, by2 + y1)))
        return got

    for region in found:
        dets += run(region)
    # A box cut off by its crop's edge (not the picture's) is looked at again on a crop twice
    # its size, as older Frigate did: live 12:59 on Camera B a crop placed for a road car cut
    # through a large object at the top, and its cut-off box took over the car's track.
    # A box still cut off after that gets another look (up to CLIPPED_REDETECT_MAX a frame).
    redone = []
    cache = [e for e in _redo_cache.get(device_id, ()) if now - e[0] < REDO_REUSE_S]
    _redo_cache[device_id] = cache
    queue = sorted([d for d in dets if regions.clipped(d, shape)], key=lambda d: -d["score"])
    while queue and len(redone) < CLIPPED_REDETECT_MAX:
        d = queue.pop(0)
        got = _reused_redo(device_id, d, now, motion)
        if got is not None:
            dets += got
            continue
        bx1, by1, bx2, by2 = d["box"]
        region = regions.calculate_region(shape, bx1, by1, bx2, by2, regions.MIN_REGION,
                                          multiplier=2)
        if region in redone:
            continue
        redone.append(region)
        got = run(region)
        whole = _whole_of(d, got)
        if whole is not None:
            cache.append((now, d["label"], d["box"], [dict(whole, reused=True)]))
        dets += got
        queue = sorted(queue + [g for g in got if regions.clipped(g, shape)],
                       key=lambda d: -d["score"])
    out = regions.reduce_detections(shape, dets)
    for r in out:
        r["clipped"] = regions.clipped(r, shape)
    out.sort(key=lambda r: -r["score"])
    for tid, tr, box in seeded:
        out.append({"objectType": tr["objectType"], "label": None, "score": tr["score"],
                    "box": box, "seeded": tid})
    return out


def _classify_frame_detections(device_id):
    """Return all qualifying detections for the latest frame as
    {objectType, score, coord} in normalized 0-1000 space, sorted by score;
    [] if none."""
    with _frame_lock:
        frame = _latest_frame.get(device_id)
        seq = _latest_frame_seq.get(device_id)
        if frame is not None:
            _analysed_frame_time[device_id] = _latest_frame_time.get(device_id) or _now_ms()
    if frame is None or DETECTOR_BACKEND == "none":
        return []
    try:
        detector = _get_detector()
        if REGIONS:
            results = _detect_regions(device_id, detector, frame)
        else:
            _detector_calls[device_id] += 1
            results = detector.detect(frame)
    except Exception:
        log.exception("classification failed for deviceID=%s", device_id)
        return []
    # Boxes are in detector-frame pixels; coords are 0-1000 on each axis of the image.
    sx = CAMERA_COORD_END / frame.shape[1]
    sy = CAMERA_COORD_END / frame.shape[0]
    out = []
    for r in results:
        x1, y1, x2, y2 = r["box"]
        coord = [max(0, x1 * sx), max(0, y1 * sy), max(1, (x2 - x1) * sx), max(1, (y2 - y1) * sy)]
        if r.get("seeded"):
            # A parked object not re-detected this frame keeps its last box (Frigate).
            out.append({"objectType": r["objectType"], "score": r["score"], "coord": coord,
                        "seeded": r["seeded"]})
            continue
        if _is_excluded(device_id, coord):
            _log_dropped(device_id, r, "excluded by configured excludeZones")
            continue
        if _on_lens(device_id, r["objectType"], coord):
            _log_dropped(device_id, r, "on the lens (AIPORT_LENS_FILTER)")
            continue
        d = {"objectType": r["objectType"], "score": r["score"], "coord": coord}
        if r.get("clipped"):
            d["clipped"] = True
        if r.get("reused"):
            d["reused"] = True  # an earlier look's box: it mustn't become the best frame
        if r["objectType"] == "vehicle" and enrich.vehicle_attrs_enabled():
            try:
                attrs = enrich.vehicle_attributes(r.get("label"), frame, coord)
            except Exception:
                log.exception("vehicle attributes failed for deviceID=%s", device_id)
                attrs = None
            if attrs:
                d["extra"] = {"attributes": attrs}
        out.append(d)
    # The full-res JPEG of this same frame: a track's best frame (and its thumbnail) comes
    # from it. Without one, _capture_best falls back to the latest snapshot. Parked objects
    # passed on as their last box never take a best frame, so they don't wait for it: live,
    # a camera with a parked trailer waited on every frame and its passes doubled.
    wanted = any(not d.get("seeded") for d in out)
    jpeg = _frame_jpeg(device_id, seq) if wanted and seq is not None else None
    if jpeg is not None:
        for d in out:
            d["frame"] = (seq, jpeg)
    return out


def _get_enricher():
    global _enricher
    with _detector_lock:
        if _enricher is None:
            faces_dir = os.environ.get("AIPORT_FACES_DIR") or \
                os.path.join(STATE_DIR or HERE, "faces")
            _enricher = enrich.Enricher(faces_dir)
        return _enricher


def _read_snapshot(device_id):
    """(mtime, JPEG bytes) of the latest full-res snapshot, re-read only when the
    file changed; (None, None) if unavailable. Face reads and best-frame capture
    share it, so a face box always refers to the exact bytes it was found in."""
    path = os.path.join(_STREAM_DIR, f"{device_id}.jpg")
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return None, None
    cached = _snapshot_bytes.get(device_id)
    if cached and cached[0] == mtime:
        return cached
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return None, None
    if not data.endswith(b"\xff\xd9"):  # torn write; the next tick retries
        return None, None
    _snapshot_bytes[device_id] = (mtime, data)
    return mtime, data


def _load_snapshot_bgr(device_id):
    """Latest full-res snapshot (BGR) for plate/face reads, decoded once per
    snapshot; (None, None) if unavailable."""
    mtime, data = _read_snapshot(device_id)
    if data is None:
        return None, None
    cached = _snapshot_cache.get(device_id)
    if cached and cached[0] == mtime:
        return cached[1], mtime
    import cv2
    import numpy as np
    img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    if img is not None:
        _snapshot_cache[device_id] = (mtime, img)
    return img, mtime


# Each confirmed face track's best crop is saved to <state-dir>/faces-seen/ when it closes, to
# pick reference photos from: move a person's into <state-dir>/faces/<Name>/ (the gallery
# enrich.py names faces from). The folders are world-writable because pi-port runs as root.
FACES_SEEN_MAX = 500
FACE_CROP_MARGIN = 0.5  # of the face box on each side, so the gallery's detector finds it


def _faces_dirs():
    base = STATE_DIR or HERE
    return (os.environ.get("AIPORT_FACES_SEEN_DIR") or os.path.join(base, "faces-seen"),
            os.environ.get("AIPORT_FACES_DIR") or os.path.join(base, "faces"))


def _open_dir(path):
    os.makedirs(path, exist_ok=True)
    try:
        os.chmod(path, 0o777)
    except OSError:
        pass


def _save_face_crop(device_id, tracker_id, tr):
    """Write a closing face track's best moment, cropped around the face, to faces-seen/."""
    best = tr.get("best")
    if tr.get("objectType") != "face" or not best or not best.get("jpeg"):
        return
    try:
        import cv2
        import numpy as np
        img = cv2.imdecode(np.frombuffer(best["jpeg"], np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            return
        h, w = img.shape[:2]
        x, y, bw, bh = best["coord"]
        s = CAMERA_COORD_END
        mx, my = bw * FACE_CROP_MARGIN, bh * FACE_CROP_MARGIN
        x1, y1 = max(0, int((x - mx) * w / s)), max(0, int((y - my) * h / s))
        x2, y2 = min(w, int((x + bw + mx) * w / s)), min(h, int((y + bh + my) * h / s))
        if x2 - x1 < 8 or y2 - y1 < 8:
            return
        ok, jpg = cv2.imencode(".jpg", img[y1:y2, x1:x2], [cv2.IMWRITE_JPEG_QUALITY, 95])
        if not ok:
            return
        seen_dir, faces_dir = _faces_dirs()
        _open_dir(seen_dir)
        _open_dir(faces_dir)
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime((best.get("wall") or 0) / 1000))
        path = os.path.join(seen_dir, f"{stamp}_{device_id}_t{tracker_id}_{best['score']:.2f}.jpg")
        with open(path, "wb") as f:
            f.write(jpg.tobytes())
        os.chmod(path, 0o666)
        names = sorted(n for n in os.listdir(seen_dir) if n.endswith(".jpg"))
        for old in names[:-FACES_SEEN_MAX]:
            os.remove(os.path.join(seen_dir, old))
        log.info("face crop saved for deviceID=%s tracker_id=%s: %s", device_id, tracker_id, path)
    except Exception:
        log.exception("saving face crop failed for deviceID=%s tracker_id=%s", device_id,
                      tracker_id)


# Thumbnail choice, as Frigate's is_better_thumbnail (frigate/util/image.py, MIT): a frame cut
# off by the picture's edge never replaces one that isn't; otherwise a frame wins with a score
# over 5% higher or a box over 10% larger. Live 12:00 at Camera A the top-scoring frame was a
# shoulder at the lens's edge. Unlike Frigate, a whole frame always replaces a cut-off one.
EDGE_MARGIN = 3  # of 1000


def _on_edge(coord):
    x, y, w, h = coord
    e = CAMERA_COORD_END - EDGE_MARGIN
    return x <= EDGE_MARGIN or y <= EDGE_MARGIN or x + w >= e or y + h >= e


def _better_thumbnail(best, d):
    new_edge, cur_edge = _on_edge(d["coord"]), _on_edge(best["coord"])
    if new_edge and not cur_edge:
        return False
    if cur_edge and not new_edge:
        return True
    area = lambda c: c[2] * c[3]  # noqa: E731
    return d["score"] > best["score"] + 0.05 or area(d["coord"]) > 1.1 * area(best["coord"])


def _note_face_read(tr, d, now_ms):
    """A face track becomes reportable ("nameable") with its first read of at least
    FACE_MATCH_MIN_PX; until then it's tracked but held back, as Protect could never name or
    group it (live 11:49 a 24 px face on Camera B showed as a face no one could identify).
    Tracking the small reads keeps the regions around the person as they were: dropping them
    nudged a person's box, and the 43 px read that followed was missed (the 14:33 clip). Its
    event box starts at that first nameable read."""
    if d["objectType"] != "face" or d.get("small") or tr.get("nameable"):
        return
    tr["nameable"] = True
    tr["first_shown_ms"] = now_ms
    tr["history"] = []


# Vehicle type and colour from Apple's on-device model (apple_fm): a vehicle track's best crop
# is sent to it in the background, up to VEHICLE_FM_MAX_CROPS times, VEHICLE_FM_GAP_MS apart,
# once at least VEHICLE_FM_MIN_PX wide and high (a car entering at the frame's edge showed
# only its roof, which the model called blue); the answers are voted (weighted by box area) and
# replace the per-frame YOLO class and HSV colour. Live 15:13 a black SUV went out as
# car/truck and blue; the model called it "suv black". Without the model (not macOS 27,
# Apple Intelligence off, no swiftc) the per-frame values stay. AIPORT_VEHICLE_FM=0 turns it off.
VEHICLE_FM = apple_fm.enabled()
VEHICLE_FM_MAX_CROPS = 3
VEHICLE_FM_GAP_MS = 1500
VEHICLE_FM_MIN_PX = 100
VEHICLE_FM_SYNC = False   # replay.py classifies inline, so results land before the leave
_fm_classifier = None
_fm_queue = None


def _fm_vote(votes):
    """{"vehicleType": {...} or None, "color": {...}} from [(type, colour, weight)]: each
    field's answer with the most weight, its share as the confidence. A "trailer" type maps
    to None (no Protect type: the key is dropped). Fields nobody answered are left out."""
    out = {}
    for key, idx in (("vehicleType", 0), ("color", 1)):
        weight = collections.Counter()
        for vote in votes:
            if vote[idx]:
                weight[vote[idx]] += vote[2]
        if not weight:
            continue
        best = max(weight, key=lambda v: weight[v])
        share = int(round(100 * weight[best] / sum(weight.values())))
        out[key] = None if best == "trailer" else {"val": best, "confidence": share}
    return out


VEHICLE_FM_AGREE = 2        # answers that must agree before a box shows a type or colour
VEHICLE_FM_HOLD_MS = 2500   # a vehicle track waits this long to close for a pending answer
VEHICLE_FM_EDGE = 5         # a crop within this of the picture's edge (0-1000) is cut off...
VEHICLE_FM_EDGE_WEIGHT = 0.25   # ...and counts this much: live 16:11 a school bus cut off at
                                # the left edge came back "van", at a bigger size than the
                                # "bus" crop after it
VEHICLE_FM_REUSE_IOU = 0.6  # a vehicle re-detected where one was classified takes its answers
VEHICLE_FM_REUSE_MS = 30 * 60 * 1000   # (live: a parked trailer was re-asked every few minutes)
_fm_cache = {}              # deviceID -> [(coord, votes, wall ms)], newest last
_fm_cache_lock = threading.Lock()


def _fm_agreed(votes):
    """The fields of _fm_vote whose answer at least VEHICLE_FM_AGREE answers give: what a box
    may show. Live 16:11 a bus's first, dark crop said "black"; the box showed it until the
    leave, though the next answer said yellow."""
    vote = _fm_vote(votes)
    out = {}
    for key, idx in (("vehicleType", 0), ("color", 1)):
        if key not in vote:
            continue
        answer = "trailer" if vote[key] is None else vote[key]["val"]
        if sum(1 for v in votes if v[idx] == answer) >= VEHICLE_FM_AGREE:
            out[key] = vote[key]
    return out


def _fm_active():
    return VEHICLE_FM and not (_fm_classifier is not None and _fm_classifier.available is False)


def _vehicle_attrs(tr, leaving):
    """A vehicle track's attributes as sent. With the model on: a box (enter/moving) carries
    only the type/colour enough answers agree on, nothing before; a leave carries the full vote,
    or the per-frame guesses when the model never answered. Without it, the per-frame guesses."""
    attrs = dict((tr.get("extra") or {}).get("attributes") or {})
    if not _fm_active():
        return attrs
    votes = tr.get("fm_votes") or []
    if leaving and not votes:
        return attrs
    fm = _fm_vote(votes) if leaving else _fm_agreed(votes)
    if not leaving:
        attrs.pop("vehicleType", None)
        attrs.pop("color", None)
    for key, value in fm.items():
        if value is None:
            attrs.pop(key, None)
        else:
            attrs[key] = value
    return attrs


def _fm_cut_off(coord):
    x, y, w, h = coord
    e = VEHICLE_FM_EDGE
    return x <= e or y <= e or x + w >= CAMERA_COORD_END - e or y + h >= CAMERA_COORD_END - e


def _fm_remember(device_id, coord, votes):
    with _fm_cache_lock:
        entries = [c for c in _fm_cache.get(device_id, []) if _iou(c[0], coord) < VEHICLE_FM_REUSE_IOU]
        entries.append((list(coord), list(votes), _frame_wall_ms(device_id)))
        _fm_cache[device_id] = entries[-20:]


def _fm_recall(device_id, coord):
    """Answers for a vehicle classified at this spot within VEHICLE_FM_REUSE_MS, or None."""
    now = _frame_wall_ms(device_id)
    with _fm_cache_lock:
        for c_coord, votes, when in reversed(_fm_cache.get(device_id, [])):
            if now - when <= VEHICLE_FM_REUSE_MS and _iou(c_coord, coord) >= VEHICLE_FM_REUSE_IOU:
                return list(votes)
    return None


def _fm_job(device_id, tr, jpeg, coord, locked=False):
    """Classify one crop of a vehicle track's best frame and record the vote. `locked`: the
    caller already holds _motion_lock (the inline replay path)."""
    try:
        result, size = _fm_classify(jpeg, coord)
    except Exception:
        result, size = None, 0
        log.exception("Apple vehicle attributes failed for deviceID=%s", device_id)

    def record():
        tr["fm_pending"] = max(0, tr.get("fm_pending", 0) - 1)
        if result is None:
            return
        vtype, color, ms = result
        weight = size * (VEHICLE_FM_EDGE_WEIGHT if _fm_cut_off(coord) else 1.0)
        tr.setdefault("fm_votes", []).append((vtype, color, weight))
        log.info("Apple vehicle attributes for deviceID=%s: %s %s (%s ms, %d px%s)", device_id,
                 vtype, color, ms, int(size ** 0.5), ", cut off" if _fm_cut_off(coord) else "")
        _fm_remember(device_id, coord, tr["fm_votes"])
    if locked:
        record()
    else:
        with _motion_lock:
            record()


def _fm_classify(jpeg, coord):
    """(model result or None, crop area in px) for one crop of a JPEG frame."""
    global _fm_classifier
    import cv2
    import numpy as np
    img = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        return None, 0
    x1, y1, x2, y2 = enrich._expand_px(coord, img.shape[1], img.shape[0], margin=1.1)
    crop = img[y1:y2, x1:x2]
    if crop.size == 0:
        return None, 0
    if _fm_classifier is None:
        _fm_classifier = apple_fm.VehicleClassifier()
    fd, path = tempfile.mkstemp(suffix=".jpg", dir=_tmp_dir())
    try:
        os.close(fd)
        os.chmod(path, 0o644)   # the helper runs as the logged-in user, not root
        cv2.imwrite(path, crop)
        return _fm_classifier.classify(path), crop.shape[0] * crop.shape[1]
    finally:
        os.remove(path)


def _fm_worker():
    while True:
        _fm_job(*_fm_queue.get())


def _request_fm(device_id, tr):
    """Queue the track's new best frame for the model, within the per-track limits; a vehicle
    where one was recently classified takes those answers instead."""
    global _fm_queue
    if not _fm_active():
        return
    best = tr.get("best") or {}
    if not best.get("jpeg") or tr.get("fm_sent", 0) >= VEHICLE_FM_MAX_CROPS:
        return
    if not tr.get("fm_votes") and not tr.get("fm_sent"):
        votes = _fm_recall(device_id, best["coord"])
        if votes:
            tr["fm_votes"], tr["fm_sent"] = votes, VEHICLE_FM_MAX_CROPS
            log.debug("Apple vehicle attributes for deviceID=%s: reused for a vehicle in the "
                      "same spot", device_id)
            return
    now = _frame_wall_ms(device_id)
    if now - tr.get("fm_last_ms", now - VEHICLE_FM_GAP_MS) < VEHICLE_FM_GAP_MS:
        return
    width, height = _stream_dims.get(device_id, (DETECT_IMG_SIZE, DETECT_IMG_SIZE))
    if min(best["coord"][2] * width, best["coord"][3] * height) / CAMERA_COORD_END < VEHICLE_FM_MIN_PX:
        return
    tr["fm_sent"] = tr.get("fm_sent", 0) + 1
    tr["fm_last_ms"] = now
    tr["fm_pending"] = tr.get("fm_pending", 0) + 1
    job = (device_id, tr, best["jpeg"], list(best["coord"]))
    if VEHICLE_FM_SYNC:
        _fm_job(*job, locked=True)
        return
    if _fm_queue is None:
        _fm_queue = queue.Queue(maxsize=20)
        threading.Thread(target=_fm_worker, name="apple-vehicle-attrs", daemon=True).start()
    try:
        _fm_queue.put_nowait(job)
    except queue.Full:
        tr["fm_pending"] -= 1
        log.debug("Apple vehicle attributes: queue full, skipping a crop for deviceID=%s",
                  device_id)


def _fm_hold(tr, now_ms):
    """True while a vehicle track that would close waits for a pending model answer, up to
    VEHICLE_FM_HOLD_MS: live 16:11 a bus's "bus yellow" arrived 2 s after its leave."""
    if not tr.get("fm_pending"):
        return False
    return now_ms - tr.setdefault("fm_hold_ms", now_ms) < VEHICLE_FM_HOLD_MS


def _capture_best(device_id, tr, d):
    """Keep the track's best-scoring moment: the full-res JPEG, the box and the
    time. On leave the thumbnails come from it, not from the frame several seconds after
    the object was last seen (which, for a face, no longer contains it)."""
    best = tr.get("best")
    if (d.get("seeded") or d.get("reused") or d.get("small")
            or (best and not _better_thumbnail(best, d))):
        return
    if best and not d.get("frame") and device_id in _frame_jpegs:
        # The pull skipped this frame's JPEG (FRAME_JPEG_EVERY): the latest snapshot is
        # another moment. replay.py has no frame JPEGs, so there the snapshot still counts.
        return
    mtime, data = d.get("frame") or _read_snapshot(device_id)
    if data is None:
        return
    tr["best"] = {"jpeg": data, "coord": list(d["coord"]), "score": d["score"],
                  "wall": _frame_wall_ms(device_id)}
    if VEHICLE_FM and tr.get("objectType") == "vehicle":
        _request_fm(device_id, tr)


# A plate can't change on a vehicle that isn't moving, and a confident read won't improve.
# As Frigate, a parked vehicle is still read for PLATE_PARKED_SCAN_MS after it parks (a car
# that has just pulled in often shows its plate only once it stops).
PLATE_CONFIDENT = 0.85
PLATE_PARKED_SCAN_MS = 5000
SETTLED_MATCH_IOU = 0.5


def _parked_scan_over(tr):
    """True once a parked track's plate-reading window has passed (or its start is unknown)."""
    parked, seen = tr.get("parked_at_ms"), tr.get("seen_ms")
    return parked is None or seen is None or seen - parked > PLATE_PARKED_SCAN_MS


def _plate_settled(device_id, coord):
    """True if this vehicle box belongs to a track that is parked, still, or already has a
    confident plate -- re-reading it is wasted work. A partial box mostly inside a parked
    car's box is that car (live 10:14 on Camera B: a parked car's plate was re-read and logged
    on every snapshot from boxes on part of it)."""
    with _motion_lock:
        by_id = dict(_motion_state.get(device_id, {}).get("tracks", {}))
    inside = _inside_parked(by_id, {"objectType": "vehicle", "coord": coord})
    if inside is not None and _parked_scan_over(by_id[inside]):
        return True
    tracks = list(by_id.values())
    for tr in tracks:
        if tr["objectType"] != "vehicle" or _iou(tr["coord"], coord) < SETTLED_MATCH_IOU:
            continue
        if ((tr.get("stationary_coord") and _parked_scan_over(tr)) or tr.get("still")
                or tr.get("extra_scores", {}).get("name", 0) >= PLATE_CONFIDENT):
            return True
    return False


def _enrich_detections(device_id, detections):
    """Face detections (plus plate text on vehicles) from this tick's boxes.
    Protect reads a vehicle's plate from its descriptor `name` and recognises a
    face from its `faceEmbed`. Results are reused until a newer snapshot lands, so
    tracks don't flap; plate/name reach the parent only on a fresh read (tracks keep them)."""
    vehicles = [d for d in detections if d["objectType"] == "vehicle"
                and not _plate_settled(device_id, d["coord"])]
    people = [d for d in detections if d["objectType"] == "person"]
    if not vehicles and not people:
        return []
    enricher = _get_enricher()
    if not (enricher.lpr and vehicles) and not (enricher.face and people):
        return []
    img, mtime = _load_snapshot_bgr(device_id)
    if img is None:
        return []
    cached = _enrich_cache.get(device_id)
    fresh = not (cached and cached[0] == mtime)
    if not fresh:
        extra = cached[1]
    else:
        extra = []
        try:
            for p in enricher.plates(img, [v["coord"] for v in vehicles]):
                vehicles[p["parent"]].setdefault("scored", {})["name"] = (p["text"], p["confidence"])
                log.debug("enrichment: plate %r on vehicle for deviceID=%s (score=%.2f)",
                         p["text"], device_id, p["confidence"])
            for f in enricher.faces(img, [p["coord"] for p in people]):
                face = {"objectType": "face", "score": f["score"], "coord": f["coord"],
                        "extra": {"name": f["name"], "tag": f["name"]},
                        "frame": _snapshot_bytes.get(device_id), "size_px": f.get("size_px"),
                        "fresh_read": True, "parent_coord": list(people[f["parent"]]["coord"])}
                # A face under FACE_MATCH_MIN_PX is too small to match: it gets no embedding
                # and no match, and is tracked but not reported until a read of its track
                # reaches the minimum (_note_face_read).
                small = f.get("size_px", enrich.FACE_MATCH_MIN_PX) < enrich.FACE_MATCH_MIN_PX
                if small:
                    face["small"] = True
                # A face whose SFace vector is short (enrich.FACE_EMBED_MIN_NORM) is reported
                # without its embedding and isn't matched: a poor face. Protect enrols the
                # embedding it's sent, and a poor one makes a bad reference.
                # Only a read of FACE_EMBED_MIN_PX or more sends its embedding: unnamed, it goes
                # to Protect's own face search, which put 43-48 px reads on the wrong person
                # (live 14:39 and 18:41, one person as another and back). Smaller ones are still
                # matched here and reported.
                if f["embedding"] is not None and not small and not enrich.weak_embedding(f["embedding"]):
                    face["scored"] = {}
                    if f.get("size_px", FACE_EMBED_MIN_PX) >= FACE_EMBED_MIN_PX:
                        face["scored"]["faceEmbed"] = (f["embedding"], f["score"])
                    db = _get_face_db()
                    m = db.match(f["embedding"], FACE_DB_THRESH) if db else None
                    if m:
                        subject_id, name, sim = m
                        face["scored"]["match"] = ((subject_id, name), sim)
                        people[f["parent"]].setdefault("scored", {})["match"] = (
                            (subject_id, name), sim)
                        log.info("face DB: face on deviceID=%s matches %r (subject %s, "
                                 "similarity %.3f)", device_id, name, subject_id, sim)
                extra.append(face)
                if f["name"]:
                    people[f["parent"]].setdefault("extra", {}).update(
                        {"name": f["name"], "tag": f["name"]})
        except Exception:
            log.exception("plate/face enrichment failed for deviceID=%s", device_id)
        _enrich_cache[device_id] = (mtime, extra)
        for e in extra:
            log.info("enrichment: %s for deviceID=%s (score=%.2f, name=%r, embedding=%s, %s px)",
                     e["objectType"], device_id, e["score"], e["extra"]["name"],
                     "faceEmbed" in e.get("scored", {}), e.get("size_px"))
    # A face DB match counts once per snapshot read, not on every tick that reuses it.
    return [dict(e, extra=dict(e["extra"]), fresh_read=fresh and e.get("fresh_read", False),
                 **({"scored": {k: v for k, v in e["scored"].items() if fresh or k != "match"}}
                    if "scored" in e else {}))
            for e in extra]


def _size_jump(a, b):
    """True if box b ([x, y, w, h]) is over NN_SIZE_RATIO_MAX x wider or taller than a, or
    under 1/NN_SIZE_RATIO_MAX x: a different object, not this one moving. Not when the
    smaller box lies mostly (SIZE_JUMP_INSIDE) inside the larger: the same object seen whole
    or in part (live 11:33 a person up close to Camera A went from a 79- to a 226-wide box
    holding the old one, and became a new person track)."""
    small, large = sorted((a, b), key=lambda c: c[2] * c[3])
    iw = min(small[0] + small[2], large[0] + large[2]) - max(small[0], large[0])
    ih = min(small[1] + small[3], large[1] + large[3]) - max(small[1], large[1])
    if iw > 0 and ih > 0 and iw * ih >= SIZE_JUMP_INSIDE * max(small[2] * small[3], 1e-6):
        return False
    for i in (2, 3):
        lo, hi = sorted((max(a[i], 1e-6), max(b[i], 1e-6)))
        if hi / lo > NN_SIZE_RATIO_MAX:
            return True
    return False


def _size_ref(tr):
    """The box a detection's size is compared with: the track's last box resized to the median
    width and height of its recent boxes (its last box until it has 3)."""
    sizes = tr.get("sizes") or []
    if len(sizes) < 3:
        return tr["coord"]
    w = statistics.median(s[0] for s in sizes)
    h = statistics.median(s[1] for s in sizes)
    x, y, lw, lh = tr["coord"]
    return [x + lw / 2 - w / 2, y + lh / 2 - h / 2, w, h]


def _nearest_detection(tr, detections, unclaimed):
    """Index of the nearest same-type unclaimed detection to where tr's motion predicts it,
    within NN_DIST_FACTOR x its size (+ margin), at most NN_DIST_MAX, widening a little
    while it's missed; None if none."""
    cx, cy = _zone_center_norm(tr["coord"])
    vx, vy = tr.get("velocity", (0.0, 0.0))
    steps = 1 + tr["miss_count"]
    px, py = cx + vx * steps, cy + vy * steps
    limit = (min(NN_DIST_FACTOR * max(tr["coord"][2], tr["coord"][3]) + NN_DIST_MARGIN, NN_DIST_MAX)
             * min(1 + NN_MISS_GROWTH * tr["miss_count"], NN_MISS_GROWTH_MAX))
    best, best_dist = None, limit
    for idx in unclaimed:
        d = detections[idx]
        if d["objectType"] != tr["objectType"] or _size_jump(_size_ref(tr), d["coord"]):
            continue
        dx, dy = _zone_center_norm(d["coord"])
        dist = ((dx - px) ** 2 + (dy - py) ** 2) ** 0.5
        if dist <= best_dist:
            best, best_dist = idx, dist
    return best


def _follow(tr, coord):
    """Move a track to a new raw box: velocity per tick and the smoothed reporting box."""
    (ox, oy), (nx, ny) = _zone_center_norm(tr["coord"]), _zone_center_norm(coord)
    steps = 1 + tr.get("miss_count", 0)
    tr["velocity"] = ((nx - ox) / steps, (ny - oy) / steps)
    prev = tr.get("smooth") or tr["coord"]
    tr["smooth"] = [BOX_SMOOTHING * c + (1 - BOX_SMOOTHING) * p for c, p in zip(coord, prev)]
    tr["coord"] = coord
    sizes = tr.setdefault("sizes", [])
    sizes.append((coord[2], coord[3]))
    del sizes[:-SIZE_HISTORY]


def _jaro_winkler(a, b):
    """Jaro-Winkler similarity of two strings, 0-1 (as rapidfuzz's, which Frigate uses)."""
    if a == b:
        return 1.0
    if not a or not b:
        return 0.0
    reach = max(0, max(len(a), len(b)) // 2 - 1)
    a_hit, b_hit = [False] * len(a), [False] * len(b)
    matches = 0
    for i, ch in enumerate(a):
        for j in range(max(0, i - reach), min(len(b), i + reach + 1)):
            if not b_hit[j] and b[j] == ch:
                a_hit[i] = b_hit[j] = True
                matches += 1
                break
    if not matches:
        return 0.0
    a_m = [ch for ch, hit in zip(a, a_hit) if hit]
    b_m = [ch for ch, hit in zip(b, b_hit) if hit]
    half_transpositions = sum(x != y for x, y in zip(a_m, b_m))
    jaro = (matches / len(a) + matches / len(b) + (matches - half_transpositions / 2) / matches) / 3
    prefix = 0
    for x, y in zip(a[:4], b[:4]):
        if x != y:
            break
        prefix += 1
    return jaro + prefix * 0.1 * (1 - jaro)


# Plate reads: as Frigate, a track keeps its recent reads, groups those whose average
# similarity is >= PLATE_CLUSTER_SIMILARITY and takes the largest group (ties: the group with
# the most confident read). Within it, the reads of its most common length vote character by
# character (each read weighted by its confidence), so one misread letter ('ABED123' among
# 'ABCD123's) is outvoted. The plate is final, and reported, only once PLATE_AGREE_READS of
# those reads agree: live 14:38 on Camera B a single confident misread (DERM782 at 0.90 for
# DEPM782) was taken as the plate and the car was never read again.
PLATE_READS_KEEP = 25
PLATE_CLUSTER_SIMILARITY = 0.85
PLATE_AGREE_READS = int(os.environ.get("AIPORT_LPR_AGREE_READS") or 3)


def _plate_rep(reads):
    """(text, confidence, agreeing) for a track's [(text, confidence), ...] plate reads:
    the per-character vote of its main group, the mean confidence of the reads that voted,
    and how many reads voted (PLATE_AGREE_READS of them make the plate final)."""
    clusters = []
    for read in reads:
        for cluster in clusters:
            if sum(_jaro_winkler(read[0], r[0]) for r in cluster) / len(cluster) >= PLATE_CLUSTER_SIMILARITY:
                cluster.append(read)
                break
        else:
            clusters.append([read])
    best = max(clusters, key=lambda c: (len(c), max(r[1] for r in c)))
    lengths = collections.Counter(len(r[0]) for r in best)
    length = max(lengths, key=lambda n: (lengths[n], max(r[1] for r in best if len(r[0]) == n)))
    voters = [r for r in best if len(r[0]) == length]
    text = ""
    for i in range(length):
        weight = collections.Counter()
        for t, conf in voters:
            weight[t[i]] += conf
        text += max(weight, key=lambda ch: weight[ch])
    return text, sum(r[1] for r in voters) / len(voters), len(voters)


def _hold_for_zones(device_id, tracks, joining):
    """joining without the tracks that haven't been in a zone yet; those wait (awaiting_zone)
    and join on the tick they first are."""
    out, seen = [], set()
    for tid, tr in tracks.items():
        if tr.get("awaiting_zone") and tr.get("zoned") and tr.get("confirmed", True):
            tr.pop("awaiting_zone")
            log.info("real motion tick: %s tracker_id=%s %s, joining for deviceID=%s",
                     tr["objectType"], tid,
                     "left the frame's corner" if tr.pop("left_corner", False)
                     else "entered a zone", device_id)
            out.append((tid, tr))
            seen.add(tid)
    for tid, tr in joining:
        if tid in seen:
            continue
        if tr.get("zoned", True):
            out.append((tid, tr))
            seen.add(tid)
        elif not tr.get("awaiting_zone"):
            tr["awaiting_zone"] = True
            if tr.get("corner_hold"):
                log.info("real motion tick: %s tracker_id=%s only in the frame's %s corner "
                         "(AIPORT_LENS_FILTER); tracked, joining once it leaves it, for "
                         "deviceID=%s", tr["objectType"], tid, tr["corner_hold"], device_id)
            else:
                log.info("real motion tick: %s tracker_id=%s outside the zones for its type; "
                         "tracked, joining once it enters one, for deviceID=%s",
                         tr["objectType"], tid, device_id)
    return out


def _merge_extra(tr, d, device_id=None, tracker_id=None):
    """Carry enrichment fields onto a track. A vehicle's plate (`name`) is the
    representative of its recent reads (_plate_rep), logged when it first appears or
    changes; other scored keys (a face's `faceEmbed`) keep the best-scoring read; other
    keys (attributes, names) take the newest value and are never cleared by a
    tick that lacks them."""
    if d.get("zone_ids"):
        tr["zone_ids"] = list(d["zone_ids"])   # the zones it's in now; kept through gaps
        seen = tr.setdefault("zones_seen", [])
        seen.extend(z for z in d["zone_ids"] if z not in seen)
    held = device_id is not None and _corner_held(device_id, tr, d)
    if d.get("in_zone", True) and not held:
        tr["zoned"] = True        # latched: it has been in a zone listing its type
    else:
        tr.setdefault("zoned", False)
    out = tr.setdefault("extra", {})
    out.update({k: v for k, v in (d.get("extra") or {}).items() if v not in ("", None)})
    if d.get("fresh_read") and "faceEmbed" in (d.get("scored") or {}):
        tr["embed_reads"] = tr.get("embed_reads", 0) + 1
    scores = tr.setdefault("extra_scores", {})
    for key, (value, score) in (d.get("scored") or {}).items():
        if key == "name" and tr.get("objectType") == "vehicle":
            reads = tr.setdefault("plate_reads", [])
            reads.append((value, score))
            del reads[:-PLATE_READS_KEEP]
            text, conf, agreeing = _plate_rep(reads)
            if agreeing < PLATE_AGREE_READS:
                log.debug("plate %r for vehicle tracker_id=%s deviceID=%s not final yet (%d of "
                          "%d agreeing reads)", text, tracker_id, device_id, agreeing,
                          PLATE_AGREE_READS)
                continue
            if text != tr.get("plate_logged"):
                log.info("plate %r for vehicle tracker_id=%s deviceID=%s (score=%.2f, %d of %d "
                         "read(s) agreeing)", text, tracker_id, device_id, conf, agreeing,
                         len(reads))
                tr["plate_logged"] = text
            out[key], scores[key] = text, conf
        elif key == "match":  # (subject_id, name) from Protect's face DB
            hits = tr.setdefault("match_hits", collections.Counter())
            hits[value] += 1
            best = max(hits, key=lambda k: hits[k])
            if hits[best] >= FACE_MATCH_HITS and out.get("matchedId") != best[0]:
                out["matchedId"], out["matchedName"] = best
                log.info("face DB: %s tracker_id=%s deviceID=%s is %r (subject %s, %d "
                         "matching read(s))", tr.get("objectType"), tracker_id, device_id,
                         best[1], best[0], hits[best])
        elif score >= scores.get(key, 0):
            out[key] = value
            scores[key] = score


def _iou(box_a, box_b):
    """Intersection-over-union of two [x, y, w, h] boxes."""
    ax1, ay1, aw, ah = box_a
    bx1, by1, bw, bh = box_b
    ax2, ay2 = ax1 + aw, ay1 + ah
    bx2, by2 = bx1 + bw, by1 + bh
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    iw, ih = max(0.0, ix2 - ix1), max(0.0, iy2 - iy1)
    inter = iw * ih
    union = aw * ah + bw * bh - inter
    return inter / union if union > 0 else 0.0


def _parse_zone_ids(zones_field):
    """Each zone's id (Protect's key for it, an int when numeric), in _parse_zones' order.
    Protect labels an event with the ids its descriptors name (zone:<camera>:<id>)."""
    out = []
    for key, zone in (zones_field or {}).items():
        coord = zone.get("coord") or []
        if len(coord) >= 6 and len(coord) % 2 == 0:
            out.append(int(key) if str(key).isdigit() else key)
    return out


def _parse_zone_types(zones_field):
    """Each zone's objectTypes (None when it doesn't say), in _parse_zones' order."""
    out = []
    for zone in (zones_field or {}).values():
        coord = zone.get("coord") or []
        if len(coord) >= 6 and len(coord) % 2 == 0:
            types = zone.get("objectTypes")
            out.append(list(types) if isinstance(types, list) else None)
    return out


def _parse_zone_map(zones_field):
    """Parse zones/excludeZones into {zone_id: polygon}, keeping the controller's id."""
    out = {}
    for zone_id, zone in (zones_field or {}).items():
        if not isinstance(zone, dict):
            continue
        coord = zone.get("coord") or []
        if len(coord) >= 6 and len(coord) % 2 == 0:
            out[str(zone_id)] = list(zip(coord[0::2], coord[1::2]))
    return out


def _parse_zones(zones_field):
    """Parse ChangeSmartDetectSettings zones/excludeZones into polygons
    (even-length coord, >= 3 points)."""
    return list(_parse_zone_map(zones_field).values())


def _parse_lines(lines_field):
    """Parse ChangeSmartDetectSettings.lines into 2-point segments (normalized
    0-1000; a `points` fallback is accepted)."""
    out = []
    for line_id, line in (lines_field or {}).items():
        if not isinstance(line, dict):
            continue
        pts = []
        coord = line.get("coord") or []
        if len(coord) >= 4 and len(coord) % 2 == 0:
            pts = list(zip(coord[0::2], coord[1::2]))
        else:
            for pt in (line.get("points") or []):
                if isinstance(pt, (list, tuple)) and len(pt) >= 2:
                    pts.append((pt[0], pt[1]))
        if len(pts) < 2:
            continue
        out.append({
            "id": str(line_id),
            "p1": pts[0],
            "p2": pts[-1],
            "direction": line.get("direction") or line.get("crosslineDirection"),
            "object_types": line.get("objectTypes") or line.get("object_types"),
        })
    return out


def _line_ref(line_id):
    """Return a numeric line id when numeric, else the string (controller keys
    numeric ids as numbers)."""
    s = str(line_id)
    return int(s) if s.isdigit() else s


def _line_side(p1, p2, pt):
    """Sign of the cross product: which side of directed line p1->p2 `pt` is on."""
    return (p2[0] - p1[0]) * (pt[1] - p1[1]) - (p2[1] - p1[1]) * (pt[0] - p1[0])


def _segment_crosses_line(prev, cur, p1, p2):
    """True if segment prev->cur crosses segment p1->p2."""
    d1 = _line_side(p1, p2, prev)
    d2 = _line_side(p1, p2, cur)
    d3 = _line_side(prev, cur, p1)
    d4 = _line_side(prev, cur, p2)
    return ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0))


def _process_line_crossings(ws, device_id, tracker_id, tr, new_center):
    """Emit a smartDetectLine event for each configured line this movement
    crosses (first crossing = enter, later = moving)."""
    lines = _lines.get(device_id)
    prev_center = tr.get("prev_center")
    if not lines or prev_center is None:
        return
    for line in lines:
        types = line.get("object_types")
        if types and tr["objectType"] not in types:
            continue
        if not _segment_crosses_line(prev_center, new_center, line["p1"], line["p2"]):
            continue
        direction = "A2B" if _line_side(line["p1"], line["p2"], prev_center) < 0 else "B2A"
        entry = tr.setdefault("crossed_lines", {}).setdefault(line["id"], {
            "a2b": 0, "b2a": 0, "direction": direction, "event_id": None,
        })
        entry["a2b" if direction == "A2B" else "b2a"] += 1
        entry["direction"] = direction
        edge_type = "moving" if entry["event_id"] is not None else "enter"
        if entry["event_id"] is None:
            entry["event_id"] = next(_event_id_counter)
        log.info("line crossing: %s crossed line %s (%s, a2b=%d b2a=%d) for deviceID=%s "
                  "tracker_id=%s -- sending %s", tr["objectType"], line["id"], direction,
                  entry["a2b"], entry["b2a"], device_id, tracker_id, edge_type)
        try:
            _send_line_detect_event(ws, device_id, line, edge_type, entry["event_id"],
                                    direction, entry["a2b"], entry["b2a"],
                                    tr["objectType"], tr["coord"], tr["score"],
                                    tr["first_shown_ms"], tracker_id)
        except Exception:
            log.exception("failed to send line %s event for tracker_id=%s deviceID=%s",
                          line["id"], tracker_id, device_id)


def _dedupe_detections(detections, iou_thresh=0.4):
    """Collapse same-type detections that heavily overlap into one (highest
    score wins). Per-class NMS in detectors/postprocess.py uses NMS_THRESH 0.45, but two
    boxes for ONE object can still survive it (e.g. a full-body box plus a
    partial/torso box with IoU just under the gate) -- observed live as two
    `person detected` ticks 1ms apart at scores 0.67/0.51 for the same person.
    Left alone, each unmatched detection opens its own track, so one object
    produces two tracks, two event streams, and no clean matched pair -- which
    is fatal for line crossing (it needs a matched pair to get a movement
    segment)."""
    out = []
    for d in sorted(detections, key=lambda d: -d["score"]):
        if any(d["objectType"] == k["objectType"] and _iou(d["coord"], k["coord"]) >= iou_thresh
               for k in out):
            continue
        out.append(d)
    return out


_scene_ref = {}  # deviceID -> (downsampled grayscale reference frame, monotonic ms taken)
_motion_detectors = {}  # deviceID -> motion.MotionDetector (rebuilt when settings change)
_motion_settings = {}   # deviceID -> {"level", "zones"} from ChangeSmartMotionSettings
_motion_boxes = {}      # deviceID -> this frame's motion boxes (detector-frame pixels)


def _canvas_to_image(device_id, x, y):
    """Protect canvas point (0-1000) -> image-normalised point (0-1000), clamped."""
    sx, sy = _canvas_scale(device_id)
    half = CAMERA_COORD_END / 2
    clamp = lambda v: min(CAMERA_COORD_END, max(0.0, v))  # noqa: E731
    return clamp(half + (x - half) / sx), clamp(half + (y - half) / sy)


def _motion_contour_area(device_id):
    """AIPORT_MOTION_CONTOUR_AREA ("<default>[,<deviceID>=<area>...]"): the smallest motion
    blob, in pixels of the 100-row motion frame (Frigate's motion.contour_area; 10 by
    default, as in Frigate)."""
    import motion
    default, per = _parse_box_delay(os.environ.get("AIPORT_MOTION_CONTOUR_AREA"),
                                    "AIPORT_MOTION_CONTOUR_AREA", motion.DEFAULT_CONTOUR_AREA)
    return per.get(str(device_id).upper(), default)


def _motion_detector(device_id, shape):
    det = _motion_detectors.get(device_id)
    if det is None or det.frame_shape != tuple(shape[:2]):
        import motion
        settings = _motion_settings.get(device_id) or {}
        override = os.environ.get("AIPORT_MOTION_THRESHOLD")
        threshold = (int(override) if override and override.isdigit()
                     else motion.threshold_for_level(settings.get("level", 50)))
        mask = motion.zones_mask(settings.get("zones"), shape,
                                 lambda x, y: _canvas_to_image(device_id, x, y))
        contour_area = _motion_contour_area(device_id)
        det = _motion_detectors[device_id] = motion.MotionDetector(
            shape[:2], threshold=threshold, contour_area=contour_area, mask=mask)
        log.info("motion detector for deviceID=%s: threshold %d, contour area %d, %s", device_id,
                 threshold, contour_area,
                 "masked to Protect's motion zones" if mask is not None else "whole frame")
    return det


def _scene_changed(device_id):
    """True if this frame has motion boxes (Frigate's motion detector, which must see
    every frame to keep its background current). While it calibrates -- at start, or after
    lightning or IR switching changed most of the picture -- nothing counts as motion."""
    with _frame_lock:
        frame = _latest_frame.get(device_id)
    if frame is None:
        return False
    try:
        import cv2
    except ImportError:
        return _scene_changed_mean(device_id, frame)
    det = _motion_detector(device_id, frame.shape)
    boxes = det.detect(cv2.cvtColor(frame, cv2.COLOR_RGB2GRAY))
    _motion_boxes[device_id] = boxes
    return bool(boxes) and not det.is_calibrating()


def _scene_changed_mean(device_id, frame):
    """Fallback without OpenCV: true if this frame differs enough from the reference. The reference
    advances at most once per report interval: frames analysed faster than that are only
    a little apart, and slow movement would otherwise stay under the gate. A static scene
    still stays below it."""
    import numpy as np  # lazy, like the detector path: avclient imports without numpy
    gray = frame[::_GATE_STRIDE, ::_GATE_STRIDE].mean(axis=2)
    now = int(time.monotonic() * 1000)
    ref, ref_ms = _scene_ref.get(device_id) or (None, None)
    if ref is None or now - ref_ms >= REPORT_INTERVAL_MS:
        _scene_ref[device_id] = (gray, now)
    if ref is None or ref.shape != gray.shape:
        return True
    return float(np.abs(gray - ref).mean()) / 255.0 >= MOTION_GATE_THRESH


# Protect keeps ONE smart-detect event per camera (not per track): the first leave
# closes it, and its scheduled check force-ends one open > ~5.5 min with no snapshots.
# So tracks share a camera-wide event: enter when the first track appears, a batched
# moving per tick, and one leave (every participant's snapshot) when the last active
# track goes or eventMaxDurationMSec (sent in ChangeSmartMotionSettings) runs out.
DEFAULT_EVENT_MAX_MS = 300000
_event_max_ms = {}  # deviceID -> eventMaxDurationMSec from ChangeSmartMotionSettings
# A new track joins the camera event only once it has been seen for this long, so a
# detection that lasts a frame or two never opens a dashboard event. Protect sends it as
# eventStartMSec in ChangeSmartDetectSettings; AIPORT_CONFIRM_MS overrides it. 0 = at once.
_event_start_ms = {}  # deviceID -> eventStartMSec from ChangeSmartDetectSettings


def _confirm_ms(device_id):
    override = os.environ.get("AIPORT_CONFIRM_MS")
    if override:
        try:
            return max(0, int(override))
        except ValueError:
            log.warning("ignoring bad AIPORT_CONFIRM_MS %r", override)
    return _event_start_ms.get(device_id, 0)
# A track still present when its event hit the time limit goes quiet (stationary, e.g. a
# parked car) until it really moves: its box mostly leaves the parked spot, or its centre
# stays shifted (0-1000 image units) for several ticks at about the same size. Someone
# walking past shrinks the box for a moment without moving it off its spot.
STATIONARY_LEFT_IOU = 0.3
STATIONARY_RESUME_DISTANCE = 50
STATIONARY_RESUME_TICKS = _ticks(6)
STATIONARY_SIZE_RATIO = (0.7, 1.4)
# A track whose box keeps overlapping its anchor (IoU >= STILL_IOU) for STILL_TICKS ticks
# (~30 s) is still: its descriptors say stationary, and a still vehicle is parked (its
# snapshot stays in the event's leave). Objects found within STARTUP_WINDOW_MS of a camera's
# tracker or stream starting (cars already parked there) are parked at once instead of opening
# an event; a stream restart starts the window again.
STILL_IOU = 0.7
STILL_TICKS = _ticks(30)
STARTUP_WINDOW_MS = 15000
PARK_WHEN_STILL = ("vehicle",)


def _center_moved(a, b, frac=RESUME_MOVE_FRACTION):
    """True if box b's centre is more than `frac` of box a's size from a's centre."""
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    return (abs((bx + bw / 2) - (ax + aw / 2)) > frac * aw
            or abs((by + bh / 2) - (ay + ah / 2)) > frac * ah)


def _inside_parked(tracks, d, frac=INSIDE_PARKED_FRACTION):
    """tracker_id of a parked same-type track whose box holds >= frac of d's box, or None:
    a second box on part of a parked car (Camera A saw one on its lower half) is the
    same car, not a new vehicle."""
    x, y, w, h = d["coord"]
    area = max(1.0, w * h)
    for tid, tr in tracks.items():
        if tr["objectType"] != d["objectType"] or not tr.get("stationary_coord"):
            continue
        px, py, pw, ph = tr["stationary_coord"]
        iw = min(x + w, px + pw) - max(x, px)
        ih = min(y + h, py + ph) - max(y, py)
        if iw > 0 and ih > 0 and iw * ih / area >= frac:
            return tid
    return None


def _resume_recent(dev, d, event):
    """(tracker_id, track, how) of a recently closed track detection d continues, taken out
    of dev["recent"]; how is "event" (its event is still open) or "parked" (a vehicle that
    never moved, or was parked). None if d is a new object."""
    found, best_iou = None, RESUME_IOU
    for i, (tid, tr, _closed_ms) in enumerate(dev.get("recent", ())):
        if tr["objectType"] != d["objectType"]:
            continue
        iou = max(_iou(tr["coord"], d["coord"]), _iou(tr.get("first_coord") or tr["coord"], d["coord"]))
        if iou < best_iou:
            continue
        if tr.get("stationary_coord"):
            how = "parked"  # stays in its event's finished list, if it is in one
        elif event is not None and tr.get("event_id") == event["id"]:
            how = "event"
        elif tr["objectType"] in PARK_WHEN_STILL and not tr.get("moved"):
            how = "parked"
        else:
            continue
        found, best_iou = (i, tid, tr, how), iou
    if found is None:
        return None
    i, tid, tr, how = found
    del dev["recent"][i]
    return tid, tr, how


def _update_still(tr, now_ms):
    """Advance a track's stillness against its anchor box (detector jitter tolerated)."""
    anchor = tr.get("anchor")
    if anchor is None or _iou(tr["coord"], anchor) < STILL_IOU:
        tr["anchor"] = list(tr["coord"])
        tr["still_ticks"] = 0
        tr["still_since_ms"] = now_ms
    else:
        tr["still_ticks"] = tr.get("still_ticks", 0) + 1
    tr["still"] = tr["still_ticks"] >= STILL_TICKS


def _park(tr):
    tr["stationary_coord"] = list(tr["coord"])
    tr["parked_at_ms"] = tr.get("seen_ms")
    tr["moved_ticks"] = 0


# A face that has matched someone once keeps its embedding back until it's named, or for this
# many fresh reads: live 11:13 on Camera C the second read was the first match, the embedding
# went out unnamed a second before the name, and Protect showed one unknown face.
FACE_EMBED_HOLD_READS = 4


def _face_decided(tr):
    """True once a face track is named; or, with no match yet, after FACE_MATCH_HITS fresh
    embedding reads; or, with a match that hasn't named it yet, after FACE_EMBED_HOLD_READS."""
    extra = tr.get("extra") or {}
    if extra.get("matchedId") is not None:
        return True
    reads = tr.get("embed_reads", 0)
    return reads >= (FACE_EMBED_HOLD_READS if tr.get("match_hits") else FACE_MATCH_HITS)


def _track_args(tracker_id, tr, hold_embed=True):
    """Per-track arguments for _send_smart_detect_event. A face's embedding is held back until
    the face is decided (live 09:43: Protect searched the first, unmatched embedding of a face
    pi-port named a second later, and tagged the event with an unknown face group too); a
    leave always carries it."""
    still = bool(tr.get("still"))
    extra = tr.get("extra")
    if tr["objectType"] == "face" and extra and "faceEmbed" in extra and (
            (hold_embed and not _face_decided(tr))
            or (not hold_embed and extra.get("matchedId") is not None)):
        # held until decided; and a named face's leave carries its match, not its embedding
        extra = {k: v for k, v in extra.items() if k != "faceEmbed"}
    if tr["objectType"] == "vehicle" and extra is not None:
        extra = dict(extra, attributes=_vehicle_attrs(tr, leaving=not hold_embed))
    return {"tracker_id": tracker_id, "object_type": tr["objectType"],
            "coord": tr.get("smooth") or tr["coord"],
            "confidence": tr["score"], "first_shown_ms": tr["first_shown_ms"],
            "descriptor_extra": extra, "best": tr.get("best"), "zones": tr.get("zone_ids"),
            "zones_seen": _zones_visited(tr), "still": still, "idle_since_ms": tr.get("still_since_ms") if still else None}


# Playback places boxes by clockWall, and a track only shows once it confirms (Protect's
# eventStartMSec, 1 s): live 17:20 on Camera B a car crossed the road in about a second, so
# its box appeared as it left. A new event's enter is sent at its tracks' first frame, then a
# moving update per report interval up to now, each describing its own frame.
BACKFILL_MAX = 50


def _send_backfill(ws, device_id, event_id, active, joining):
    """Send a new event's enter and moving updates for the frames its joining tracks were
    seen in before they confirmed, back-dated to each frame. True if the enter was sent."""
    ids = {tid for tid, _ in joining}
    history = {tid: tr.pop("history", None) or [] for tid, tr in active if tid in ids}
    current = _frame_time(device_id)
    stamps = sorted({h[0] for hs in history.values() for h in hs if h[0][1] < current[1]},
                    key=lambda stamp: stamp[1])
    edge, last = "enter", None
    for stamp in stamps:
        if last is not None and stamp[1] - last < REPORT_INTERVAL_MS:
            continue
        members = []
        for tid, tr in active:
            seen = [h for h in history.get(tid, ()) if h[0][1] <= stamp[1]]
            if seen:
                _, coord, score = seen[-1]
                members.append((tid, dict(tr, coord=coord, smooth=None, score=score)))
        if not members:
            continue
        with _at_frame(device_id, stamp):
            _send_event_message(ws, device_id, edge, event_id, members)
        edge, last = "moving", stamp[1]
    return edge == "moving"


def _send_event_message(ws, device_id, edge_type, event_id, members, stationary=False):
    """One EventSmartDetect for a camera event carrying every member track
    (faces last, so a leave's Full-FoV comes from a person/vehicle best frame)."""
    if not members:
        return
    members = sorted(members, key=lambda m: m[1]["objectType"] == "face")
    args = [_track_args(tid, tr, hold_embed=edge_type != "leave") for tid, tr in members]
    try:
        _send_smart_detect_event(ws, device_id, edge_type=edge_type, event_id=event_id,
                                  stationary=stationary, also_leaving=args[1:], **args[0])
    except Exception:
        log.exception("failed to send %s for tracker_ids=%s deviceID=%s", edge_type,
                      [tid for tid, _ in members], device_id)


# Protect fetched snapshots for single-tracker leaves but, seen live, requested nothing
# for a leave carrying two trackers (and logged nothing). So an event closes with one
# tracker: the most useful participant's best frame.
LEAVE_PRIORITY = {"face": 0, "person": 1, "vehicle": 2, "animal": 3}


# A multi-participant event closes the way Protect's backend reads it (onObjectsLeft,
# getAssociatedFaceTrackerIds, publishFace, fillAndCorrectTrackInfo): empty descriptors,
# a snapshot per participant, and per-tracker attributes in trackerIDAttrMap (faceEmbed,
# associatedFaceTrackerID, vehicleType/color, plate as matchedName). If Protect requests
# none of its snapshots within LEAVE_FALLBACK_S, the proven single-tracker leave is resent.
LEAVE_FALLBACK_S = 4.0
_pending_leaves = {}  # snapshot filename -> shared {"seen": bool} for a multi-tracker leave


def _center_inside(inner, outer):
    cx, cy = _zone_center_norm(inner)
    x, y, w, h = outer
    return x <= cx <= x + w and y <= cy <= y + h


# One face track per person (live 09:43 on Camera C: a face read once a second moved and grew
# from 56 to 92 px between reads as its person walked up, and became face tracks 5, 7 and 8).
# A face belongs to the person whose box holds its centre; it always continues that person's
# face track (`face_tid`), however far it moved, and a named person's match carries over to it.
# A face whose centre is in the top FACE_HEAD_FRACTION of a person box is at that person's head.
FACE_HEAD_FRACTION = 0.4


def _face_depth(coord, box):
    """How far down person box `box` the centre of face `coord` is (0 top, 1 bottom)."""
    return (coord[1] + coord[3] / 2 - box[1]) / max(1.0, box[3])


# A face's finder is the person track whose box overlaps the box it was found in by this much
# (or the only one overlapping it at all).
FACE_FINDER_IOU = 0.3


def _face_owner(tracks, coord, boxes=None, parent_coord=None):
    """tracker_id of the person a face belongs to. First the person whose crop found it
    (parent_coord: that person's box when it was read); else the person track whose box holds
    coord's centre nearest its top (the smallest such box when the face is at more than one
    person's head); failing that, the only moving person in view (faces come from the
    once-a-second snapshot, so a face can trail a fast-moving person's live box: live 10:40 on
    Camera C a face fell just outside its person's box and opened a second face track); else
    None. Live 13:33 on Camera B two people crossed: a face found in her crop sat low in my
    smaller box, went to me as the smallest box holding it, and its crop became my thumbnail."""
    boxes = boxes or {}
    people = [(tid, boxes.get(tid) or tr["coord"]) for tid, tr in tracks.items()
              if tr["objectType"] == "person"]
    if parent_coord:
        # The finder has moved since the once-a-second snapshot (13:33 clip: walking, its box
        # overlapped its box at the read by IoU 0.22), so the only person overlapping that box
        # is the finder too.
        overlaps = [(p, _iou(p[1], parent_coord)) for p in people]
        overlaps = [(p, v) for p, v in overlaps if v > 0]
        if overlaps:
            (tid, _box), best = max(overlaps, key=lambda o: o[1])
            if best >= FACE_FINDER_IOU or len(overlaps) == 1:
                return tid
    holders = [(tid, box) for tid, box in people if _center_inside(coord, box)]
    if holders:
        heads = [h for h in holders if _face_depth(coord, h[1]) <= FACE_HEAD_FRACTION]
        if heads:
            return min(heads, key=lambda h: h[1][2] * h[1][3])[0]
        return min(holders, key=lambda h: _face_depth(coord, h[1]))[0]
    moving = [tid for tid, _ in people if not tracks[tid].get("stationary_coord")]
    return moving[0] if len(moving) == 1 else None


def _match_faces_to_people(tracks, detections, matches, unclaimed):
    """Re-point each face detection at its person's face track (matches/unclaimed in place)."""
    boxes = {tid: detections[i]["coord"] for tid, i in matches.items()
             if tracks.get(tid, {}).get("objectType") == "person"}
    claimed, kept = set(), []
    # The same face can turn up in two people's crops (each crop has a margin): the read at
    # its finder's head comes first, so it's the one kept.
    def at_head(d):
        return (d.get("parent_coord") is None
                or _face_depth(d["coord"], d["parent_coord"]) <= FACE_HEAD_FRACTION)
    for idx, d in sorted(enumerate(detections), key=lambda e: (not at_head(e[1]), -e[1]["score"])):
        if d["objectType"] != "face" or d.get("seeded"):
            continue
        if any(_iou(d["coord"], k) >= 0.5 for k in kept):  # the same face found twice
            for tid in [t for t, i in matches.items() if i == idx]:
                del matches[tid]
            if idx in unclaimed:
                unclaimed.remove(idx)
            continue
        kept.append(d["coord"])
        owner = _face_owner(tracks, d["coord"], boxes, d.get("parent_coord"))
        if owner is None:
            continue
        if owner in claimed:  # its person already has a face from this read
            for tid in [t for t, i in matches.items() if i == idx]:
                del matches[tid]
            if idx in unclaimed:
                unclaimed.remove(idx)
            continue
        claimed.add(owner)
        fid = tracks[owner].get("face_tid")
        if fid not in tracks or tracks[fid]["objectType"] != "face":
            continue
        for tid in [t for t, i in matches.items() if i == idx and t != fid]:
            del matches[tid]                       # another face track had taken it
        displaced = matches.get(fid)
        if displaced is not None and displaced != idx:
            unclaimed.append(displaced)
        matches[fid] = idx
        if idx in unclaimed:
            unclaimed.remove(idx)


def _link_faces(tracks, device_id):
    """Record each face track's person (and the person's face track), and give a face the
    match its person already has."""
    for fid, face in tracks.items():
        if face["objectType"] != "face":
            continue
        pid = face.get("person_tid")
        if pid not in tracks:
            pid = _face_owner(tracks, face["coord"])
            if pid is None:
                continue
            face["person_tid"] = pid
        person = tracks[pid]
        if person.get("face_tid") not in tracks:
            person["face_tid"] = fid
        # A face is in a zone when its person's feet are in one listing faces (the face box's
        # own bottom is the chin).
        face_zones = _zones_of(device_id, person["coord"], "face")
        if face_zones:
            face["zone_ids"] = face_zones
        if not face.get("zoned") and person.get("zoned", True) and not _is_outside_detect_zones(
                device_id, person["coord"], "face"):
            face["zoned"] = True
        pextra, fextra = person.get("extra") or {}, face.setdefault("extra", {})
        if pextra.get("matchedId") is not None and fextra.get("matchedId") is None:
            fextra["matchedId"], fextra["matchedName"] = pextra["matchedId"], pextra.get("matchedName")
            log.info("face DB: face tracker_id=%s deviceID=%s takes its person tracker_id=%s's "
                     "match %r", fid, device_id, pid, pextra.get("matchedName"))


def _associate_faces(members):
    """{person tracker_id: face tracker_id}: a person's own face track when it's a member,
    else a face inside its box."""
    def box(tr):
        return (tr.get("best") or {}).get("coord") or tr["coord"]
    people = [(tid, tr) for tid, tr in members if tr["objectType"] == "person"]
    member_ids = {tid for tid, _ in members}
    out = {tid: tr["face_tid"] for tid, tr in people if tr.get("face_tid") in member_ids}
    for fid, face in members:
        if face["objectType"] != "face" or fid in out.values():
            continue
        holders = [(tid, tr) for tid, tr in people
                   if tid not in out and _center_inside(box(face), box(tr))]
        if holders:
            tid, _ = min(holders, key=lambda m: box(m[1])[2] * box(m[1])[3])
            out[tid] = fid
    return out


def _tracker_attrs(tracker_id, tr, faces_by_person):
    """trackerIDAttrMap entry for one participant (attributeSchema keys)."""
    entry = {"objectType": tr["objectType"], "zone": _track_zones(_zones_visited(tr))}
    extra = tr.get("extra") or {}
    # A named face's leave carries its match, not its embedding: live 11:18 Protect kept
    # group:face_186 (Alex) through the event, then clustered the leave's faceEmbed itself into
    # a new unknown group 189, which replaced the name.
    if (tr["objectType"] == "face" and extra.get("faceEmbed")
            and extra.get("matchedId") is None):
        entry["faceEmbed"] = extra["faceEmbed"]
    if tr["objectType"] == "person" and tracker_id in faces_by_person:
        entry["associatedFaceTrackerID"] = faces_by_person[tracker_id]
    if tr["objectType"] in ("face", "person") and extra.get("matchedId") is not None:
        entry["matchedId"], entry["matchedName"] = extra["matchedId"], extra.get("matchedName")
    if tr["objectType"] == "vehicle":
        for key in ("vehicleType", "color"):
            attrs = _vehicle_attrs(tr, leaving=True)
            if key in attrs:
                entry[key] = attrs[key]
        if extra.get("name"):
            entry["matchedName"] = extra["name"]
    return entry


def _send_multi_leave(ws, device_id, event_id, members, fallback):
    """Close a multi-participant event in one leave; run fallback() if Protect then
    requests none of its snapshots within LEAVE_FALLBACK_S."""
    members = sorted(members, key=lambda m: (m[1]["objectType"] == "face",
                                              LEAVE_PRIORITY.get(m[1]["objectType"], 9)))
    now_ms = _frame_wall_ms(device_id)
    clock_mono, clock_stream = _stream_clocks(device_id)
    seq = next(_snapshot_seq)
    fov_w, fov_h = _stream_dims.get(device_id, (DETECT_IMG_SIZE, DETECT_IMG_SIZE))
    fullfov = f"smartdetectsnap_zone_{seq:08d}_fullfov.jpg"
    first_best = members[0][1].get("best")
    _record_snapshot_filename(fullfov, device_id, kind="fullfov",
                              jpeg=first_best["jpeg"] if first_best else None)
    faces_by_person = _associate_faces(members)
    snapshots, attr_map = [], {}
    for tid, tr in members:
        b = tr.get("best")
        crop = list(b["coord"] if b else tr["coord"])
        box = [int(round(v)) for v in _to_protect(device_id, crop)]
        wall = b["wall"] if b else tr["first_shown_ms"]
        name = f"smartdetectsnap_zone_{tid}{wall}.jpg"
        _record_snapshot_filename(name, device_id, kind="object", coord=crop,
                                  jpeg=b["jpeg"] if b else None)
        level = int(round(tr["score"] * 100)) if tr["score"] <= 1 else int(round(tr["score"]))
        snapshots.append({
            "clockBestMonotonic": 0, "clockBestWall": wall, "confidenceLevel": level,
            "coord": box, "framingRect": box, "reVerifyEligible": False,
            "smartDetectSnapshot": name, "smartDetectSnapshotHeight": SMART_SNAPSHOT_SIZE,
            "smartDetectSnapshotName": "", "smartDetectSnapshotType": tr["objectType"],
            "smartDetectSnapshotWidth": SMART_SNAPSHOT_SIZE, "trackerID": tid,
        })
        attr_map[str(tid)] = _tracker_attrs(tid, tr, faces_by_person)
    level = max(s_["confidenceLevel"] for s_ in snapshots)
    payload = {
        "deviceID": device_id, "clockMonotonic": clock_mono, "clockStream": clock_stream,
        "clockStreamRate": 1000, "clockWall": _clock_wall(device_id, now_ms),
        # Departing trackers aren't repeated in descriptors (real captures, notes s7).
        "descriptors": [], "displayTimeoutMSec": 200, "edgeType": "leave",
        "eventId": event_id, "objectTypes": [],
        "smartDetectSnapshotFullFoV": fullfov,
        "smartDetectSnapshotFullFoVHeight": fov_h, "smartDetectSnapshotFullFoVWidth": fov_w,
        "smartDetectSnapshots": snapshots, "trackerIDAttrMap": attr_map,
        "zonesStatus": _zones_status([_zones_visited(tr) for _tid, tr in members], level,
                                     "leave"),
    }
    pending = {"seen": False}
    names = [fullfov] + [x["smartDetectSnapshot"] for x in snapshots]
    for n in names:
        _pending_leaves[n] = pending

    def _check():
        for n in names:
            _pending_leaves.pop(n, None)
        if pending["seen"]:
            log.info("multi-tracker leave for event %s deviceID=%s: snapshots requested",
                     event_id, device_id)
            return
        log.warning("multi-tracker leave for event %s deviceID=%s got no snapshot request in "
                    "%.0fs; resending as a single-tracker leave", event_id, device_id,
                    LEAVE_FALLBACK_S)
        try:
            fallback()
        except Exception:
            log.exception("single-tracker fallback leave failed for event %s", event_id)

    send_msg(ws, "EventSmartDetect", payload, response_expected=False)
    timer = threading.Timer(LEAVE_FALLBACK_S, _check)
    timer.daemon = True
    timer.start()
    return timer


def _leave_track(members):
    """(tracker_id, track) whose snapshot closes the event: face > person > vehicle >
    animal, then the best-scoring frame."""
    return min(members, key=lambda m: (LEAVE_PRIORITY.get(m[1]["objectType"], 9),
                                       -(m[1].get("best") or {}).get("score", m[1]["score"])))


def _close_line_events(ws, device_id, tracker_id, tr):
    """Close any line events this track opened (line events stay per track)."""
    for line_id, entry in tr.get("crossed_lines", {}).items():
        if entry["event_id"] is None:
            continue
        line = next((l for l in _lines.get(device_id, []) if l["id"] == line_id), None)
        if line is None:
            continue
        try:
            _send_line_detect_event(ws, device_id, line, "leave", entry["event_id"],
                                    entry["direction"], entry["a2b"], entry["b2a"],
                                    tr["objectType"], tr["coord"], tr["score"],
                                    tr["first_shown_ms"], tracker_id)
        except Exception:
            log.exception("failed to send line leave for line=%s tracker_id=%s deviceID=%s",
                          line_id, tracker_id, device_id)


def _moved_since(tr):
    """True once a stationary track has really moved off its parked spot (occlusion-proof)."""
    cur, parked = tr["coord"], tr["stationary_coord"]
    if _iou(cur, parked) < STATIONARY_LEFT_IOU:
        return True
    cx, cy = _zone_center_norm(cur)
    sx, sy = _zone_center_norm(parked)
    shifted = ((cx - sx) ** 2 + (cy - sy) ** 2) ** 0.5 > STATIONARY_RESUME_DISTANCE
    ratio = (cur[2] * cur[3]) / max(1.0, parked[2] * parked[3])
    same_size = STATIONARY_SIZE_RATIO[0] <= ratio <= STATIONARY_SIZE_RATIO[1]
    tr["moved_ticks"] = tr.get("moved_ticks", 0) + 1 if shifted and same_size else 0
    return tr["moved_ticks"] >= STATIONARY_RESUME_TICKS


def _builtin_match(tracks, detections):
    """(matches {tracker_id: detection index}, unclaimed indices): each track takes its
    best remaining same-type detection by IoU, then still-unmatched moving tracks take the
    nearest detection where their motion predicts them (parked tracks don't, so a passing
    car can't wake one)."""
    unclaimed = list(range(len(detections)))
    # A lone track and a lone detection of a type no longer match unconditionally: a
    # missed track then took a car anywhere in the frame. The nearest rule below covers a
    # fast mover that no box overlaps.
    matches = {}
    for tracker_id, tr in tracks.items():
        best_idx, best_iou = None, IOU_MATCH_THRESHOLD
        for idx in unclaimed:
            d = detections[idx]
            if d["objectType"] != tr["objectType"]:
                continue
            iou = _iou(tr["coord"], d["coord"])
            if iou > best_iou:
                best_idx, best_iou = idx, iou
        if best_idx is not None:
            matches[tracker_id] = best_idx
            unclaimed.remove(best_idx)
    for tracker_id, tr in sorted(tracks.items(), key=lambda m: m[1]["miss_count"]):
        if tracker_id in matches or tr.get("stationary_coord"):
            continue
        idx = _nearest_detection(tr, detections, unclaimed)
        if idx is not None:
            matches[tracker_id] = idx
            unclaimed.remove(idx)

    return matches, unclaimed


def _norfair_match(dev, tracks, detections):
    """(matches, unclaimed, pending) with Norfair first: a detection associated with an
    object that maps to a live track matches it, except that a parked track only takes a
    box overlapping its parked spot (a passing car mustn't take over a parked one). Then
    pi-port's own rules for what Norfair left: parked tracks by IoU, moving tracks by the
    nearest detection where their motion predicts them (a small person moving 3x their width
    between frames is past Norfair's distance limit). pending {index: Norfair id} binds each
    Norfair object to the track its detection ends up in."""
    import norfair_match
    nf = dev.get("norfair")
    if nf is None:
        nf = dev["norfair"] = norfair_match.Matcher(PARKED_MISS_TOLERANCE_TICKS)
    ids = dev.setdefault("norfair_ids", {})
    for gid in [g for g, tid in ids.items() if tid not in tracks]:
        del ids[gid]
    associated = nf.match(detections)
    matches = {}
    # A parked object's repeated box is its own (two overlapping parked boxes on one camper
    # mustn't trade places through Norfair).
    for idx, d in enumerate(detections):
        tid = d.get("seeded")
        if tid in tracks and tid not in matches:
            matches[tid] = idx
            if idx in associated:
                ids[associated[idx]] = tid
    for idx, gid in associated.items():
        if idx in matches.values():
            continue
        tid = ids.get(gid)
        tr = tracks.get(tid)
        if tr is None or tid in matches:
            continue
        if (tr.get("stationary_coord")
                and _iou(tr["stationary_coord"], detections[idx]["coord"]) < IOU_MATCH_THRESHOLD):
            del ids[gid]  # Norfair moved this object onto another one: the parked track lets go
            continue
        if not tr.get("stationary_coord") and _size_jump(_size_ref(tr), detections[idx]["coord"]):
            del ids[gid]  # a box of another size beside it is another object
            continue
        matches[tid] = idx
    unclaimed = [i for i in range(len(detections)) if i not in set(matches.values())]
    for tid, tr in tracks.items():
        if tid in matches:
            continue
        idx = None
        if tr.get("stationary_coord"):
            best = IOU_MATCH_THRESHOLD
            for i in unclaimed:
                d = detections[i]
                if d["objectType"] == tr["objectType"]:
                    iou = _iou(tr["stationary_coord"], d["coord"])
                    if iou >= best:
                        idx, best = i, iou
        else:
            idx = _nearest_detection(tr, detections, unclaimed)
        if idx is not None:
            matches[tid] = idx
            unclaimed.remove(idx)
            if idx in associated:
                ids[associated[idx]] = tid
    pending = {i: associated[i] for i in unclaimed if i in associated}
    return matches, unclaimed, pending


def _frame_gray(device_id):
    """This frame in grayscale (detector-frame pixels), or None without one or OpenCV."""
    with _frame_lock:
        frame = _latest_frame.get(device_id)
    if frame is None:
        return None
    try:
        import cv2
    except ImportError:
        return None
    return cv2.cvtColor(frame, cv2.COLOR_RGB2GRAY)


def _coord_px(coord, shape):
    """Image-normalised [x, y, w, h] -> pixel box (x1, y1, x2, y2) in a frame of shape."""
    h, w = shape[:2]
    x, y, bw, bh = coord
    s = CAMERA_COORD_END
    return (x * w / s, y * h / s, (x + bw) * w / s, (y + bh) * h / s)


def _anchor_parked(device_id, tracks, now_ms=0):
    """Give each parked track an anchor crop of its spot, for _parked_spot_unchanged."""
    todo = [tr for tr in tracks.values() if tr.get("stationary_coord") and "pixel_anchor" not in tr]
    if not todo:
        return
    gray = _frame_gray(device_id)
    if gray is None:
        return
    import stationary
    for tr in todo:
        tr["pixel_anchor"] = stationary.anchor(gray, _coord_px(tr["stationary_coord"], gray.shape))
        tr["anchor_ms"] = now_ms


def _parked_spot_unchanged(device_id, tracker_id, tr):
    """True if the parked track's spot still looks as it did when it parked (Frigate's
    stationary classifier), though its boxes say it moved. Without an anchor or a frame,
    False: the boxes decide, as before."""
    state = tr.get("pixel_anchor")
    gray = _frame_gray(device_id) if state else None
    if gray is None:
        return False
    import stationary
    keep = stationary.still_there(state, gray)
    ncc, shift = state["last"]
    log.log(logging.DEBUG if keep else logging.INFO,
            "real motion tick: parked %s tracker_id=%s boxes moved; its spot %s (ncc %.2f, "
            "shift %.3f) for deviceID=%s", tr["objectType"], tracker_id,
            "looks the same, staying parked" if keep else "changed", ncc, shift, device_id)
    return keep


EMPTY_MOTION_LOG_S = 60.0
_empty_motion = {}  # deviceID -> [monotonic s of the last log line, frames since]


def _log_empty_motion(device_id):
    """"Scene changed but nothing found", at most once a minute per camera with the count:
    leaves and shadows on Camera C moved on ~40 frames a minute."""
    now = time.monotonic()
    entry = _empty_motion.setdefault(device_id, [None, 0])
    entry[1] += 1
    if entry[0] is None or now - entry[0] >= EMPTY_MOTION_LOG_S:
        log.info("real motion tick: scene changed but no person/vehicle/animal found for "
                 "deviceID=%s -- ignoring (not smart-detect-worthy; %d such frame(s) since "
                 "the last note)", device_id, entry[1])
        entry[0], entry[1] = now, 0


_dropped = {}  # (deviceID, objectType, reason) -> [monotonic s of the last log line, count]


def _log_dropped(device_id, r, reason):
    """A detection dropped by the zones, at most once a minute per camera, type and reason
    with the count: live 18:10 Camera A's parked cars (its zone is person/animal only)
    logged a line several times a second."""
    now = time.monotonic()
    entry = _dropped.setdefault((device_id, r["objectType"], reason), [None, 0])
    entry[1] += 1
    if entry[0] is None or now - entry[0] >= EMPTY_MOTION_LOG_S:
        log.info("real motion tick: %s detection %s for deviceID=%s (score=%.2f; %d such "
                 "detection(s) since the last note)", r["objectType"], reason, device_id,
                 r["score"], entry[1])
        entry[0], entry[1] = now, 0


def _motion_tick(ws, device_id):
    # With open tracks we always classify (a stationary object must keep its track
    # and be able to close); otherwise skip the classifier when nothing moved.
    with _motion_lock:
        dev_state = _motion_state.get(device_id, {})
        has_tracks = bool(dev_state.get("tracks"))
        started = dev_state.get("started_ms")
    with _frame_lock:  # this tick describes this frame, whether or not the gate skips it
        if device_id in _latest_frame_time:
            _analysed_frame_time[device_id] = _latest_frame_time[device_id]
    changed = _scene_changed(device_id)
    # The startup look runs on a still scene too: live 17:05 Camera A saw no motion for
    # 27 s at dusk, so its parked cars were first detected after the window and opened an event.
    in_startup = started is None or _frame_wall_ms(device_id) - started <= STARTUP_WINDOW_MS
    gated = MOTION_GATE and not has_tracks and not changed and not in_startup
    if not gated:
        _inferred[device_id] += 1
    if gated:
        log.debug("motion gate: static scene, skipping inference for deviceID=%s", device_id)
        detections = []
    else:
        t0 = time.monotonic()
        detections = _dedupe_detections(_classify_frame_detections(device_id))
        # AIPORT_DEBUG: proves frames reach the detector and what it returned.
        log.debug("motion tick: deviceID=%s inference=%.0f ms detections=%d (%s)",
                  device_id, (time.monotonic() - t0) * 1000, len(detections),
                  ", ".join(f"{d['objectType']} {d['score']:.2f}" for d in detections) or "none")
        detections += _enrich_detections(
            device_id, detections)
    with _motion_lock:
        now_ms = _frame_wall_ms(device_id)
        confirm_ms = _confirm_ms(device_id)
        dev = _motion_state.setdefault(device_id, {"tracks": {}, "next_tracker_id": 1,
                                                   "started_ms": None})
        tracks = dev["tracks"]
        event = dev.get("event")
        dev["recent"] = [r for r in dev.get("recent", ())
                         if now_ms - r[2] <= RESUME_S * 1000][-RESUME_MAX:]
        # The startup window starts on the stream's first analysed frame, not on a tick with
        # no frame yet (live 17:02 on Camera B ticks ran through a 10 s reconnect and used it up).
        if dev.get("started_ms") is None and device_id in _analysed_frame_time:
            dev["started_ms"] = now_ms
        starting = dev.get("started_ms") is None or now_ms - dev["started_ms"] <= STARTUP_WINDOW_MS  # {"id", "started_ms", "finished": [(tid, tr)]} or None
        unclaimed = list(range(len(detections)))
        joining = []  # tracks that start (or resume) taking part in the event this tick
        # Zones decide what's reported, not what's tracked (as Frigate): every detection is
        # tracked; a track joins the event only once its feet have been in a zone listing its
        # type, and stays in it until it's gone. Live 11:38 on Camera B a walk to the front door
        # crossed a gap between zones; dropping the detections there starved the person's track,
        # named Alex, and it never made the event.
        for d in detections:
            if "in_zone" not in d:
                d["zone_ids"] = _zones_of(device_id, d["coord"], d["objectType"])
                d["in_zone"] = bool(d.get("seeded")) or bool(d["zone_ids"]) \
                    or not _detect_zones.get(device_id)

        # 1. Which detection belongs to which track: Norfair (AIPORT_TRACKER=norfair) or
        #    the built-in IoU + nearest-neighbour matcher.
        if TRACKER == "norfair":
            matches, unclaimed, pending_ids = _norfair_match(dev, tracks, detections)
        else:
            matches, unclaimed = _builtin_match(tracks, detections)
            pending_ids = {}
        _match_faces_to_people(tracks, detections, matches, unclaimed)
        # A box still cut off by its crop after the second look neither moves a moving track
        # (that frame counts as a miss) nor starts one; a parked track may still take it.
        for tid in [t for t, i in matches.items()
                    if detections[i].get("clipped") and not tracks[t].get("stationary_coord")]:
            del matches[tid]
        for idx in [i for i in unclaimed if detections[i].get("clipped")]:
            unclaimed.remove(idx)

        for tracker_id, tr in list(tracks.items()):
            if tracker_id in matches:
                d = detections[matches[tracker_id]]
                _follow(tr, d["coord"])
                tr["score"] = d["score"]
                tr["miss_count"] = 0
                if _center_moved(tr.get("first_coord") or d["coord"], d["coord"]):
                    tr["moved"] = True
                if _iou(tr.get("first_coord") or d["coord"], d["coord"]) < STILL_IOU:
                    tr["left_first_box"] = True
                _record_score(tr, d["score"], device_id)
                _update_still(tr, now_ms)
                if (tr.get("stationary_coord") and not d.get("seeded")
                        and _iou(d["coord"], tr["stationary_coord"]) >= STILL_IOU
                        and now_ms - tr.get("anchor_ms", now_ms) >= PARKED_RECHECK_S * 1000):
                    # Re-detected right where it parked: take a fresh anchor, so slow light
                    # and shadow changes don't add up to "its spot changed" (a parked camper's
                    # spot fell to NCC 0.82 over 4 minutes of moving sun, with no shift).
                    tr.pop("pixel_anchor", None)
                if tr.get("stationary_coord") and _moved_since(tr):
                    if _parked_spot_unchanged(device_id, tracker_id, tr):
                        # Its boxes moved but its pixels didn't (jitter, a partial box,
                        # someone walking past): it stays where it parked.
                        tr["coord"] = list(tr["stationary_coord"])
                        tr["moved_ticks"] = 0
                    else:
                        # A stationary object moved again: it rejoins as a fresh participant.
                        log.info("real motion tick: stationary %s tracker_id=%s moved, rejoining "
                                 "for deviceID=%s", tr["objectType"], tracker_id, device_id)
                        tr.pop("stationary_coord")
                        tr.pop("moved_ticks", None)
                        tr.pop("pixel_anchor", None)
                        tr["first_shown_ms"] = now_ms
                        tr.pop("best", None)
                        tr.pop("anchor", None)
                        _update_still(tr, now_ms)
                        if event is not None:  # parked earlier in this same event
                            event["finished"] = [m for m in event["finished"]
                                                 if m[0] != tracker_id]
                        joining.append((tracker_id, tr))
                _merge_extra(tr, d, device_id, tracker_id)
                _note_face_read(tr, d, now_ms)
                _capture_best(device_id, tr, d)
                tr["seen_ms"] = now_ms
                # Line Crossing: check this frame's movement against configured lines.
                new_center = _protect_center(device_id, d["coord"])
                if tr["objectType"] not in ("licensePlate", "face") and tr.get("confirmed", True):
                    _process_line_crossings(ws, device_id, tracker_id, tr, new_center)
                tr["prev_center"] = new_center
                continue
            # No matching detection this frame for this track.
            tr["miss_count"] += 1
            _record_score(tr, 0.0, device_id)
            tolerance = (PARKED_MISS_TOLERANCE_TICKS if tr.get("stationary_coord")
                         else MOTION_MISS_TOLERANCE_TICKS)
            if not tr.get("confirmed", True) and tr["miss_count"] >= tolerance:
                # Gone before it was confirmed: a passing false detection, no event.
                log.debug("real motion tick: unconfirmed %s tracker_id=%s lost (median score "
                          "%.2f), dropped for deviceID=%s", tr["objectType"], tracker_id,
                          statistics.median(tr.get("score_history") or [0.0]), device_id)
                del tracks[tracker_id]
                continue
            if (tr["objectType"] == "face" and tr.get("person_tid") in tracks
                    and not tracks[tr["person_tid"]].get("stationary_coord")):
                # A face isn't gone while its person is still tracked: live 11:06 on Camera C a
                # person looked away for 5 s, the face track closed, and their face came back
                # as a second face track.
                continue
            if tr["miss_count"] < tolerance:
                # Within the tolerance window -- send nothing, keep the track.
                log.debug("real motion tick: no match (miss %d/%d) for tracker_id=%s deviceID=%s -- "
                          "tolerating brief gap, track still open", tr["miss_count"],
                          tolerance, tracker_id, device_id)
                continue
            if tr["objectType"] == "vehicle" and _fm_hold(tr, now_ms):
                continue   # its model answer is on the way: the leave should carry it
            # Missed enough consecutive ticks -- this track is genuinely gone.
            log.info("real motion tick: no detection for %d consecutive ticks, closing smartDetectZone "
                      "track for deviceID=%s tracker_id=%s (last seen: %s score=%.2f)",
                      tolerance, device_id, tracker_id,
                      tr["objectType"], tr["score"])
            if event is not None and not tr.get("stationary_coord"):
                event["finished"].append((tracker_id, tr))  # its snapshot goes in the leave
            _close_line_events(ws, device_id, tracker_id, tr)
            _save_face_crop(device_id, tracker_id, tr)
            del tracks[tracker_id]
            dev["recent"].append((tracker_id, tr, now_ms))

        # 2. Any unclaimed detection resumes a track that just closed, or opens a new track
        #    (which joins the camera event once confirmed).
        for idx in unclaimed:
            d = detections[idx]
            part_of = _inside_parked(tracks, d)
            if part_of is not None:
                log.debug("real motion tick: %s (score %.2f) lies inside parked tracker_id=%s for "
                          "deviceID=%s -- part of it, ignored", d["objectType"], d["score"],
                          part_of, device_id)
                continue
            resumed = _resume_recent(dev, d, event)
            if resumed:
                tracker_id, tr, how = resumed
                tracks[tracker_id] = tr
                if idx in pending_ids:
                    dev["norfair_ids"][pending_ids[idx]] = tracker_id
                _follow(tr, d["coord"])
                tr["score"] = d["score"]
                tr["miss_count"] = 0
                tr["seen_ms"] = now_ms
                tr["prev_center"] = _protect_center(device_id, d["coord"])
                _update_still(tr, now_ms)
                _merge_extra(tr, d, device_id, tracker_id)
                _note_face_read(tr, d, now_ms)
                _capture_best(device_id, tr, d)
                if how == "event":
                    event["finished"] = [m for m in event["finished"] if m[0] != tracker_id]
                    joining.append((tracker_id, tr))
                else:
                    _park(tr)
                log.info("real motion tick: %s resumed tracker_id=%s (%s) for deviceID=%s "
                         "(score=%.2f)", d["objectType"], tracker_id,
                         "back in its event" if how == "event" else "never moved, parked",
                         device_id, d["score"])
                continue
            tracker_id = dev["next_tracker_id"]
            dev["next_tracker_id"] += 1
            if idx in pending_ids:
                dev["norfair_ids"][pending_ids[idx]] = tracker_id
            tracks[tracker_id] = {"objectType": d["objectType"],
                                  "coord": d["coord"], "score": d["score"],
                                  "first_shown_ms": now_ms, "seen_ms": now_ms, "miss_count": 0,
                                  "first_coord": list(d["coord"]),
                                  "prev_center": _protect_center(device_id, d["coord"]),
                                  "crossed_lines": {}}
            _record_score(tracks[tracker_id], d["score"], device_id)
            tracks[tracker_id]["confirmed"] = starting or (
                confirm_ms <= 0 and tracks[tracker_id].get("true_positive")
                and d["objectType"] not in CONFIRM_WITH)
            _update_still(tracks[tracker_id], now_ms)
            _merge_extra(tracks[tracker_id], d, device_id, tracker_id)
            _note_face_read(tracks[tracker_id], d, now_ms)
            _capture_best(device_id, tracks[tracker_id], d)
            if starting:
                # Already there when the tracker started (a parked car): no event until it moves.
                _park(tracks[tracker_id])
                log.info("real motion tick: %s already present at start, parked for deviceID=%s "
                         "tracker_id=%s", d["objectType"], device_id, tracker_id)
                continue
            if not tracks[tracker_id]["confirmed"]:
                log.debug("real motion tick: %s detected (score=%.2f), tracker_id=%s waiting to "
                          "confirm for deviceID=%s", d["objectType"], d["score"], tracker_id,
                          device_id)
                continue
            log.info("real motion tick: %s detected (score=%.2f), opening smartDetectZone track "
                      "for deviceID=%s tracker_id=%s", d["objectType"], d["score"],
                      device_id, tracker_id)
            joining.append((tracker_id, tracks[tracker_id]))

        _link_faces(tracks, device_id)

        # 2a. An unconfirmed track keeps each frame's box, sent back-dated when it confirms.
        stamp = _frame_time(device_id)
        for tr in tracks.values():
            if not tr.get("confirmed", True) and tr.get("seen_ms") == now_ms:
                history = tr.setdefault("history", [])
                history.append((stamp, list(tr["coord"]), tr["score"]))
                del history[:-BACKFILL_MAX]

        # 2b. A track confirms once it's a true positive (median score) and has been around
        #     for Protect's eventStartMSec -- even on a frame it was missed in, so an object
        #     seen on only its first few frames still makes its event.
        #     A vehicle still on the box it was first seen on waits: the yolov5n model misses
        #     the red car parked on Camera B, and a crop zoomed in on a passing road car found
        #     its side mirror as a small "vehicle" that joined the car's event (live 12:59 and
        #     the 17:20 clip). A car that is arriving or passing leaves its first box at once.
        for tid, tr in tracks.items():
            if (not tr.get("confirmed", True) and tr.get("true_positive")
                    and tr["objectType"] not in CONFIRM_WITH
                    and (tr["objectType"] not in PARK_WHEN_STILL or tr.get("left_first_box"))
                    and now_ms - tr["first_shown_ms"] >= confirm_ms):
                tr["confirmed"] = True
                log.info("real motion tick: %s tracker_id=%s confirmed after %d ms (median score "
                         "%.2f), joining for deviceID=%s", tr["objectType"], tid,
                         now_ms - tr["first_shown_ms"], statistics.median(tr["score_history"]),
                         device_id)
                joining.append((tid, tr))

        # 3. A vehicle that has stopped (still ~30 s) parks; its snapshot stays in the leave.
        #    One that never left its first box parks with no event.
        for tid, tr in tracks.items():
            if (not tr.get("stationary_coord") and tr.get("still")
                    and tr["objectType"] in PARK_WHEN_STILL and not tr.get("confirmed", True)
                    and tr.get("true_positive") and not tr.get("left_first_box")):
                tr["confirmed"] = True
            if (not tr.get("stationary_coord") and tr.get("still")
                    and tr["objectType"] in PARK_WHEN_STILL and tr.get("confirmed", True)):
                log.info("real motion tick: %s tracker_id=%s still for %d ticks, parked for "
                         "deviceID=%s", tr["objectType"], tid, tr["still_ticks"], device_id)
                _park(tr)
                if event is not None and tr.get("event_id") == event["id"]:
                    event["finished"].append((tid, tr))

        # 3b. A face or plate confirms as soon as a confirmed, active track of its parent
        #     type is there (it was found inside that track's box); a face only once a read
        #     was big enough to name (_note_face_read).
        for tid, tr in tracks.items():
            parent = CONFIRM_WITH.get(tr["objectType"])
            if tr["objectType"] == "face" and not tr.get("nameable"):
                continue
            if parent and not tr.get("confirmed", True) and tr["miss_count"] == 0 and any(
                    p["objectType"] == parent and p.get("confirmed", True)
                    and not p.get("stationary_coord") for p in tracks.values()):
                tr["confirmed"] = True
                log.info("real motion tick: %s tracker_id=%s confirmed with its %s, joining for "
                         "deviceID=%s", tr["objectType"], tid, parent, device_id)
                joining.append((tid, tr))

        # 3c. A confirmed track joins only once it has been in a zone listing its type.
        joining = _hold_for_zones(device_id, tracks, joining)

        # 4. Camera event lifecycle. Unconfirmed (or not yet zoned) tracks aren't part of it.
        active = [(tid, tr) for tid, tr in tracks.items()
                  if not tr.get("stationary_coord") and tr.get("confirmed", True)
                  and tr.get("zoned", True)]
        # A track can join and park in one tick (still since before it was confirmed):
        # that's a parked car, not an event.
        if event is None and joining and active:
            event = dev["event"] = {"id": next(_event_id_counter), "started_ms": now_ms,
                                    "finished": []}
            for _, tr in active:
                tr["event_id"] = event["id"]
            log.info("smartDetectZone event %s opened for deviceID=%s", event["id"], device_id)
            backfilled = _send_backfill(ws, device_id, event["id"], active, joining)
            _send_event_message(ws, device_id, "moving" if backfilled else "enter", event["id"],
                                active)
            dev["last_report_ms"] = now_ms
        elif event is not None:
            for _, tr in joining:
                tr["event_id"] = event["id"]
                tr.pop("history", None)  # an open event's timeline isn't rewound
            max_ms = _event_max_ms.get(device_id, DEFAULT_EVENT_MAX_MS)
            timed_out = now_ms - event["started_ms"] >= max_ms
            if not active or timed_out:
                members = event["finished"] + active
            if (not active or timed_out) and not members:
                # Nothing to report (shouldn't happen): close it quietly rather than
                # failing every frame with the event stuck open.
                log.warning("smartDetectZone event %s for deviceID=%s has no tracks left; "
                            "closing it without a leave", event["id"], device_id)
                dev["event"] = None
            elif not active or timed_out:
                lead = _leave_track(members)
                log.info("smartDetectZone event %s closed for deviceID=%s (%s; %d track(s), "
                         "snapshot from %s tracker_id=%s)", event["id"], device_id,
                         "time limit" if timed_out else "all gone", len(members),
                         lead[1]["objectType"], lead[0])
                def single(ws=ws, event_id=event["id"], lead=lead):
                    _send_event_message(ws, device_id, "leave", event_id, [lead], stationary=True)
                if len(members) > 1:
                    try:
                        _send_multi_leave(ws, device_id, event["id"], members, single)
                    except Exception:
                        log.exception("multi-tracker leave failed for event %s", event["id"])
                        single()
                else:
                    single()
                for _, tr in active:  # still in view: quiet until it moves again
                    _park(tr)
                dev["event"] = None
            elif now_ms - dev.get("last_report_ms", now_ms - REPORT_INTERVAL_MS) >= REPORT_INTERVAL_MS:
                # One batched update per report interval, with only the tracks seen since the
                # last one: a track inside its miss tolerance stays in the event (and its
                # leave) but isn't drawn where it was last seen.
                last = dev.get("last_report_ms")
                seen = [m for m in active
                        if last is None or m[1].get("seen_ms", now_ms) > last]
                if seen:
                    _send_event_message(ws, device_id, "moving", event["id"], seen)
                    dev["last_report_ms"] = now_ms

        # 5. A parked track without a pixel anchor gets one from this frame.
        _anchor_parked(device_id, tracks, now_ms)

        # Logged only when the frame really changed: with the gate off every frame is
        # classified, and an empty one isn't news.
        if not detections and not tracks and not gated and changed:
            # Classified and found nothing = non-smart motion (lighting/noise); ignore.
            _log_empty_motion(device_id)


def _stop_motion_detector(device_id, preserve_tracks=False, ws=None):
    """Stop this device's poll loop.

    ws: the live avclient connection, needed to emit `leave` for open tracks
    (without one those leaves fail and are only logged).

    preserve_tracks=True keeps `_motion_state` (open tracks, prev_center) so a
    UiStreamControl refresh does not silently abandon an in-flight track. Only a
    genuine teardown (stream stop / un-adopt) should discard track state -- and
    when a track IS discarded with an open event, emit a `leave` first so the
    controller closes it instead of leaving a dangling event.
    """
    if not preserve_tracks:
        with _motion_lock:
            dev = _motion_state.get(device_id)
            open_tracks = list((dev or {}).get("tracks", {}).items())
            _motion_state.pop(device_id, None)
        # Emit leave for anything still open (a discarded track with no leave
        # leaves an orphaned event on the controller).
        for tracker_id, tr in open_tracks:
            if "event_id" not in tr:
                continue   # never joined an event (held, unconfirmed or parked): nothing to close
            try:
                _send_smart_detect_event(ws, device_id, edge_type="leave",
                                          event_id=tr["event_id"], stationary=True,
                                          object_type=tr["objectType"], coord=tr["coord"],
                                          confidence=tr["score"],
                                          first_shown_ms=tr["first_shown_ms"],
                                          tracker_id=tracker_id)
                log.info("emitted leave for discarded track tracker_id=%s deviceID=%s "
                          "(detector stopping)", tracker_id, device_id)
            except Exception:
                log.exception("failed to emit leave for discarded tracker_id=%s deviceID=%s",
                              tracker_id, device_id)
    ev = _motion_poll_stop.pop(device_id, None)
    if ev:
        ev.set()
    # Join the loop: stop_event is checked only between ticks, so an old thread
    # could still be inside _motion_tick() and mutate the same track state.
    t = _motion_poll_thread.pop(device_id, None)
    if t and t.is_alive():
        t.join(timeout=MOTION_POLL_INTERVAL_S + 5.0)


# Per-camera frame accounting, logged once a minute: frames ffmpeg delivered (from its frame
# numbers), frames analysed, frames skipped (replaced by a newer one before a pass picked them
# up), inference runs (the rest were gated off as static) with the detector calls each made
# (one per region crop and second look), and the average pass time. A camera
# analysing under FRAME_RATE_WARN of DETECT_FPS is falling behind. The line ends with the
# camera's box delay (AIPORT_BOX_DELAY_MS), which back-dates its boxes.
FRAME_STATS_S = 60.0
FRAME_RATE_WARN = 0.9
_frame_stats = {}     # deviceID -> {"t0", "first_seq", "last_seq", "analysed", "busy_s"}
_inferred = collections.Counter()   # deviceID -> passes that ran the detector
_detector_calls = collections.Counter()   # deviceID -> detector runs (crops) over those passes


def _note_frame(device_id, seq, busy_s, now=None):
    """Count one analysed frame; log the camera's minute when it's up."""
    if seq is None:   # no frame numbered yet (a stream just starting)
        return
    now = time.monotonic() if now is None else now
    st = _frame_stats.get(device_id)
    if st is None or seq < st["last_seq"]:
        # first frame, or a new stream (its frame numbers start again)
        _frame_stats[device_id] = {"t0": now, "first_seq": seq, "last_seq": seq, "analysed": 0,
                                   "busy_s": 0.0, "inferred0": _inferred[device_id],
                                   "calls0": _detector_calls[device_id]}
        return
    st["analysed"] += 1
    st["busy_s"] += busy_s
    st["last_seq"] = seq
    elapsed = now - st["t0"]
    if elapsed < FRAME_STATS_S:
        return
    delivered = seq - st["first_seq"]
    analysed = st["analysed"]
    inferred = _inferred[device_id] - st["inferred0"]
    calls = _detector_calls[device_id] - st["calls0"]
    fps_in, fps_done = delivered / elapsed, analysed / elapsed
    behind = fps_done < FRAME_RATE_WARN * DETECT_FPS
    (log.warning if behind else log.info)(
        "frames for deviceID=%s over %.0f s: %d delivered (%.1f/s), %d analysed (%.1f/s), %d "
        "skipped, %d with inference (%.1f detector calls each), %.0f ms a pass, box delay %d ms%s",
        device_id, elapsed, delivered, fps_in, analysed, fps_done, max(0, delivered - analysed),
        inferred, calls / max(inferred, 1),
        1000 * st["busy_s"] / max(analysed, 1), _box_delay_ms(device_id),
        " -- falling behind the %d fps detection rate" % DETECT_FPS if behind else "")
    _frame_stats[device_id] = {"t0": now, "first_seq": seq, "last_seq": seq, "analysed": 0,
                               "busy_s": 0.0, "inferred0": _inferred[device_id],
                               "calls0": _detector_calls[device_id]}


def _start_motion_detector(ws, device_id, ip, port, uri, preserve_tracks=False):
    # Runs the classifier periodically on the latest frame; no scene-diff pre-filter
    # (unreliable, and NPU inference is cheap).
    _stop_motion_detector(device_id, preserve_tracks=preserve_tracks)
    stop_event = threading.Event()
    _motion_poll_stop[device_id] = stop_event

    def _poll_loop():
        last_stamp = None
        while not stop_event.is_set():
            # Wake on each new frame (live 12:43: a fixed 0.1 s wait before each 11-20 ms pass
            # analysed only 7-8 of 10 frames a second). With no stream there's no frame: the
            # timeout keeps ticking every DETECT_INTERVAL_S so tracks still close.
            with _frame_cond:
                _frame_cond.wait_for(
                    lambda: stop_event.is_set()
                    or _latest_frame_time.get(device_id) != last_stamp,
                    timeout=DETECT_INTERVAL_S)
                stamp = _latest_frame_time.get(device_id)
            if stop_event.is_set():
                return
            # Superseded by a newer loop -> this one must not touch shared state.
            if _motion_poll_stop.get(device_id) is not stop_event:
                log.info("motion poll loop superseded for deviceID=%s -- exiting", device_id)
                return
            # Analyse each frame once: with no new frame since the last pass, wait again.
            if stamp is not None and stamp == last_stamp:
                continue
            last_stamp = stamp
            with _frame_lock:
                seq = _latest_frame_seq.get(device_id)
            started = time.monotonic()
            try:
                _motion_tick(ws, device_id)
            except Exception:
                log.exception("motion poll tick crashed for deviceID=%s", device_id)
            _last_analysed[device_id] = time.monotonic()
            try:
                _note_frame(device_id, seq, time.monotonic() - started)
            except Exception:  # accounting must never stop a camera
                log.exception("frame stats failed for deviceID=%s", device_id)

    log.info("starting real motion detector (up to %.0f frames/s, reports every %.2fs) for "
             "deviceID=%s", 1 / DETECT_INTERVAL_S, MOTION_POLL_INTERVAL_S, device_id)
    t = threading.Thread(target=_poll_loop, daemon=True)
    _motion_poll_thread[device_id] = t
    t.start()


def _record_snapshot_filename(filename, device_id, kind, coord=None, jpeg=None):
    _snapshot_filename_to_device[filename] = {
        "device_id": device_id, "kind": kind, "coord": coord, "jpeg": jpeg,
    }
    while len(_snapshot_filename_to_device) > _SNAPSHOT_FILENAME_MAP_MAX:
        _snapshot_filename_to_device.popitem(last=False)
    # Bound memory: only the newest records keep their best-frame JPEG.
    holding = [r for r in _snapshot_filename_to_device.values() if r.get("jpeg")]
    for r in holding[:max(0, len(holding) - BEST_FRAME_RECORDS_MAX)]:
        r["jpeg"] = None
    # Record for the fallback; collapse consecutive repeats from one leave (two filenames).
    if not _recent_snapshot_announcements or _recent_snapshot_announcements[-1][1] != device_id:
        _recent_snapshot_announcements.append((time.monotonic(), device_id))
    if kind == "object" and coord:
        _last_object_coord_by_device[device_id] = coord


def _find_device_by_id(requested_device_id):
    """Match a controller-supplied deviceID (a paired camera MAC, upper or lower
    case, with or without separators) to a known active/known camera id, or None."""
    if not requested_device_id:
        return None
    want = str(requested_device_id).lower().replace(":", "").replace("-", "")
    if not want:
        return None
    known = list(_active_streams) + [d for d in _stream_dims if d not in _active_streams]
    for device_id in known:
        if str(device_id).lower().replace(":", "").replace("-", "") == want:
            return device_id
    return None


def _resolve_snapshot_device(requested_filename, what, requested_device_id=None):
    """Resolve a snapshot GetRequest to a camera; returns (record, how), record
    None if ambiguous. The controller's general snapshot request (camera
    overview/timeline thumbnail) carries no `filename` but DOES carry a
    `deviceID` naming the paired camera -- use it. Otherwise fall back only to a
    sole active stream or sole recent announcement."""
    record = _snapshot_filename_to_device.get(requested_filename)
    if record is not None:
        return record, "filename"
    device_id = _find_device_by_id(requested_device_id)
    if device_id is not None:
        return _fallback_record(device_id, what), "request deviceID"
    now = time.monotonic()
    recent = [dev for t, dev in _recent_snapshot_announcements
              if now - t <= SNAPSHOT_FALLBACK_WINDOW_S]
    distinct = list(dict.fromkeys(recent))
    active = list(_active_streams)
    if len(active) == 1:
        return _fallback_record(active[0], what), "sole active stream"
    if len(distinct) == 1:
        return _fallback_record(distinct[0], what), "sole recent announcement"
    log.warning("snapshot upload requested for unknown filename=%r (what=%r, deviceID=%r): "
                "%d active stream(s), %d distinct camera(s) announced in the last %ds -- "
                "ambiguous, refusing to guess",
                requested_filename, what, requested_device_id, len(active), len(distinct),
                SNAPSHOT_FALLBACK_WINDOW_S)
    return None, None


def _fallback_record(device_id, what):
    # Only smart-detect object snapshots are crops; a general/FullFoV snapshot is
    # the full frame even if a stale coord is on file.
    w = str(what or "").lower()
    kind = "object" if ("smartdetect" in w and "fullfov" not in w) else "fullfov"
    coord = _last_object_coord_by_device.get(device_id) if kind == "object" else None
    return {"device_id": device_id, "kind": kind, "coord": coord}


def _jpeg_size(path):
    """(width, height) from a JPEG's SOF header, or None."""
    try:
        with open(path, "rb") as f:
            if f.read(2) != b"\xff\xd8":
                return None
            while True:
                marker = f.read(2)
                if len(marker) < 2 or marker[0] != 0xFF:
                    return None
                seg_len = int.from_bytes(f.read(2), "big")
                # SOF0-SOF15, excluding DHT (C4), JPG (C8) and DAC (CC)
                if 0xC0 <= marker[1] <= 0xCF and marker[1] not in (0xC4, 0xC8, 0xCC):
                    sof = f.read(5)
                    return int.from_bytes(sof[3:5], "big"), int.from_bytes(sof[1:3], "big")
                f.seek(seg_len - 2, 1)
    except OSError:
        return None


def _crop_snapshot_jpeg(src_path, coord, fov_w, fov_h):
    """Square object-crop JPEG centered on the 0-1000 coord, scaled to
    SMART_SNAPSHOT_SIZE (ffmpeg); None on failure."""
    # The relay can deliver a different size than UiStreamControl announced
    # (a 1920x2560 doorbell arrived as 1504x2016); crop in the JPEG's real pixels.
    fov_w, fov_h = _jpeg_size(src_path) or (fov_w, fov_h)
    x, y, w, h = coord
    cx = (x + w / 2) / CAMERA_COORD_END * fov_w
    cy = (y + h / 2) / CAMERA_COORD_END * fov_h
    box_w = w / CAMERA_COORD_END * fov_w
    box_h = h / CAMERA_COORD_END * fov_h
    side = max(box_w, box_h) * SMART_SNAPSHOT_MARGIN
    side = max(float(SMART_SNAPSHOT_SIZE), min(side, float(min(fov_w, fov_h))))
    side = int(side) // 2 * 2  # even: required for yuvj420p JPEG dimensions
    crop_x = max(0, min(int(round(cx - side / 2)), fov_w - side))
    crop_y = max(0, min(int(round(cy - side / 2)), fov_h - side))
    vf = f"crop={side}:{side}:{crop_x}:{crop_y},scale={SMART_SNAPSHOT_SIZE}:{SMART_SNAPSHOT_SIZE}"
    try:
        out = subprocess.run(
            [FFMPEG_BIN, "-loglevel", "error", "-i", src_path, "-vf", vf,
             "-frames:v", "1", "-f", "image2pipe", "-vcodec", "mjpeg", "-"],
            capture_output=True, timeout=15)
    except (OSError, subprocess.SubprocessError) as e:
        log.warning("snapshot crop failed for %s (coord=%s): %r", src_path, coord, e)
        return None
    if out.returncode != 0 or not out.stdout:
        log.warning("snapshot crop produced no image for %s (coord=%s) rc=%s: %s",
                    src_path, coord, out.returncode, out.stderr.decode(errors="replace")[-300:])
        return None
    return out.stdout


def _wait_for_frame(path, timeout=2.0):
    """Wait briefly for the snapshot JPEG; a missing thumbnail leaves the
    event's detectedAreas empty."""
    deadline = time.monotonic() + timeout
    while not os.path.isfile(path):
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.2)
    return True


def _upload_snapshot(upload_uri, device_info, requested_filename, what=None,
                     requested_device_id=None):
    if not upload_uri:
        log.warning("snapshot GetRequest had no uri, nothing to upload")
        return
    # Resolve filename -> camera and read that camera's frame (never guess from directory contents).
    record, resolved_by = _resolve_snapshot_device(requested_filename, what,
                                                   requested_device_id)
    if record is None:
        return
    device_id = record["device_id"]
    if resolved_by not in ("filename",):
        log.info("snapshot upload for filename=%r (what=%r, deviceID=%r) resolved to "
                  "deviceID=%s via %s", requested_filename, what, requested_device_id,
                  device_id, resolved_by)
    path = os.path.join(_STREAM_DIR, f"{device_id}.jpg")
    best_path = None
    if record.get("jpeg"):
        # The track's best-scoring frame, kept at detection time.
        data = record["jpeg"]
        if record["kind"] == "object" and record["coord"]:
            fd, best_path = tempfile.mkstemp(suffix=".jpg", dir=_tmp_dir())
            with os.fdopen(fd, "wb") as f:
                f.write(data)
    else:
        if not _wait_for_frame(path):
            log.warning("snapshot upload requested for deviceID=%s (filename=%s) but no captured "
                        "frame available yet at %s", device_id, requested_filename, path)
            return
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError as e:
            log.warning("failed to read %s for snapshot upload: %s", path, e)
            return
        if len(data) < 4 or data[:2] != b"\xff\xd8" or b"\xff\xd9" not in data[2:]:
            log.warning("snapshot for deviceID=%s is a torn/incomplete JPEG (%d bytes); "
                        "not uploading", device_id, len(data))
            return
    if record["kind"] == "object" and record["coord"]:
        fov_w, fov_h = _stream_dims.get(device_id, (DETECT_IMG_SIZE, DETECT_IMG_SIZE))
        try:
            cropped = _crop_snapshot_jpeg(best_path or path, record["coord"], fov_w, fov_h)
        finally:
            if best_path:
                os.remove(best_path)
        if cropped is not None:
            log.info("snapshot crop for deviceID=%s coord=%s -> %d bytes (from %d-byte full frame)",
                      device_id, record["coord"], len(cropped), len(data))
            data = cropped
        else:
            log.warning("snapshot crop failed for deviceID=%s, uploading full frame instead",
                        device_id)
    headers = {
        "x-ident": device_info["mac_nosep"].upper(),
        "x-type": device_info["type"],
        "x-sysid": device_info["sysid_hex"],
    }
    files_form = {"payload": ("snapshot.jpg", data, "image/jpeg")}
    try:
        resp = requests.post(upload_uri, headers=headers, files=files_form,
                              cert=(CERT_PATH, KEY_PATH), verify=False, timeout=15)
        log.info("uploaded snapshot (%d bytes) from %s to %s -> %s: %s",
                  len(data), path, upload_uri, resp.status_code, resp.text[:300])
    except requests.RequestException as e:
        log.warning("snapshot upload to %s failed: %r", upload_uri, e)


def _describe_face_db(path):
    """One-line summary of a downloaded face DB. Protect 7.3 sends a tar of
    known/uiface.json (subject/name maps) + known/uiface.db (binary store): list
    members with sizes, JSON keys with entry counts, and non-zero bytes per binary.
    SQLite gets each table's row count and columns (blob length = embedding size)."""
    import tarfile
    try:
        is_tar = tarfile.is_tarfile(path)
    except Exception:  # e.g. a truncated gzip raises instead of returning False
        is_tar = False
    if is_tar:
        parts = []
        with tarfile.open(path) as tar:
            for m in tar.getmembers():
                if not m.isfile():
                    continue
                data = tar.extractfile(m).read()
                desc = f"{m.name} ({m.size} bytes"
                try:
                    doc = json.loads(data)
                    desc += "; " + ", ".join(f"{k}={len(v) if isinstance(v, (list, dict)) else v!r}"
                                             for k, v in doc.items())
                except ValueError:
                    desc += f"; {sum(1 for x in data if x)} non-zero bytes"
                parts.append(desc + ")")
        return "tar: " + "; ".join(parts)
    with open(path, "rb") as f:
        head = f.read(16)
    if not head.startswith(b"SQLite format 3"):
        kind = {b"\x1f\x8b": "gzip", b"PK": "zip", b"{": "json", b"[": "json"}
        return next((k for m, k in kind.items() if head.startswith(m)), f"unknown ({head!r})")
    import sqlite3
    parts = []
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        for (table,) in con.execute("SELECT name FROM sqlite_master WHERE type='table'"):
            cols = [r[1] for r in con.execute(f'PRAGMA table_info("{table}")')]
            rows = con.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            sample = con.execute(f'SELECT * FROM "{table}" LIMIT 1').fetchone() or ()
            blobs = {c: len(v) for c, v in zip(cols, sample) if isinstance(v, bytes)}
            parts.append(f"{table}({rows} rows; cols={cols}; blob bytes={blobs})")
    finally:
        con.close()
    return "sqlite: " + "; ".join(parts)


def _download_face_db(uri, device_info):
    """Fetch the known-faces DB Protect offers (UpdateFaceDBRequest) into
    <state-dir>/facedb/ and log what it contains."""
    if not uri:
        log.warning("UpdateFaceDBRequest had no uri")
        return
    headers = {"x-ident": device_info["mac_nosep"].upper(), "x-type": device_info["type"],
               "x-sysid": device_info["sysid_hex"]}
    try:
        resp = requests.get(uri, headers=headers, cert=(CERT_PATH, KEY_PATH), verify=False,
                            timeout=30)
    except requests.RequestException as e:
        log.warning("face DB download from %s failed: %r", uri, e)
        return
    if resp.status_code != 200:
        log.info("face DB download from %s -> %s: %s", uri, resp.status_code, resp.text[:200])
        return
    out_dir = os.path.join(STATE_DIR or HERE, "facedb")
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "known.db")
    with open(path + ".tmp", "wb") as f:
        f.write(resp.content)
    os.replace(path + ".tmp", path)
    try:
        summary = _describe_face_db(path)
    except Exception as e:  # unknown format: the raw file is still saved
        summary = f"unreadable ({e!r})"
    log.info("face DB saved to %s (%d bytes, content-type=%s): %s", path, len(resp.content),
             resp.headers.get("content-type"), summary)
    _set_face_db(facedb.load(path))


# Protect's known faces (docs/protect-face-matching.md): loaded from the saved download at
# first use and again whenever Protect sends a new one. A face names a subject once it has
# matched on FACE_MATCH_HITS reads of its track, as the firmware's identity candidates do.
# Similarity needed against the DB: stricter than SFace's 0.363, which 7 of 322 distinct faces
# passed against Ian on a day he wasn't there (top 0.429). AIPORT_FACE_DB_THRESH overrides it.
FACE_MATCH_HITS = 2
FACE_DB_THRESH = float(os.environ.get("AIPORT_FACE_DB_THRESH") or 0.45)
# Smallest face read (shorter side, snapshot px) whose embedding is sent to Protect.
FACE_EMBED_MIN_PX = int(os.environ.get("AIPORT_FACE_EMBED_MIN_PX") or 60)
_face_db = None
_face_db_tried = False


def _set_face_db(db):
    global _face_db, _face_db_tried
    _face_db, _face_db_tried = db, True


def _get_face_db():
    if not _face_db_tried:
        _set_face_db(facedb.load(os.path.join(STATE_DIR or HERE, "facedb", "known.db")))
    return _face_db


def _parse_box_delay(spec, name="AIPORT_BOX_DELAY_MS", default=0):
    """A per-camera integer setting such as AIPORT_BOX_DELAY_MS: "<default>[,<deviceID>=<value>
    ...]" -> (default, {deviceID: value})."""
    per = {}
    for part in (spec or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            if "=" in part:
                dev, ms = part.split("=", 1)
                per[dev.strip().upper()] = int(ms)
            else:
                default = int(part)
        except ValueError:
            log.warning("ignoring bad %s entry %r", name, part)
    return default, per


# Protect places boxes in playback by clockWall. Event times come from when the analysed
# frame left ffmpeg, so the tick wait and inference no longer delay them; what's left is
# Protect's relay and our decode. This per-camera delay back-dates every frame-describing
# time by that remainder (default 0 = unchanged).
_BOX_DELAY_DEFAULT_MS, _BOX_DELAY_MS = _parse_box_delay(os.environ.get("AIPORT_BOX_DELAY_MS"))


def _box_delay_ms(device_id):
    return _BOX_DELAY_MS.get(str(device_id).upper(), _BOX_DELAY_DEFAULT_MS)


def _now_ms():
    """(wall ms, monotonic ms) right now."""
    return int(time.time() * 1000), int(time.monotonic() * 1000)


# deviceID -> (wall ms, monotonic ms) a back-filled message describes (_at_frame).
_frame_time_override = {}


def _frame_time(device_id):
    """(wall ms, monotonic ms) when the analysed frame was read from ffmpeg; now if no
    frame has been analysed yet."""
    override = _frame_time_override.get(device_id)
    if override is not None:
        return override
    with _frame_lock:
        stamp = _analysed_frame_time.get(device_id)
    return stamp or _now_ms()


@contextlib.contextmanager
def _at_frame(device_id, stamp):
    """Messages sent inside describe the frame read at stamp (wall ms, monotonic ms)."""
    _frame_time_override[device_id] = stamp
    try:
        yield
    finally:
        _frame_time_override.pop(device_id, None)


def _frame_wall_ms(device_id):
    """Wall-clock ms of the analysed frame, minus the camera's residual delay."""
    return _frame_time(device_id)[0] - _box_delay_ms(device_id)


# Experiment (docs/camera-clock-experiment.md): for the cameras in AIPORT_CAMERA_CLOCK
# (deviceIDs), clockMonotonic/clockStream are the camera's own clock from its RTCP sender
# reports; AIPORT_CLOCK_WALL_SKEW_MS ("<ms>[,<deviceID>=<ms>...]") shifts clockWall, to see
# which clock Protect places boxes by.
CAMERA_CLOCK_DEVICES = {d.strip().upper() for d in
                        os.environ.get("AIPORT_CAMERA_CLOCK", "").split(",") if d.strip()}
_WALL_SKEW_DEFAULT_MS, _WALL_SKEW_MS = _parse_box_delay(
    os.environ.get("AIPORT_CLOCK_WALL_SKEW_MS"), "AIPORT_CLOCK_WALL_SKEW_MS")
_camera_clocks = {}  # deviceID -> camera_clock.Reader (kept across stream restarts)


def _start_camera_clock(device_id, url):
    if str(device_id).upper() not in CAMERA_CLOCK_DEVICES:
        return
    reader = _camera_clocks.get(device_id)
    if reader is not None and reader.url == url:
        return
    if reader is not None:
        reader.stop()
    import camera_clock
    _camera_clocks[device_id] = camera_clock.Reader(device_id, url).start()
    log.info("camera clock experiment on for deviceID=%s (wall skew %d ms)", device_id,
             _clock_wall_skew_ms(device_id))


def _clock_wall_skew_ms(device_id):
    return _WALL_SKEW_MS.get(str(device_id).upper(), _WALL_SKEW_DEFAULT_MS)


def _clock_wall(device_id, now_ms):
    """clockWall for a message describing now_ms (AIPORT_CLOCK_WALL_SKEW_MS added)."""
    return now_ms + _clock_wall_skew_ms(device_id)


def _stream_clocks(device_id):
    """(clockMonotonic, clockStream) in ms for a device: the device's monotonic
    clock (ms since this process started the stream) and the media-timeline
    position. Real cameras report the two within tens of ms of each other
    (confirmed from a native camera's EventSmartDetect: clockStream 69938862 vs
    clockMonotonic 69964728, rate 1000 = ms). The console joins tracks to the
    media timeline with these, so zeros -- what we used to send -- leave the
    console unable to place a track (no live overlay).
    """
    reader = _camera_clocks.get(device_id)
    offset = reader.offset_ms() if reader is not None else None
    if offset is not None:  # the camera's own clock (AIPORT_CAMERA_CLOCK)
        camera_ms = int(round(_frame_time(device_id)[0] - offset))
        return camera_ms, camera_ms
    started = _stream_started_monotonic_ms.get(device_id)
    if started is None:
        return 0, 0
    mono = max(0, _frame_time(device_id)[1] - started - _box_delay_ms(device_id))
    return mono, mono


def _leave_track_attrs(t):
    """trackerIDAttrMap entry for a track in a single-tracker leave. Protect's search reads a
    vehicle's type, colour and plate from here, not from its descriptor: live 10:32 a two-truck
    event (multi leave, attributes here) was found by "truck" and "black", while one-vehicle
    events with the same attributes only on the descriptor were not."""
    extra = t["descriptor_extra"] or {}
    entry = {"objectType": t["object_type"],
             "zone": _track_zones(t.get("zones_seen") or t.get("zones"))}
    if t["object_type"] in ("face", "person"):
        entry.update({k: extra[k] for k in ("matchedId", "matchedName")
                      if extra.get(k) is not None})
    if t["object_type"] == "vehicle":
        attrs = extra.get("attributes") or {}
        entry.update({k: attrs[k] for k in ("vehicleType", "color") if k in attrs})
        if extra.get("name"):
            entry["matchedName"] = extra["name"]   # the plate, as _tracker_attrs sends it
    return entry


def _send_smart_detect_event(ws, device_id, edge_type="none", event_id=1, stationary=False,
                              object_type="person", coord=None, confidence=95,
                              first_shown_ms=None, tracker_id=1, descriptor_extra=None, best=None,
                              also_leaving=None, still=False, idle_since_ms=None, zones=None,
                              zones_seen=None):
    # Real EventSmartDetect shape; deviceID is our addition (required by AI-Port event routing).
    # also_leaving: further tracks closing on the same tick (dicts with this function's
    # per-track arguments). Real devices batch them into one leave with one snapshot
    # entry per tracker; one leave per track got only the first track's images fetched.
    now_ms = _frame_wall_ms(device_id)
    clock_mono, clock_stream = _stream_clocks(device_id)
    tracks = [{"tracker_id": tracker_id, "object_type": object_type, "coord": coord,
               "confidence": confidence, "first_shown_ms": first_shown_ms,
               "descriptor_extra": descriptor_extra, "best": best, "still": still,
               "idle_since_ms": idle_since_ms, "zones": zones, "zones_seen": zones_seen},
              *(also_leaving or [])]
    for t in tracks:
        # Placeholder box is only a fallback; tracks open on real detections.
        t["real_coord"] = ([int(round(v)) for v in _to_protect(device_id, t["coord"])]
                           if t["coord"] is not None else [100, 100, 200, 200])
        # firstShownTimeMs must stay at the track's first-seen time (recomputing looks like a new track).
        t["first_shown"] = t["first_shown_ms"] if t["first_shown_ms"] is not None else now_ms
        c = t["confidence"]
        t["level"] = int(round(c * 100)) if c <= 1 else int(round(c))
    confidence_level = tracks[0]["level"]
    # zonesStatus mirrors edgeType with a non-zero level; an empty one blocks add_smart_detect_types().
    zone_status = "none" if edge_type == "none" else edge_type
    zone_level = 0 if edge_type == "none" else confidence_level
    payload = {
        "deviceID": device_id,
        "clockMonotonic": clock_mono,
        "clockStream": clock_stream,
        "clockStreamRate": 1000,
        "clockWall": _clock_wall(device_id, now_ms),
        "descriptors": [{
            "attributes": None,
            "boxColor": "cyan",
            "confidenceLevel": t["level"],
            "coord": t["real_coord"],
            "coord3d": [-1, -1],
            "firstShownTimeMs": t["first_shown"],
            # Real capture had idleSinceTimeMs == firstShownTimeMs; a still track reports
            # when it stopped moving (Protect's idle tracker reads it with stationary).
            "idleSinceTimeMs": t.get("idle_since_ms") or t["first_shown"],
            "intelligenceZones": [],
            "lines": [],
            "loiterZones": [],
            "name": "",
            "objectType": t["object_type"],
            "secondLensZones": [],
            "stationary": stationary or bool(t.get("still")),
            "tag": "",
            "trackerID": t["tracker_id"],
            # Naming the zone makes ds create a smartDetectZone (vs smartDetectLine) event; Protect
            # labels the event with these zone ids.
            "zones": _track_zones(t.get("zones")),
            # Enrichment fields: attributes, plate text in name, faceEmbed, name/tag (enrich.py).
            **(t["descriptor_extra"] or {}),
        } for t in tracks],
        "displayTimeoutMSec": BOX_DISPLAY_MS,
        "edgeType": edge_type,
        "eventId": event_id,
        # Top-level objectTypes; likely what add_smart_detect_types() reads.
        "objectTypes": list(dict.fromkeys(t["object_type"] for t in tracks if t["object_type"])),
        "smartDetectSnapshotFullFoV": "",
        "smartDetectSnapshotFullFoVHeight": 0,
        "smartDetectSnapshotFullFoVWidth": 0,
        "smartDetectSnapshots": [],
        # A leave names every zone each track was in (_zones_visited); other edges, where it is now.
        "zonesStatus": _zones_status([(t.get("zones_seen") if edge_type == "leave" else None)
                                      or t.get("zones") for t in tracks],
                                     zone_level, zone_status),
    }
    # Other *Status fields omitted: the real capture had only zonesStatus, and those detectors aren't implemented.
    if edge_type == "leave":
        # "leave" carries the final classification in trackerIDAttrMap (likely read by add_smart_detect_types()).
        payload["trackerIDAttrMap"] = {str(t["tracker_id"]): _leave_track_attrs(t) for t in tracks}
        # Full-FoV dims use the real stream resolution, not the square detection buffer.
        seq = next(_snapshot_seq)
        fov_w, fov_h = _stream_dims.get(device_id, (DETECT_IMG_SIZE, DETECT_IMG_SIZE))
        fullfov_filename = f"smartdetectsnap_zone_{seq:08d}_fullfov.jpg"
        # One Full-FoV per message, from the first track's best-scoring moment if kept.
        first_best = tracks[0]["best"]
        _record_snapshot_filename(fullfov_filename, device_id, kind="fullfov",
                                  jpeg=first_best["jpeg"] if first_best else None)
        payload["smartDetectSnapshotFullFoV"] = fullfov_filename
        payload["smartDetectSnapshotFullFoVHeight"] = fov_h
        payload["smartDetectSnapshotFullFoVWidth"] = fov_w
        for t in tracks:
            b = t["best"]
            # Thumbnails come from the track's best-scoring moment when we kept one.
            # Crop in image space; report the box in Protect's canvas space.
            crop_coord = list(b["coord"]) if b else (list(t["coord"]) if t["coord"] is not None else None)
            snap_coord = ([int(round(v)) for v in _to_protect(device_id, b["coord"])]
                          if b else t["real_coord"])
            best_wall = b["wall"] if b else t["first_shown"]
            snapshot_filename = f"smartdetectsnap_zone_{t['tracker_id']}{best_wall}.jpg"
            _record_snapshot_filename(snapshot_filename, device_id, kind="object", coord=crop_coord,
                                      jpeg=b["jpeg"] if b else None)
            payload["smartDetectSnapshots"].append({
                "clockBestMonotonic": 0,
                "clockBestWall": best_wall,
                "confidenceLevel": t["level"],
                "coord": snap_coord,
                "framingRect": snap_coord,
                "reVerifyEligible": False,
                "smartDetectSnapshot": snapshot_filename,
                # Matches the object-crop image, not the full frame.
                "smartDetectSnapshotHeight": SMART_SNAPSHOT_SIZE,
                "smartDetectSnapshotName": "",
                "smartDetectSnapshotType": t["object_type"],
                "smartDetectSnapshotWidth": SMART_SNAPSHOT_SIZE,
                "trackerID": t["tracker_id"],
            })
    send_msg(ws, "EventSmartDetect", payload, response_expected=False)


def _send_line_detect_event(ws, device_id, line, edge_type, event_id, direction,
                            a2b, b2a, object_type, coord, confidence,
                            first_shown_ms, tracker_id):
    """Line Crossing event: same envelope as the zone path but with
    linesStatus, so the controller selects smartDetectLine."""
    now_ms = _frame_wall_ms(device_id)
    clock_mono, clock_stream = _stream_clocks(device_id)
    real_coord = ([int(round(v)) for v in _to_protect(device_id, coord)]
                  if coord is not None else [100, 100, 200, 200])
    first_shown = first_shown_ms if first_shown_ms is not None else now_ms
    confidence_level = int(round(confidence * 100)) if confidence <= 1 else int(round(confidence))
    line_ref = _line_ref(line["id"])
    stationary = edge_type == "leave"
    zone_status = "none" if edge_type == "none" else edge_type
    zone_level = 0 if edge_type == "none" else confidence_level
    payload = {
        "deviceID": device_id,
        "clockMonotonic": clock_mono,
        "clockStream": clock_stream,
        "clockStreamRate": 1000,
        "clockWall": _clock_wall(device_id, now_ms),
        "descriptors": [{
            "attributes": None,
            "boxColor": "cyan",
            "confidenceLevel": confidence_level,
            "coord": real_coord,
            "coord3d": [-1, -1],
            "firstShownTimeMs": first_shown,
            "idleSinceTimeMs": first_shown,
            "intelligenceZones": [],
            "lines": [line_ref],
            "loiterZones": [],
            "name": "",
            "objectType": object_type,
            "secondLensZones": [],
            "stationary": stationary,
            "tag": "",
            "trackerID": tracker_id,
            "zones": [],
            # Cumulative crossings; the real descriptor also carries per-crossing Add deltas.
            "crosslineA2B": a2b,
            "crosslineB2A": b2a,
            "crosslineA2BAdd": 1 if direction == "A2B" else 0,
            "crosslineB2AAdd": 1 if direction == "B2A" else 0,
        }],
        "displayTimeoutMSec": BOX_DISPLAY_MS,
        "edgeType": edge_type,
        "eventId": event_id,
        "objectTypes": [object_type] if object_type else [],
        "smartDetectSnapshotFullFoV": "",
        "smartDetectSnapshotFullFoVHeight": 0,
        "smartDetectSnapshotFullFoVWidth": 0,
        "smartDetectSnapshots": [],
        "linesStatus": {str(line_ref): {
            "level": zone_level,
            "status": zone_status,
            "direction": direction,
            "crosslineA2B": a2b,
            "crosslineB2A": b2a,
        }},
    }
    if edge_type == "leave":
        # Line event closes with trackerIDAttrMap naming the line (zone path uses "zone").
        payload["trackerIDAttrMap"] = {
            str(tracker_id): {"objectType": object_type, "line": [line_ref]},
        }
        seq = next(_snapshot_seq)
        fov_w, fov_h = _stream_dims.get(device_id, (DETECT_IMG_SIZE, DETECT_IMG_SIZE))
        fullfov_filename = f"smartdetectsnap_line_{seq:08d}_fullfov.jpg"
        snapshot_filename = f"smartdetectsnap_line_{tracker_id}{now_ms}.jpg"
        _record_snapshot_filename(fullfov_filename, device_id, kind="fullfov")
        # Crop in image space; the payload's coord/framingRect stay in canvas space.
        _record_snapshot_filename(snapshot_filename, device_id, kind="object",
                                  coord=list(coord) if coord is not None else real_coord)
        payload["smartDetectSnapshotFullFoV"] = fullfov_filename
        payload["smartDetectSnapshotFullFoVHeight"] = fov_h
        payload["smartDetectSnapshotFullFoVWidth"] = fov_w
        payload["smartDetectSnapshots"] = [{
            "clockBestMonotonic": 0,
            "clockBestWall": first_shown,
            "confidenceLevel": confidence_level,
            "coord": real_coord,
            "framingRect": real_coord,
            "reVerifyEligible": False,
            "smartDetectSnapshot": snapshot_filename,
            "smartDetectSnapshotHeight": SMART_SNAPSHOT_SIZE,
            "smartDetectSnapshotName": "",
            "smartDetectSnapshotType": object_type,
            "smartDetectSnapshotWidth": SMART_SNAPSHOT_SIZE,
            "trackerID": tracker_id,
        }]
    send_msg(ws, "EventSmartDetect", payload, response_expected=False)


# Dedupe the idempotent status event: a unchanged flood of EventAIPortStatus is
# pointless, and a G3 camera's "Service Unavailable" reply loops.
# EventFeatureFlagsUpdated is deliberately NOT deduped across stream starts: it is
# what makes Protect re-apply smart-detect capability to the paired camera, and
# the console can lose that (re-pair, DB restore) without telling us.
_last_status_sent = {}


# EventAIPortStatus follows each paired camera's health: isStreaming once its stream has
# delivered no frame for STATUS_STALE_S, isSmartDetectReady once its loop has analysed none
# for as long (a crash or hang: live 12:39 every camera's loop had crashed while Protect was
# still told all was well); back to true when it recovers. Checked every STATUS_CHECK_S, sent
# only on a change, with STATUS_STALE_S of grace after Protect starts a stream.
STATUS_STALE_S = 30.0
STATUS_CHECK_S = 5.0
_status_ws = None            # the live connection to Protect
_stream_started_at = {}      # deviceID -> monotonic s when UiStreamControl started its stream
_last_analysed = {}          # deviceID -> monotonic s of its loop's last pass
_status_thread = None


def _camera_health(device_id, now):
    """(streaming, smart_ready) for a paired camera at monotonic time now."""
    started = _stream_started_at.get(device_id, now)
    with _frame_lock:
        stamp = _latest_frame_time.get(device_id)
    last_frame = max(stamp[1] / 1000.0 if stamp else started, started,
                     _last_frame_at.get(device_id, started))
    last_pass = max(_last_analysed.get(device_id, started), started)
    loop = _motion_poll_thread.get(device_id)
    streaming = now - last_frame < STATUS_STALE_S
    ready = now - last_pass < STATUS_STALE_S and (loop is None or loop.is_alive())
    return streaming, ready


# A pull that has delivered no frame for this long is killed, and its reader restarts it as
# it would after any unexpected exit: the read timeout misses a pull stuck past the socket
# (decoding or the filter graph).
STREAM_STALL_S = float(os.environ.get("AIPORT_STREAM_STALL_S") or 30)


def _restart_stalled_pulls(now_ms=None):
    """Kill each running pull with no frame for STREAM_STALL_S since its last frame or its
    start; returns the deviceIDs killed."""
    now_ms = int(time.monotonic() * 1000) if now_ms is None else now_ms
    killed = []
    for device_id, proc in list(_active_streams.items()):
        if proc is None or proc.poll() is not None:
            continue   # not started, or exited and about to be restarted
        started = _stream_started_monotonic_ms.get(device_id)
        if started is None:
            continue
        with _frame_lock:
            stamp = _latest_frame_time.get(device_id)
        last = max(stamp[1] if stamp else started, started)
        if now_ms - last < STREAM_STALL_S * 1000:
            continue
        log.warning("deviceID=%s RTSP pull delivered no frame for %.0f s, restarting it (pid=%s)",
                    device_id, (now_ms - last) / 1000, proc.pid)
        proc.kill()
        killed.append(device_id)
    return killed


def _check_status(ws, now=None):
    """Send EventAIPortStatus for each paired camera whose health changed."""
    now = time.monotonic() if now is None else now
    for device_id in list(_stream_points):
        streaming, ready = _camera_health(device_id, now)
        state = (True, streaming, ready, True)
        prev = _last_status_sent.get(device_id)
        if prev == state:
            continue
        if prev is not None:
            (log.warning if not (streaming and ready) else log.info)(
                "camera deviceID=%s now %s, %s", device_id,
                "streaming" if streaming else "not streaming (no frame for %.0f s)" % STATUS_STALE_S,
                "detecting" if ready else "not detecting (no pass for %.0f s)" % STATUS_STALE_S)
        _send_status_event(ws, device_id, plug=True, streaming=streaming, smart_ready=ready,
                           audio_ready=True)


def _start_status_watch():
    global _status_thread
    if _status_thread is not None and _status_thread.is_alive():
        return

    def watch():
        while True:
            time.sleep(STATUS_CHECK_S)
            try:
                _restart_stalled_pulls()
            except Exception:
                log.exception("stalled pull check failed")
            ws = _status_ws
            if ws is None:
                continue
            try:
                _check_status(ws)
            except Exception:
                log.exception("camera status check failed")

    _status_thread = threading.Thread(target=watch, name="camera-status", daemon=True)
    _status_thread.start()


def _send_status_event(ws, device_id, plug, streaming, smart_ready, audio_ready):
    # Field names from the real ubnt_av_aiport string table; isSmartDetectReady gates AI-task dispatch.
    state = (bool(plug), bool(streaming), bool(smart_ready), bool(audio_ready))
    if _last_status_sent.get(device_id) == state:
        log.info("EventAIPortStatus unchanged for deviceID=%s %s -- not resending",
                  device_id, state)
        return
    _last_status_sent[device_id] = state
    send_msg(ws, "EventAIPortStatus", {
        "deviceID": device_id,
        "isPlug": state[0],
        "isStreaming": state[1],
        "isSmartDetectReady": state[2],
        "isAudioEventReady": state[3],
    }, response_expected=False)


def _send_feature_flags_event(ws, device_id):
    # Must mirror the AI Port's real key names/values: the controller WHOLESALE
    # replaces the paired camera's flags, so an omitted key wipes the real value.
    # Sent on every stream start: Protect re-applies ChangeSmartMotionSettings /
    # ChangeSmartDetectSettings for the camera on receipt (service.log), which is
    # how the camera row gets smartDetectTypes and the UI its Detection Zone.
    # A once-per-connection guard left a re-paired camera motion-only until the
    # next reconnect (observed on the lab UNVR, 2026-10-26).
    send_msg(ws, "EventFeatureFlagsUpdated", {
        "deviceID": device_id,
        "smartDetect": _smart_detect_features(),
        # Real key names + genuine hardware values (echoed, not owned).
        "motionDetect": ["enhanced"],
        "privacyMask": True,
        "privacyMasks": {"maxZones": 16, "rectangleOnly": False},
        "squareEventThumbnail": True,
        "mic": True,
        "speaker": True,
        "ledStatus": True,
    }, response_expected=False)


def _smart_detect_features():
    # Plate/face types and the *EnhancedByAiKey flags (seen on a real doorbell's
    # hello) are only advertised when enrich.py can actually produce them.
    return ["person", "vehicle", "animal", "lineCrossing", "liveviewTracking",
            *enrich.smart_detect_features()]


# Capacity points per stream, measured from the real AI Port's ubnt_onvifd
# (GetUiStreamPoints, firmware 5.1.12): tiered on width x height x fps, the same
# thresholds Protect uses for its own estimate (it divides usedPoints by 10 and
# allows 1.0 in total). Orientation doesn't matter.
STREAM_POINT_TIERS = ((62208000, 2), (121927680, 2.5), (248832000, 5))
STREAM_POINTS_MAX = 10


def _parse_points_scale(spec):
    """AIPORT_POINTS_SCALE: a factor > 0 on every stream's points (default 1)."""
    if not spec:
        return 1.0
    try:
        scale = float(spec)
    except ValueError:
        scale = 0
    if scale <= 0:
        log.warning("ignoring bad AIPORT_POINTS_SCALE %r", spec)
        return 1.0
    return scale


# Protect divides usedPoints by 10 and caps the total at the AI Port's maxCameraCapacity, which
# its controller sets by device type. AIPORT_POINTS_SCALE reports each stream at a fraction of
# the real device's points, so more cameras pair: 0.5 doubles the room. Protect still adds
# its own full estimate for the camera being paired, and may show capacity as "inconsistent".
POINTS_SCALE = _parse_points_scale(os.environ.get("AIPORT_POINTS_SCALE"))


def _points_value(points):
    """2.0 -> 2, 2.5 stays: the real device sends whole points as integers."""
    points = round(points, 1)
    return int(points) if points == int(points) else points


def _stream_points_for(width, height, fps):
    """Capacity points for one stream, or None if width/height/fps aren't positive numbers."""
    try:
        rate = int(width) * int(height) * int(fps)
    except (TypeError, ValueError):
        return None
    if rate <= 0:
        return None
    for limit, points in STREAM_POINT_TIERS:
        if rate <= limit:
            return points * POINTS_SCALE
    return STREAM_POINTS_MAX * POINTS_SCALE


def _used_points():
    return _points_value(sum(_stream_points.values()))


def _apply_motion_settings(payload):
    """Protect's motion zones and sensitivity (each zone's `level`, 0-100) for the motion
    gate; the detector is rebuilt on its next frame when they change."""
    device_id = payload.get("deviceID")
    zones = payload.get("zones")
    if not device_id or not isinstance(zones, dict):
        return
    levels = [z.get("level") for z in zones.values()
              if isinstance(z, dict) and isinstance(z.get("level"), (int, float))]
    settings = {"zones": zones, "level": max(levels) if levels else 50}
    if _motion_settings.get(device_id) != settings:
        _motion_settings[device_id] = settings
        _motion_detectors.pop(device_id, None)


def _stop_all_streams(preserve_tracks=False):
    for device_id in list(set(_active_streams) | set(_stream_points)):
        _stop_stream(device_id)
        _stop_motion_detector(device_id, preserve_tracks=preserve_tracks)
    _stream_points.clear()


def _version_from_update_uri(uri):
    """Target version out of the controller's update URL, or None.

    service.js `internalUpdatePath` builds
    `/internal/update/<platform>/<product>/<firmware|software>/<version>/<mac>`;
    the non-camera `getUpdateUrl` form is the same fields as a query string.
    """
    if not uri:
        return None
    try:
        parts = urlsplit(uri)
    except ValueError:
        return None
    segments = [s for s in parts.path.split("/") if s]
    if len(segments) >= 3 and segments[-3] in ("firmware", "software"):
        return segments[-2]
    return dict(parse_qsl(parts.query)).get("version") or None


def handle_function(ws, msg, device_info):
    fn = msg.get("functionName")
    payload = msg.get("payload") or {}
    msg_id = msg.get("messageId")
    log.info("<<< received functionName=%r messageId=%s from=%r to=%r payload=%s",
             fn, msg_id, msg.get("from"), msg.get("to"), payload)

    if fn == "UiStreamControl":
        device_id = payload.get("deviceID")
        streaming = bool(payload.get("streaming"))
        if streaming and payload.get("ip") and payload.get("port") and payload.get("uri"):
            try:
                # Fresh viewing session: reset the death-retry counter.
                _stream_restart_count.pop(device_id, None)
                _start_stream(device_id, payload["ip"], payload["port"], payload["uri"],
                              payload.get("width", DETECT_IMG_SIZE),
                              payload.get("height", DETECT_IMG_SIZE))
                # Preserve tracks across UiStreamControl re-issues, else an open
                # event is abandoned and prev_center resets (line crossing breaks).
                _start_motion_detector(ws, device_id, payload["ip"], payload["port"],
                                       payload["uri"], preserve_tracks=True)
                _stream_started_at[device_id] = time.monotonic()
                _start_status_watch()
                points = _stream_points_for(payload.get("width"), payload.get("height"),
                                            payload.get("fps"))
                # Protect always sends width/height/fps; without them, assume the lowest tier.
                _stream_points[device_id] = points if points is not None else STREAM_POINT_TIERS[0][1]
                reply_status = "started"
            except Exception:
                log.exception("failed to start RTSP pull for deviceID=%s", device_id)
                _stream_points.pop(device_id, None)
                reply_status = "stopped"
        else:
            if device_id:
                _stop_stream(device_id)
                _stop_motion_detector(device_id, ws=ws)
                _stream_points.pop(device_id, None)
            reply_status = "stopped"
        # usedPoints MUST be on every reply (start and stop): without it Protect sets
        # utilization to the maximum and re-pairing fails with estimated_capacity_exceeded.
        reply = {"status": reply_status, "usedPoints": _used_points()}
        send_msg(ws, "UiStreamControl", reply, in_response_to=msg_id)
        if device_id:
            # smart_ready is the likely gate for AI-task dispatch.
            _send_status_event(ws, device_id, plug=True,
                                streaming=(reply_status == "started"),
                                smart_ready=True, audio_ready=True)
            if reply_status == "started":
                _send_feature_flags_event(ws, device_id)
        return
    if fn == "OnvifStreamControl":
        # Assigning an ONVIF camera: we pull its RTSP for detection and push its
        # video to Protect (channels arrive via ChangeVideoSettings with this deviceID).
        device_id = payload.get("deviceID")
        url = payload.get("streamUrl")
        reply_status = "stopped"
        if device_id and payload.get("streaming") and url:
            try:
                _onvif.set_source(device_id, url, audio=payload.get("audio", True),
                                  width=payload.get("width"), height=payload.get("height"),
                                  fps=payload.get("fps"))
                _stream_restart_count.pop(device_id, None)
                _start_stream(device_id, None, None, None, payload.get("width"),
                              payload.get("height"), url=url)
                _start_motion_detector(ws, device_id, None, None, None, preserve_tracks=True)
                # Counted, health-watched and stall-checked like a UiStreamControl camera.
                _stream_points[device_id] = _stream_points_for(
                    payload.get("width"), payload.get("height"), payload.get("fps"))
                _stream_started_at[device_id] = time.monotonic()
                _start_status_watch()
                reply_status = "started"
            except Exception:
                log.exception("failed to start ONVIF camera deviceID=%s", device_id)
        elif device_id:
            # Stop, don't forget: Protect often re-issues only streaming:true
            # (not ChangeVideoSettings), and the destinations are still valid.
            _onvif.stop_device(device_id)
            _stop_stream(device_id)
            _stop_motion_detector(device_id, ws=ws)
            _stream_points.pop(device_id, None)
        send_msg(ws, "OnvifStreamControl", {"status": reply_status, "usedPoints": _used_points()},
                 in_response_to=msg_id)
        if device_id and reply_status == "started":
            _send_status_event(ws, device_id, plug=True, streaming=True,
                               smart_ready=True, audio_ready=True)
            _send_feature_flags_event(ws, device_id)
        return
    if fn == "GetStreamList":
        # Protect asks on every connect; a non-empty list makes it send ResetAIPortStreams
        # before re-issuing UiStreamControl for each paired camera.
        send_msg(ws, "GetStreamList", {"list": [
            {"deviceID": d, "points": _points_value(p)} for d, p in _stream_points.items()
        ]}, in_response_to=msg_id)
        return
    if fn == "GetUiStreamPoints":
        # Cost of each proposed stream; entries without a positive width/height/fps are skipped.
        result = []
        for item in payload.get("list") or []:
            if not isinstance(item, dict):
                continue
            points = _stream_points_for(item.get("width"), item.get("height"), item.get("fps"))
            if points is not None:
                result.append({"deviceID": item.get("deviceID"), "points": _points_value(points)})
        send_msg(ws, fn, {"list": result}, in_response_to=msg_id)
        return
    if fn == "ResetAIPortStreams":
        # The real device stops every stream. Keep track state: Protect re-issues
        # UiStreamControl right after, and open events should carry on.
        log.info("ResetAIPortStreams: stopping %d stream(s)", len(_stream_points))
        _stop_all_streams(preserve_tracks=True)
        if msg.get("responseExpected"):
            send_msg(ws, fn, {}, in_response_to=msg_id)
        return
    if fn == "ChangeVideoSettings":
        # With a deviceID it configures an ONVIF camera's channel pushes (streamName +
        # :7550 destination per videoN). Either way the reply is also read as a query:
        # the controller builds its channel list from `.video`/`.audio`, so `{}` blocks
        # provisioning.
        onvif_src = None
        if payload.get("deviceID") and isinstance(payload.get("video"), dict):
            _onvif.configure(payload["deviceID"], payload["video"])
            onvif_src = _onvif.source(payload["deviceID"])
        if onvif_src:
            # Every channel carries the camera's main stream, so report its real
            # geometry -- probed, since Protect echoes back whatever we answered
            # before. A wrong size breaks the web player (it sizes its decoder from it).
            probe = _stream_probe.get(payload["deviceID"]) or {}
            channel = {"enabled": True,
                       "width": probe.get("width") or onvif_src["width"] or 1920,
                       "height": probe.get("height") or onvif_src["height"] or 1080,
                       "fps": probe.get("fps") or onvif_src["fps"] or 15,
                       "bitrate": 8000000}
            send_msg(ws, "ChangeVideoSettings", {
                "video": {k: dict(channel) for k in payload["video"]},
                "audio": {"enabled": True, "volume": 100},
            }, in_response_to=msg_id)
            return
        send_msg(ws, "ChangeVideoSettings", {
            "video": {
                "video1": {"enabled": True, "width": 1920, "height": 1080,
                           "fps": 25, "bitrate": 4000000},
                "video2": {"enabled": False},
                "video3": {"enabled": False},
            },
            "audio": {"enabled": False, "volume": 100},
        }, in_response_to=msg_id)
        return
    if fn == "ChangeIspSettings":
        # Same query-not-push pattern as ChangeVideoSettings; minimal best-effort shape.
        send_msg(ws, "ChangeIspSettings", {
            "irLedMode": "auto",
            "wdr": 1,
        }, in_response_to=msg_id)
        return
    if fn == "UpdateUsernamePassword":
        # Password rotation request; just ack (not persisted) so the controller stops retrying.
        send_msg(ws, "UpdateUsernamePassword", {}, in_response_to=msg_id)
        return
    if fn == "GetRequest" and "snapshot" in str(payload.get("what", "")).lower():
        # Snapshot upload request (what may be snapshot/smartDetectZoneSnapshot/...FullFoV);
        # ack and upload in the background.
        pending = _pending_leaves.pop(payload.get("filename") or "", None)
        if pending is not None:
            pending["seen"] = True  # Protect accepted a multi-tracker leave
        send_msg(ws, "GetRequest", {}, in_response_to=msg_id)
        threading.Thread(target=_upload_snapshot,
                          args=(payload.get("uri"), device_info, payload.get("filename"),
                                payload.get("what"), payload.get("deviceID")),
                          daemon=True).start()
        return

    if fn == "ChangeSmartMotionSettings":
        # Real devices end a smart-detect event after eventMaxDurationMSec themselves.
        max_ms = payload.get("eventMaxDurationMSec")
        if payload.get("deviceID") and isinstance(max_ms, (int, float)) and max_ms > 0:
            _event_max_ms[payload["deviceID"]] = int(max_ms)
        _apply_motion_settings(payload)
        if msg.get("responseExpected"):
            send_msg(ws, fn, {}, in_response_to=msg_id)
        return

    if fn == "UpdateFaceDBRequest":
        # Protect pushes its known-faces DB (named face groups) for on-device matching.
        # Download and describe it; matching against it isn't implemented yet.
        if msg.get("responseExpected"):
            send_msg(ws, fn, {}, in_response_to=msg_id)
        threading.Thread(target=_download_face_db, args=(payload.get("uri"), device_info),
                         daemon=True).start()
        return

    if fn == "ChangeSmartDetectSettings":
        # Parses zones/excludeZones/lines (normalized 0-1000, arbitrary vertices);
        # per-zone sensitivity isn't applied.
        device_id = payload.get("deviceID")
        start_ms = payload.get("eventStartMSec")
        if device_id and isinstance(start_ms, (int, float)) and start_ms >= 0:
            _event_start_ms[device_id] = int(start_ms)
        if device_id:
            # Protect also sends partial updates ({"isLprCamera": 0}): keep what they omit.
            if "excludeZones" in payload:
                _exclude_zones[device_id] = _parse_zones(payload.get("excludeZones"))
            if "zones" in payload:
                _detect_zones[device_id] = _parse_zones(payload.get("zones"))
                _detect_zone_types[device_id] = _parse_zone_types(payload.get("zones"))
                _detect_zone_ids[device_id] = _parse_zone_ids(payload.get("zones"))
            if "lines" in payload:
                _lines[device_id] = _parse_lines(payload.get("lines"))
            log.info("ChangeSmartDetectSettings: deviceID=%s now has %d excludeZones "
                      "polygon(s), %d detection-zone polygon(s), %d Line Crossing segment(s)",
                      device_id, len(_exclude_zones.get(device_id, ())),
                      len(_detect_zones.get(device_id, ())), len(_lines.get(device_id, ())))
        if msg.get("responseExpected"):
            send_msg(ws, fn, {}, in_response_to=msg_id)
        return

    if fn == "ResetToDefaults":
        # Un-adopt signal: the controller doesn't close the WS afterward, so tear down here.
        log.info("ResetToDefaults received -- treating as un-adopt: clearing "
                  "adopt state and tearing down streams")
        _onvif.stop_all()
        _stop_all_streams()
        _exclude_zones.clear()
        _detect_zones.clear()
        _detect_zone_types.clear()
        _detect_zone_ids.clear()
        _lines.clear()
        _snapshot_filename_to_device.clear()
        _last_object_coord_by_device.clear()
        try:
            for name in os.listdir(_STREAM_DIR):
                if name.endswith(".jpg"):
                    try:
                        os.remove(os.path.join(_STREAM_DIR, name))
                    except OSError:
                        pass
        except OSError:
            pass
        _clear_adopt_state()
        if msg.get("responseExpected"):
            send_msg(ws, fn, {}, in_response_to=msg_id)
        log.info("closing this connection to force a fresh, honestly-unadopted reconnect")
        ws.close()
        return

    if fn == "UpdateFirmwareRequest":
        # Pin the version from uri and fake a reboot; never fetch/flash (the vendor
        # does the same via custom.skipFirmwareUpdate). Applied on the reconnect.
        uri = str(payload.get("uri") or "")
        target = _version_from_update_uri(uri)
        log.info("UpdateFirmwareRequest uri=%s timeoutMs=%s -> pinning version %s "
                  "(no flash)",
                  uri, payload.get("timeoutMs"), target or "<unparsed>")
        # Status the controller's handleEventUpdateFirmwareStatus consumes:
        # FW_DOWNLOADING then FW_UPDATING -> isDownloadingFW/isUpdating.
        send_msg(ws, "EventUpdateFirmwareStatus", {"status": "FW_DOWNLOADING"},
                 response_expected=False)
        send_msg(ws, "EventUpdateFirmwareStatus", {"status": "FW_UPDATING"},
                 response_expected=False)
        if msg.get("responseExpected"):
            send_msg(ws, fn, {}, in_response_to=msg_id)
        if target:
            config.pin_fw_version(STATE_DIR, target)
        else:
            log.warning("could not parse a version from update uri %r -- acking "
                          "without pinning", uri)
        # Let the status frames flush, then drop the socket: main()'s reconnect
        # loop re-reads the pin and reports the new version.
        time.sleep(1.0)
        log.info("simulated post-update reboot: closing avclient connection")
        ws.close()
        return

    if fn == "ChangeDeviceSettings":
        # Protect pushes the user's rename here; persist it (discovery is a separate
        # process) so the discovery TLV 0x0B reports the same name Network shows.
        name = payload.get("name")
        if isinstance(name, str) and name:
            try:
                config.set_device_name(STATE_DIR, name)
                log.info("ChangeDeviceSettings: device renamed to %r", name)
            except OSError:
                log.warning("failed to persist device name %r", name, exc_info=True)
        if msg.get("responseExpected"):
            send_msg(ws, fn, {"name": config.device_name(STATE_DIR, device_info.get("name")),
                              "timezone": payload.get("timezone") or ""},
                     in_response_to=msg_id)
        return

    # Unknown function: ack if a response is expected.
    if msg.get("responseExpected"):
        send_msg(ws, fn, {}, in_response_to=msg_id)


def _is_adopted():
    try:
        with open(ADOPT_STATE_FILE) as f:
            state = json.load(f)
        return bool(state.get("token")) or bool(state.get("hosts"))
    except (OSError, json.JSONDecodeError):
        return False


def _clear_adopt_state():
    # Overwrite (not delete) adopt_state.json so concurrent readers never hit ENOENT.
    try:
        config.atomic_write_json(ADOPT_STATE_FILE, {})
    except OSError:
        log.warning("failed to clear adopt state file %s", ADOPT_STATE_FILE, exc_info=True)


def _adopt_token():
    try:
        with open(ADOPT_STATE_FILE) as f:
            return json.load(f).get("token")
    except (OSError, json.JSONDecodeError):
        return None


# No wait-for-adopt gate: the unsolicited pre-adopt connection is load-bearing for candidate identity.


def run(host, port, device_info, token=None):
    # connectionHost must be the controller's (Protect builds camera-upload URLs
    # from it), not ours -- self-IP left paired cameras without thumbnails.
    device_info = {**device_info, "console_host": host}
    # New controller connection = fresh state: forget what this session already
    # reported so the controller gets one full status/capability snapshot.
    _last_status_sent.clear()
    url = f"wss://{host}:{port}/camera/1.0/ws"
    if token:
        url += f"?token={token}"
    # All headers are load-bearing: ds proxies the connection and closes incomplete sets.
    headers = {
        "camera-mac": device_info["mac_nosep"],
        "camera-ip": device_info["ip"],
        "camera-model": device_info["sysid_hex"],
        "camera-firmware": device_info["version"],
        "device-id": device_info["device_id"],
        "x-guid": device_info["guid"],
        "adopted": "true" if _is_adopted() else "false",
        "Origin": "http://ws_camera_proto_secure_transfer",
    }
    log.info("connecting to %s headers=%s", url, headers)
    ctx = build_ssl_context()
    with connect(url, subprotocols=["secure_transfer"], additional_headers=headers,
                 ssl=ctx, open_timeout=15) as ws:
        log.info("CLASSIC AVCLIENT WSS CONNECTED (subprotocol=%s)", ws.subprotocol)
        global _status_ws
        _status_ws = ws
        # hwrev must be non-null or hardwareRevision stays empty; 1 is a placeholder.
        now_ms = int(time.time() * 1000)
        send_msg(ws, "ubnt_avclient_hello", {
            "mac": device_info["mac_nosep"],
            "model": device_info["type"],
            "name": config.device_name(STATE_DIR, device_info.get("name")),
            "fwVersion": device_info["version"],
            "connectionHost": device_info.get("console_host") or device_info["ip"],
            "connectionSecurePort": port,
            "protocolVersion": 67,
            "adoptionCode": token or "",
            "ip": device_info["ip"],
            "hwrev": 1,
            "idleTime": 0,
            "rebootTimeoutSec": 30,
            "upgradeTimeoutSec": 150,
            "semver": f"v{device_info['version']}",
            "totalLoad": 0.1,
            "uptime": (now_ms - _PROCESS_START_MS) // 1000,
            "features": {
                "smartDetect": _smart_detect_features(),
                "motionDetect": ["enhanced"],
                "privacyMask": True,
                "privacyMasks": {"maxZones": 16, "rectangleOnly": False},
                "squareEventThumbnail": True,
                "mic": True,
                "speaker": True,
                "ledStatus": True,
            },
        }, response_expected=True)

        for raw in ws:
            # Controller sends JSON as BINARY frames; decode bytes as UTF-8 JSON.
            text = raw.decode() if isinstance(raw, (bytes, bytearray)) else raw
            try:
                msg = json.loads(text)
            except json.JSONDecodeError:
                log.warning("non-JSON frame (%d bytes): %r", len(text), text[:200])
                continue
            if msg.get("inResponseTo"):
                log.info("<<< response to our msg id=%s payload=%s",
                          msg.get("inResponseTo"), msg.get("payload"))
                continue
            handle_function(ws, msg, device_info)


def _init_detector(cfg):
    """Load the configured detector up front so ffmpeg is started at its input
    size. A failure is logged, not fatal: the device still pairs and relays
    snapshots, and classification retries the load each tick."""
    global DETECTOR_BACKEND, DETECTOR_MODEL
    # AIPORT_BACKEND is the pre-modular name (auto|rknn|onnx); keep it working.
    DETECTOR_BACKEND = (os.environ.get("AIPORT_DETECTOR")
                        or os.environ.get("AIPORT_BACKEND")
                        or cfg.get("detector") or "auto")
    DETECTOR_MODEL = os.environ.get("AIPORT_MODEL_PATH") or cfg.get("detector_model") or None
    if DETECTOR_BACKEND.strip().lower() == "none":
        DETECTOR_BACKEND = "none"
        log.info("detector backend 'none': object detection disabled")
        return
    try:
        _get_detector()
    except Exception:
        log.exception("detector backend %r failed to load; detection disabled until it does",
                      DETECTOR_BACKEND)


def main():
    cfg = config.load_config_and_logging(sys.argv[1:])
    ap = argparse.ArgumentParser(description=__doc__)
    config.add_common_flags(ap)
    ap.add_argument("--iface", default=None,
                     help="interface to derive identity from (never bound); defaults to cfg "
                          "[network] iface, else the default-route interface (auto-detected)")
    ap.add_argument("--mac", default=None)
    ap.add_argument("--fallback-port", type=int, default=int(cfg["port"]),
                     help="port to use when there's no real adopt state yet (dialing --host) "
                          "or when adopt_state.json's hosts[0] entry doesn't include one of its "
                          "own")
    ap.add_argument("--host", default=cfg["host"],
                     help="dial this unconditionally when there's no adopt state yet: the "
                          "unsolicited pre-adopt handshake is required for the app to show the "
                          "device as 'AI Port' rather than a generic/'Unknown' device (the L2 "
                          "discovery beacon alone is not enough). It does NOT cause an un-adopted "
                          "device to auto-re-adopt. See the note above run() before re-adding a "
                          "wait-for-adopt gate.")
    ap.add_argument("--type", default=cfg["platform"],
                     help="catalog model string (see discovery.py's --platform help)")
    ap.add_argument("--sysid-hex", default=cfg["sysid"])
    ap.add_argument("--version", default=cfg["fw_version"])
    ap.add_argument("--bind-ip", default="0.0.0.0")
    ap.add_argument("--hostname", default=cfg["hostname"])
    ap.add_argument("--device-id", default=cfg["device_id"],
                     help="must match ucp4_client.py's --device-id -- same physical device")
    # Fixed Ubiquiti catalog GUID for the "AI Port" SKU, not a per-device random ID.
    ap.add_argument("--guid", default=cfg["guid"],
                     help="must match ucp4_client.py's --guid -- same physical device")
    ap.add_argument("--state-dir", default=None,
                     help="where adopt_state.json/server.crt/server.key live -- "
                          "defaults to the shared piport/ path (single-instance, "
                          "unchanged behavior); set this to isolate a second/third AI "
                          "Port instance running on the same box (see "
                          "instance_manager.py) -- must match the other 3 processes' "
                          "--state-dir for the same instance. Snapshot JPEGs always "
                          "live in a tmpfs-backed dir (config.stream_dir), never on "
                          "persistent storage")
    args = ap.parse_args()
    args.iface = config.resolve_iface(args.iface, cfg)

    if args.state_dir:
        global ADOPT_STATE_FILE, CERT_PATH, KEY_PATH, _STREAM_DIR, STATE_DIR
        os.makedirs(args.state_dir, exist_ok=True)
        ADOPT_STATE_FILE = os.path.join(args.state_dir, "adopt_state.json")
        CERT_PATH = os.path.join(args.state_dir, "server.crt")
        KEY_PATH = os.path.join(args.state_dir, "server.key")
        # RAM-only handoff; http_api.py derives the same dir from --state-dir.
        _STREAM_DIR = config.stream_dir(args.state_dir)
        STATE_DIR = args.state_dir
    _reset_stream_dir()
    if hoststats.enabled():
        # Once a minute beside the frame stats: CPU by pi-port/Frigate/OrbStack, GPU, ANE.
        hoststats.start()

    mac_nosep = config.resolve_mac(args.iface, args.mac, cfg).lower()
    ip = config.advertised_ip(config.resolve_ip(args.iface, cfg, args.bind_ip))

    device_info = {
        "mac_nosep": mac_nosep,
        "type": args.type,
        "sysid_hex": args.sysid_hex,
        "version": args.version,
        "ip": ip,
        "hostname": args.hostname,
        "name": cfg.get("name"),
        "device_id": args.device_id,
        "guid": args.guid,
    }

    # Single-use token: present on first connect after adopt, omitted on later reconnects.
    used_tokens = set()
    waiting_logged = False

    _init_detector(cfg)

    pinned = config.pinned_fw_version(STATE_DIR)
    if pinned:
        log.info("using pinned firmware version %s; delete the state fw_version.json to unpin",
                  pinned)

    while True:
        # A simulated update pins a version for the next (fake-reboot) reconnect;
        # otherwise report the configured --version.
        device_info["version"] = config.effective_fw_version(args.version, STATE_DIR)
        # Read adopt state if present but don't block; dial args.host pre-adopt.
        try:
            with open(ADOPT_STATE_FILE) as f:
                state = json.load(f)
        except (OSError, json.JSONDecodeError):
            state = {}
        hosts = state.get("hosts") or []
        if hosts:
            try:
                host_port = hosts[0]
                if ":" in host_port:
                    host, port_s = host_port.rsplit(":", 1)
                    port = int(port_s)
                else:
                    host, port = host_port, args.fallback_port
            except (TypeError, ValueError):
                log.warning("malformed adopt state host %r; using --host", hosts[0])
                host, port = args.host, args.fallback_port
        else:
            host, port = args.host, args.fallback_port

        if not host:
            # Not adopted and no console configured: Protect finds it by discovery and adopts it
            # through http_api.py, which writes adopt_state.json. Connecting meanwhile only
            # logged "hostname isn't provided" every 5 s.
            if not waiting_logged:
                log.info("no console host yet ([console] host / AIPORT_CONSOLE): waiting to be "
                         "adopted through discovery")
                waiting_logged = True
            time.sleep(5.0)
            continue
        waiting_logged = False

        token = state.get("token")
        send_token = token is not None and token not in used_tokens
        if token is not None:
            used_tokens.add(token)

        start = time.time()
        try:
            run(host, port, device_info, token=token if send_token else None)
            log.warning("classic avclient connection closed cleanly by peer")
        except Exception as e:
            log.warning("classic avclient connection ended: %r", e)
        # Always pace reconnects; a clean close must not skip the sleep (rate limiter is 60 msgs/60s).
        elapsed = time.time() - start
        time.sleep(max(0.0, 5.0 - elapsed))


if __name__ == "__main__":
    main()
