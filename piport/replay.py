# SPDX-License-Identifier: AGPL-3.0-or-later
"""Replay a recorded clip through pi-port's detector and tracker, offline.

Runs the clip through the same ffmpeg filter graph as a live camera, feeds each frame to
avclient's motion tick with a clock taken from the frame timestamps (so it runs as fast as
the detector allows), and prints the EventSmartDetect messages pi-port would have sent:
which events open, which objects join them, and when they leave.

    venv/bin/python piport/replay.py ~/piport-clips/camera-walk.mp4
    venv/bin/python piport/replay.py CLIP --moves          # every moving update too
    venv/bin/python piport/replay.py CLIP --json           # machine-readable summary
    venv/bin/python piport/replay.py CLIP --faces          # read faces as the live snapshot does
    venv/bin/python piport/replay.py --check ~/piport-clips  # every clip with an "expect"

A sidecar CLIP.json (written by scripts/record_clip.py) supplies the camera's Protect
settings: stream width/height (for Protect's coordinate canvas), detection and exclusion
zones, lines, eventStartMSec and the event time limit. Without one the camera has no zones.
The detector is the live one: AIPORT_MODEL_PATH etc. apply. Thumbnails aren't produced.

With --faces, faces (and plates) are read once a second from the clip's own full-size frames,
as live they're read from the camera's once-a-second snapshot; no face DB or gallery is used.

--check replays every clip whose sidecar has an "expect" block, each in its own process with
the sidecar's "env" (AIPORT_* settings, read at import) and "args" (e.g. ["--startup",
"--device", "02000000000B"]), and prints PASS or FAIL per clip. "expect" may hold:
    "events": 1 or [min, max]                 how many events
    "objects": {"vehicle": 1, "person": [2, 3]}  tracks of each type, over all events
    "distinct_face_snapshots": true           no two face tracks have the same face thumbnail
    "no_face_embed": true                     no faceEmbed is sent
    "vehicle_type": "suv", "color": "black", "plate": "ABC123"
                                              every reported vehicle's final attributes (a value
                                              or a list of accepted values)
and "why": what the clip guards against. A clip still running after --clip-timeout seconds
(default 300; a sidecar's "timeout" overrides it) is killed and reported as FAIL timed out.
"""
import argparse
import collections
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time as _time
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

DEVICE = "REPLAY"


def load_sidecar(clip_path, sidecar_path=None):
    """The clip's settings JSON ({} if there is none)."""
    path = sidecar_path or os.path.splitext(clip_path)[0] + ".json"
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        return json.load(f)


def probe_size(path):
    """(width, height) of the clip's video, or None."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=width,height", "-of", "csv=p=0", path], capture_output=True, text=True)
    try:
        w, h = out.stdout.strip().split(",")[:2]
        return int(w), int(h)
    except ValueError:
        return None


def probe_codec(path):
    """The clip's video codec ('h264', 'hevc'), or None."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=codec_name", "-of", "csv=p=0", path], capture_output=True, text=True)
    return out.stdout.strip().lower() or None


def clip_frames(av, path, width=None, height=None):
    """Detector frames of the clip, exactly as a live camera produces them: the same decoder,
    GPU scaling and filter graph (width/height: the stream's size, which sets the detector
    frame's shape when regions are on)."""
    if not (width and height):
        width, height = probe_size(path) or (None, None)
    det_w, det_h = av._detect_dims(width, height)
    pre_args, graph, _, _ = av._pull_video_args(probe_codec(path), det_w, det_h)
    cmd = [av.FFMPEG_BIN, "-loglevel", "error", *pre_args, "-i", path,
           "-filter_complex", graph,
           "-map", "[jpg]", "-fps_mode", "passthrough", "-f", "null", os.devnull,
           "-map", "[det]", "-fps_mode", "passthrough", "-pix_fmt", "rgb24", "-f", "rawvideo", "-"]
    import numpy as np
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE)
    frame_bytes = det_w * det_h * 3
    try:
        while True:
            buf = proc.stdout.read(frame_bytes)
            if len(buf) < frame_bytes:
                break
            yield np.frombuffer(buf, dtype=np.uint8).reshape((det_h, det_w, 3))
    finally:
        proc.stdout.close()
        proc.wait()


def snapshot_frames(av, path):
    """(t_s, BGR frame) once a second at the clip's full size: what the live snapshot gives the
    face and plate readers."""
    width, height = probe_size(path) or (None, None)
    if not (width and height):
        return
    import numpy as np
    cmd = [av.FFMPEG_BIN, "-loglevel", "error", "-i", path, "-vf", "fps=1",
           "-pix_fmt", "bgr24", "-f", "rawvideo", "-"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE)
    frame_bytes = width * height * 3
    try:
        k = 0
        while True:
            buf = proc.stdout.read(frame_bytes)
            if len(buf) < frame_bytes:
                break
            yield float(k), np.frombuffer(buf, dtype=np.uint8).reshape((height, width, 3))
            k += 1
    finally:
        proc.stdout.close()
        proc.wait()


REPLAY_DIR_PREFIX = "piport-replay-"
LEGACY_DIR_MAX_AGE_S = 3600


def _pid_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def sweep_replay_dirs(tmp=None):
    """Remove temp dirs left by replays that died without cleaning up (killed by --check's
    timeout, or by hand: their finally never ran). A dir is named after its replay's pid
    and removed once that process is gone; an older unnumbered one, after an hour."""
    tmp = tmp or tempfile.gettempdir()
    try:
        names = os.listdir(tmp)
    except OSError:
        return []
    removed = []
    for name in names:
        if not name.startswith(REPLAY_DIR_PREFIX):
            continue
        path = os.path.join(tmp, name)
        pid = name[len(REPLAY_DIR_PREFIX):].split("-", 1)[0]
        if pid.isdigit():
            stale = not _pid_alive(int(pid))
        else:
            try:
                stale = _time.time() - os.path.getmtime(path) > LEGACY_DIR_MAX_AGE_S
            except OSError:
                continue
        if stale:
            shutil.rmtree(path, ignore_errors=True)
            removed.append(name)
    return removed


def replay(av, frames, settings=None, start_ms=0, settle_s=5.0, startup=False, snapshots=None):
    """Run frames (at av.DETECT_FPS) through the tracker. Returns [(t_s, payload)] for every
    EventSmartDetect sent. After the clip, `settle_s` of empty frames let open tracks close.
    startup=False skips the live 15 s startup window, in which objects already in view are
    parked silently; True keeps it (a clip that starts with a parked car). avclient's
    globals are restored afterwards."""
    sweep_replay_dirs()
    settings = settings or {}
    sent, now = [], [0.0]
    snap = {"next": None, "cur": (None, None), "it": iter(snapshots) if snapshots else None}

    def load_snapshot(device_id):
        """The newest snapshot at or before the clip's clock (None before the first)."""
        if snap["it"] is None:
            return None, None
        while True:
            if snap["next"] is None:
                snap["next"] = next(snap["it"], False)
            if snap["next"] is False or snap["next"][0] > now[0]:
                return snap["cur"][1], snap["cur"][0]
            snap["cur"], snap["next"] = snap["next"], None

    jpeg_cache = {}

    def read_snapshot(device_id):
        """The current snapshot as (mtime, JPEG bytes), as live best frames are captured from
        the snapshot file; (None, None) without --faces."""
        img, mtime = load_snapshot(device_id)
        if img is None:
            return None, None
        if mtime not in jpeg_cache:
            import cv2
            jpeg_cache.clear()
            jpeg_cache[mtime] = cv2.imencode(".jpg", img)[1].tobytes()
        return mtime, jpeg_cache[mtime]

    class _ClipTime:
        """avclient's `time`, with the monotonic clock following the clip (the motion
        gate's reference frame advances on it)."""
        def __getattr__(self, name):
            return getattr(_time, name)

        def monotonic(self):
            return now[0]

    patches = {
        "time": _ClipTime(),
        "send_msg": lambda ws, fn, payload, **kw: (
            sent.append((now[0], payload)) if fn == "EventSmartDetect" else None),
        "LEAVE_FALLBACK_S": 10 ** 9,             # no snapshot requests will come
        "_frame_jpeg": lambda *a, **k: None,     # no JPEG pipe offline
        "_BOX_DELAY_DEFAULT_MS": 0,
        "_BOX_DELAY_MS": {},
        # Never the live ones; named after this process so a later sweep knows when it's gone.
        "_STREAM_DIR": tempfile.mkdtemp(prefix="%s%d-stream-" % (REPLAY_DIR_PREFIX, os.getpid())),
        "STATE_DIR": tempfile.mkdtemp(prefix="%s%d-state-" % (REPLAY_DIR_PREFIX, os.getpid())),
        "_load_snapshot_bgr": load_snapshot,
        "_read_snapshot": read_snapshot,
        "VEHICLE_FM_SYNC": True,   # the model answers before the clip moves on
    }
    if not startup:
        patches["STARTUP_WINDOW_MS"] = -1
    saved = {name: getattr(av, name) for name in patches}
    for name, value in patches.items():
        setattr(av, name, value)
    try:
        _run(av, frames, settings, start_ms, settle_s, now)
    finally:
        for name, value in saved.items():
            setattr(av, name, value)
        shutil.rmtree(patches["_STREAM_DIR"], ignore_errors=True)
        shutil.rmtree(patches["STATE_DIR"], ignore_errors=True)
        av._enrich_cache.pop(DEVICE, None)
        for table in (av._motion_state, av._stream_dims, av._latest_frame,
                      av._latest_frame_seq, av._latest_frame_time, av._analysed_frame_time,
                      av._detect_zones, av._detect_zone_types, av._exclude_zones, av._lines, av._event_start_ms,
                      av._event_max_ms, av._stream_started_monotonic_ms, av._scene_ref,
                      av._motion_detectors, av._motion_settings, av._motion_boxes,
                      av._last_parked_check, av._empty_motion):
            table.pop(DEVICE, None)
        for key in [k for k in av._dropped if k[0] == DEVICE]:
            av._dropped.pop(key)
        av._startup_scan.pop(DEVICE, None)
    return sent


def _run(av, frames, settings, start_ms, settle_s, now):
    import numpy as np
    for table in (av._motion_state, av._detect_zones, av._detect_zone_types, av._exclude_zones, av._lines,
                  av._event_start_ms, av._event_max_ms, av._motion_detectors,
                  av._motion_settings, av._motion_boxes):
        table.pop(DEVICE, None)
    if settings.get("width") and settings.get("height"):
        av._stream_dims[DEVICE] = (int(settings["width"]), int(settings["height"]))
    detect = settings.get("smartDetect") or {}
    av._detect_zones[DEVICE] = av._parse_zones(detect.get("zones"))
    av._detect_zone_types[DEVICE] = av._parse_zone_types(detect.get("zones"))
    av._exclude_zones[DEVICE] = av._parse_zones(detect.get("excludeZones"))
    av._lines[DEVICE] = av._parse_lines(detect.get("lines"))
    if isinstance(detect.get("eventStartMSec"), (int, float)):
        av._event_start_ms[DEVICE] = int(detect["eventStartMSec"])
    motion = settings.get("smartMotion") or {}
    if isinstance(motion.get("eventMaxDurationMSec"), (int, float)):
        av._event_max_ms[DEVICE] = int(motion["eventMaxDurationMSec"])
    av._motion_detectors.pop(DEVICE, None)
    av._motion_settings.pop(DEVICE, None)
    av._startup_scan.pop(DEVICE, None)
    av._last_parked_check.pop(DEVICE, None)
    av._redo_cache.pop(DEVICE, None)
    av._apply_motion_settings(dict(motion, deviceID=DEVICE))
    av._stream_started_monotonic_ms[DEVICE] = 0

    step_ms = 1000.0 / av.DETECT_FPS
    n, last = 0, None

    def tick(frame):
        t_ms = int(n * step_ms)
        now[0] = t_ms / 1000.0
        with av._frame_lock:
            av._latest_frame[DEVICE] = frame
            av._latest_frame_seq[DEVICE] = n + 1
            av._latest_frame_time[DEVICE] = (start_ms + t_ms, t_ms)
        av._motion_tick(None, DEVICE)

    for frame in frames:
        tick(frame)
        last, n = frame, n + 1
    if last is not None:
        blank = np.zeros_like(last)
        for _ in range(int(settle_s * av.DETECT_FPS)):
            tick(blank)
            n += 1


def summarize(sent):
    """Events in order: id, start/end (s), and each object's type and best confidence."""
    events = collections.OrderedDict()
    for t, p in sent:
        ev = events.setdefault(p["eventId"], {"id": p["eventId"], "start_s": t, "end_s": None,
                                              "objects": {}})
        for d in p.get("descriptors") or []:
            o = ev["objects"].setdefault(d["trackerID"], {"tracker": d["trackerID"],
                                                         "type": d["objectType"], "max_conf": 0})
            o["max_conf"] = max(o["max_conf"], d.get("confidenceLevel", 0))
        for snap in p.get("smartDetectSnapshots") or []:
            o = ev["objects"].setdefault(snap["trackerID"], {
                "tracker": snap["trackerID"], "type": snap.get("smartDetectSnapshotType"),
                "max_conf": 0})
            o["max_conf"] = max(o["max_conf"], snap.get("confidenceLevel", 0))
        if p["edgeType"] == "leave":
            ev["end_s"] = t
            for snap in p.get("smartDetectSnapshots") or []:
                ev["objects"][snap["trackerID"]]["snapshot"] = snap.get("coord")
            for tid, attrs in (p.get("trackerIDAttrMap") or {}).items():
                o = ev["objects"].get(int(tid))
                if o is not None:
                    o["face_embed"] = "faceEmbed" in attrs
                    if attrs.get("objectType") == "vehicle":
                        for key, field in (("vehicleType", "vehicle_type"), ("color", "color")):
                            if isinstance(attrs.get(key), dict):
                                o[field] = attrs[key].get("val")
                        if attrs.get("matchedName"):
                            o["plate"] = attrs["matchedName"]
    return [dict(ev, objects=list(ev["objects"].values())) for ev in events.values()]


def _within(value, want):
    lo, hi = (want, want) if isinstance(want, int) else want
    return lo <= value <= hi


def check(expect, events):
    """What in `expect` (a sidecar's "expect" block) the summarized events miss: [reason]."""
    bad = []
    if "events" in expect and not _within(len(events), expect["events"]):
        bad.append("%d event(s), expected %s" % (len(events), expect["events"]))
    objects = [o for ev in events for o in ev["objects"]]
    for kind, want in (expect.get("objects") or {}).items():
        n = sum(o["type"] == kind for o in objects)
        if not _within(n, want):
            bad.append("%d %s track(s), expected %s" % (n, kind, want))
    if expect.get("distinct_face_snapshots"):
        faces = [o for o in objects if o["type"] == "face" and o.get("snapshot")]
        for i, a in enumerate(faces):
            for b in faces[i + 1:]:
                if _iou(a["snapshot"], b["snapshot"]) >= 0.5:
                    bad.append("faces #%s and #%s share a thumbnail %s"
                               % (a["tracker"], b["tracker"], a["snapshot"]))
    if expect.get("no_face_embed"):
        sent = [o["tracker"] for o in objects if o.get("face_embed")]
        if sent:
            bad.append("faceEmbed sent for face(s) %s" % sent)
    vehicles = [o for o in objects if o["type"] == "vehicle"]
    for field in ("vehicle_type", "color", "plate"):
        if field not in expect:
            continue
        want = expect[field] if isinstance(expect[field], list) else [expect[field]]
        got = [(o["tracker"], o.get(field)) for o in vehicles]
        if not got:
            bad.append("no vehicle to check %s against" % field)
        for tracker, value in got:
            if value not in want:
                bad.append("vehicle #%s %s %s, expected %s"
                           % (tracker, field.replace("_", " "), value, " or ".join(want)))
    return bad


def _iou(a, b):
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    iw = min(ax + aw, bx + bw) - max(ax, bx)
    ih = min(ay + ah, by + bh) - max(ay, by)
    if iw <= 0 or ih <= 0:
        return 0.0
    return iw * ih / float(aw * ah + bw * bh - iw * ih)


# A clip replays in about real time or faster (the longest, 60 s, took ~70 s); one still going
# after this is stuck (live: 21 min on one clip at 74% CPU, with nothing printed).
CLIP_TIMEOUT_S = 300


def _run_clip(cmd, env, timeout):
    """Run one clip's replay; on a timeout kill its whole process group (the replay and the
    ffmpeg it decodes with) and raise subprocess.TimeoutExpired."""
    proc = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, start_new_session=True)
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.communicate()
        raise
    return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)


def check_clips(paths, out=sys.stdout, timeout=CLIP_TIMEOUT_S):
    """Replay every clip with an "expect" under paths (clips or folders), one process each,
    killing one that runs past `timeout` seconds (a sidecar's "timeout" overrides it).
    Returns how many failed."""
    sidecars = []
    for path in paths:
        if os.path.isdir(path):
            sidecars += sorted(os.path.join(path, n) for n in os.listdir(path)
                               if n.endswith(".json"))
        else:
            sidecars.append(os.path.splitext(path)[0] + ".json")
    failed = 0
    for sidecar in sidecars:
        with open(sidecar) as f:
            settings = json.load(f)
        expect = settings.get("expect")
        clips = [os.path.splitext(sidecar)[0] + ext for ext in (".mp4", ".mkv", ".avi")]
        clip = next((c for c in clips if os.path.exists(c)), None)
        if not expect or clip is None:
            continue
        env = dict(os.environ, **{k: str(v) for k, v in (settings.get("env") or {}).items()})
        cmd = [sys.executable, os.path.abspath(__file__), clip, "--json"] + list(
            settings.get("args") or [])
        t0 = _time.monotonic()
        name = os.path.basename(clip)
        limit = float(settings.get("timeout") or timeout)
        try:
            run = _run_clip(cmd, env, limit)
        except subprocess.TimeoutExpired:
            failed += 1
            print("FAIL %s: timed out after %.0f s" % (name, limit), file=out)
            if settings.get("why"):
                print("    guards: %s" % settings["why"], file=out)
            continue
        try:
            events = json.loads(run.stdout)["events"]
        except (ValueError, KeyError):
            failed += 1
            print("FAIL %s: replay crashed\n%s" % (name, run.stderr[-2000:]), file=out)
            continue
        bad = check(expect, events)
        failed += bool(bad)
        found = ", ".join("%s#%s" % (o["type"], o["tracker"]) for ev in events
                          for o in ev["objects"]) or "no events"
        print("%s %s (%.0f s): %s%s" % ("FAIL" if bad else "PASS", name, _time.monotonic() - t0,
                                       found, "".join("\n    - " + b for b in bad)), file=out)
        if bad and settings.get("why"):
            print("    guards: %s" % settings["why"], file=out)
    return failed


def _fmt_desc(d):
    return "#%s %s %s %s" % (d["trackerID"], d["objectType"], d.get("confidenceLevel"),
                             d.get("coord"))


def print_report(sent, moves=False, out=sys.stdout):
    for t, p in sent:
        if p["edgeType"] == "moving" and not moves:
            continue
        descs = ", ".join(_fmt_desc(d) for d in p.get("descriptors") or [])
        if p["edgeType"] == "leave" and not descs:
            descs = ", ".join("#%s %s" % (s["trackerID"], s.get("smartDetectSnapshotType"))
                              for s in p.get("smartDetectSnapshots") or [])
        print("t=%7.2fs  %-6s event %s  %s" % (t, p["edgeType"], p["eventId"], descs), file=out)
    events = summarize(sent)
    print("\n%d event(s)" % len(events), file=out)
    for ev in events:
        span = ("%.1fs-%.1fs" % (ev["start_s"], ev["end_s"]) if ev["end_s"] is not None
                else "%.1fs-(still open)" % ev["start_s"])
        objs = ", ".join("%s#%s (max %s)" % (o["type"], o["tracker"], o["max_conf"])
                         for o in ev["objects"])
        print("  event %s: %s  %s" % (ev["id"], span, objs), file=out)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("clip", nargs="?")
    ap.add_argument("--check", nargs="+", metavar="CLIP_OR_DIR",
                    help="replay every clip with an \"expect\" block and report PASS/FAIL")
    ap.add_argument("--clip-timeout", type=float, default=CLIP_TIMEOUT_S, metavar="S",
                    help="with --check, kill a clip still running after S seconds (default 300)")
    ap.add_argument("--faces", action="store_true",
                    help="read faces and plates once a second from full-size frames")
    ap.add_argument("--device", help="the camera's deviceID, so per-camera AIPORT_* settings apply")
    ap.add_argument("--startup-window-ms", type=int,
                    help="length of the startup window with --startup (live: 15000)")
    ap.add_argument("--sidecar", help="settings JSON (default: CLIP with .json)")
    ap.add_argument("--fps", type=float, help="detection rate (AIPORT_DETECT_FPS)")
    ap.add_argument("--settle", type=float, default=5.0,
                    help="seconds of empty frames after the clip so open tracks close")
    ap.add_argument("--startup", action="store_true",
                    help="keep the live startup window (objects present at once are parked)")
    ap.add_argument("--moves", action="store_true", help="print every moving update")
    ap.add_argument("--json", action="store_true", help="print the summary as JSON")
    ap.add_argument("-v", "--verbose", action="store_true", help="avclient's INFO log on stderr")
    args = ap.parse_args(argv)
    if args.check:
        sys.exit(1 if check_clips(args.check, timeout=args.clip_timeout) else 0)
    if not args.clip:
        ap.error("a clip is needed (or --check)")
    if args.device:
        global DEVICE
        DEVICE = args.device

    if args.fps:
        os.environ["AIPORT_DETECT_FPS"] = str(args.fps)
    import logging
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(levelname)s %(message)s", stream=sys.stderr)
    import avclient as av  # after the env is set: rates are read at import

    settings = load_sidecar(args.clip, args.sidecar)
    if not settings.get("width"):
        size = probe_size(args.clip)
        if size:
            settings["width"], settings["height"] = size
    start_ms = 0
    if settings.get("start"):
        start_ms = int(datetime.fromisoformat(settings["start"]).timestamp() * 1000)
    if args.startup_window_ms is not None:
        av.STARTUP_WINDOW_MS = args.startup_window_ms
    sent = replay(av, clip_frames(av, args.clip, settings.get("width"), settings.get("height")),
                  settings, start_ms=start_ms,
                  settle_s=args.settle, startup=args.startup,
                  snapshots=snapshot_frames(av, args.clip) if args.faces else None)
    if args.json:
        json.dump({"events": summarize(sent), "messages": len(sent)}, sys.stdout, indent=1)
        print()
    else:
        print_report(sent, moves=args.moves)


if __name__ == "__main__":
    main()
