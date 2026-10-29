# Plan: porting Frigate's detection pipeline into pi-port

Status: done. Proposed 2026-10-17; steps 0 to 6 are in the code (Norfair is optional,
`AIPORT_TRACKER`). Kept as the record of what was ported from Frigate and why.
Source: Frigate at `~/Code/frigate` (commit `add310d63`, MIT licence). Code copied or closely
adapted from it keeps Frigate's copyright and MIT notice in the file header; pi-port stays
AGPL-3.0.

## Why

What pi-port got wrong on 2026-10-16/17, and the Frigate piece that avoids it:

| Seen live | pi-port today | Frigate |
| --- | --- | --- |
| Person walking through Camera C's background never detected (13:54:03) | whole-frame mean-diff gate; person ~0.5% of the frame | motion **boxes** from a background model |
| Tree stump "animal" 3x, Camera A's pot "animal" | open at 0.5; my must-move and keep-at-0.3 rules | true positive only when the **median of the last 10 scores >= 0.7** |
| Parked car woken by box jitter (Camera A 12:25) | box-geometry `_moved_since` | **pixel** comparison of the parked spot |
| Sedan on the far road: 2 of ~6 frames detected; cut-off bus dropped | whole frame squeezed to 640x640, then 320x320 | **square crops** at native resolution around motion and tracks |
| Zone edge along the Camera C hedge | box centre in zone | box **bottom-centre** |
| Unloading car re-detected as new vehicles | IoU + nearest-neighbour, home-grown resume | Kalman-predicted **Norfair** tracker, 5 s hit counter, appearance re-ID |

## Order of work

Each step is its own commit, with tests, docs, and a replay check (step 0) before it ships.
Steps 1-4 work on today's 640x640 frames; step 5 changes the video pipeline; step 6 is
decided after step 5.

### Step 0. Replay harness (prerequisite)

Done: `piport/replay.py`, `scripts/record_clip.py`, `piport/tests/test_replay.py`
(see "Replaying clips" in `piport/README.md`). Golden clips still to record.

Every change so far was judged on live events after a restart. Instead:

- `scripts/record_clip.sh <camera-uri> <seconds> <name>`: `ffmpeg -rtsp_transport tcp -i ... -c copy`
  into `~/piport-clips/<name>.mp4` (outside git; the clips show the property).
- `piport/replay.py <clip> [--camera-dims WxH] [--zones json]`: runs the same ffmpeg filter
  graph on the file, feeds frames through `_motion_tick` with a fake websocket and a fake
  clock driven by frame timestamps, and prints the timeline of enter/moving/leave per tracker,
  plus a summary (events, objects, false events).
- Golden clips to record first: Camera C walker (left-to-right and back), Camera C stump in
  sun, Camera A parked car with weak boxes, Camera B sedan on the far road, Camera B car
  unloading, a bus.
- Each later step states its expected effect on these clips; `tests/test_replay.py` runs the
  harness on tiny synthetic clips (lavfi) in CI so the harness itself can't rot.

Size: medium. Risk: low.

### Step 1. Zones by bottom-centre (item 3)

Done: `_protect_foot` in `avclient.py`; the three recorded clips still replay to 0 events.

- `_is_outside_detect_zones` and `_is_excluded` test `(x + w/2, y + h)` instead of the centre,
  in Protect's coordinate space as now. Line crossing keeps using the centre path (Frigate does
  the same: zones use the bottom-centre, the path uses centroids).
- Open question: what the real AI Port uses. `docs/aiport-firmware-notes.md` doesn't say; if a
  later firmware read shows otherwise, make it `AIPORT_ZONE_POINT=center|bottom`.

Size: tiny. Risk: a zone drawn tight around feet-level ground behaves differently; check the
Camera C zones in Protect afterwards.

### Step 2. True-positive gating by median score (item 1)

Done. One change from the plan: a track confirms once it is a true positive and old enough even
on a frame it was missed in, so Protect's `eventStartMSec` doesn't drop objects seen briefly.

Port Frigate's `TrackedObject.compute_score` / `_is_false_positive`:

- Each detection still needs `AIPORT_OBJ_THRESH` (0.5) to exist at all (Frigate's `min_score`).
- Each track keeps `score_history`: its score each analysed frame, **0.0 for a frame it was
  missed**, last 10 only.
- A track confirms (joins the event) when `median(score_history) >= AIPORT_TRUE_THRESH`
  (default 0.7; per type, e.g. `0.7,animal=0.75`) **and** Protect's `eventStartMSec` has passed.
  Faces and plates keep confirming with their parent (they come from enrichment, not YOLO).
- Removed, because the median replaces them: `KEEP_THRESH` (weak boxes keeping tracks),
  `MUST_MOVE`, "weak boxes don't confirm", "weak boxes don't move parked tracks", the
  weak-tick logging, and the detector's `min_score` plumbing (back to one threshold).
- Kept: `AIPORT_MISS_S` (Frigate's equivalent is `max_disappeared` = 5 s at detect fps),
  resume-recent (until step 6), inside-a-parked-box.
- Expected on the golden clips: stump (median ~0.55) and pot never confirm; the 14:51 sedan
  (0.85, 0.78) confirms on frame 2; the hatch-open car (0.50-0.59) never re-confirms, so it
  can't come back as a new vehicle.
- Risk: real objects that score low (a person at night ~0.55) are no longer reported. Frigate
  users tune `threshold` per object for this; log a debug line with the median for tracks that
  never confirm so the right per-type value can be read off the log.

Size: small. Net code removed.

### Step 3. Frigate's motion detector (item 2)

Done: `piport/motion.py`; the gate is on everywhere again. Also fixed on the way: a tick the gate
skipped took the wall clock instead of its frame's time.

Port `frigate/motion/improved_motion.py` (`ImprovedMotionDetector`) as `piport/motion.py`:

- 100 px high grayscale, contrast stretched to the 4th-96th percentile averaged over 50 frames,
  Gaussian blur (cv2 instead of scipy, to avoid a dependency), background as a running average
  (alpha 0.01, 0.2 while calibrating), pixel threshold 30, dilate, contours with area > 10 px,
  recalibrate when > 80% of the frame changes (lightning, IR switch). Output: motion boxes in
  frame pixels.
- The gate becomes "no motion boxes and no open tracks". It goes back **on by default on
  macOS**, since it no longer hides small movers.
- Protect's `ChangeSmartMotionSettings` (`zones`, `level` 0-100) map onto the motion mask and
  threshold (level 50 -> 30; scale linearly), so Protect's motion sensitivity slider works.
- The 5 s keyframe "pops" on Camera C should stay under the per-pixel threshold after blurring;
  check on the replay clips.
- Motion boxes are also what step 5 crops around.

Size: small-medium (the module is ~280 lines, mostly copied). Risk: low; tunables are
Frigate's, which are well exercised.

### Step 4. Pixel-based "did the parked object move" (item 4)

Done: `piport/stationary.py`, used by `_parked_spot_unchanged` in `avclient.py`.

Port `frigate/track/stationary_classifier.py` (`StationaryMotionClassifier`):

- When a track parks, store an anchor crop: the luma inside its median box from the last 10
  boxes, resized to a fixed size (`CROP_SIZE`).
- When the parked track's box drifts (today's `_moved_since` geometry), compare the same spot
  now with the anchor: normalised cross-correlation and phase-correlation shift. It counts as
  moved only if the picture there changed (low NCC or a real shift), not when the box changed.
- Needs a luma frame: today's 640x640 RGB is enough (convert the crop); after step 5, the Y
  plane at detect resolution.
- Expected: Camera A 12:25 case (weak boxes walking the box onto half the car) stays
  parked even without step 2's removal of weak boxes; a car driving off still wakes.

Size: medium. Risk: lighting changes across a parked car (sun, headlights) lower NCC; Frigate
blends its thresholds for that, and they're copied as they are.

### Step 5. Region crops at native aspect (item 5): the pipeline change

Done, with RGB frames rather than YUV (simpler; the pipe carries 2.8 MB a frame at 1280x720).
Found on the recorded Camera B clip and fixed: a single high-scoring crop became an event
(now 2 hits needed, as Frigate's tracker requires); a parked object repeated a recheck's box
until it counted as moved (it now repeats its parked box); and a parked camper's pixel anchor
went stale in moving sun (refreshed at each recheck that finds it in place). Parked-car scores
on startup crops: Camera A 0.77 and 0.65, Camera B red car 0.63, so `vehicle=0.6` stays for now.

**Frames.** ffmpeg's detection output changes from 640x640 stretched RGB to **YUV420 at a
detect resolution that keeps the camera's aspect ratio**: the long side is 1280 by default
(`AIPORT_DETECT_SIZE`), so 1280x720 for Camera B/Camera C and 960x1280 for portrait
Camera A. That's 1.4-1.8 MB per frame, against 1.2 MB now. The full-res JPEG pipe and its
frame numbering stay as they are.

**Per frame** (after Frigate's `process_frames`):

1. Motion boxes from step 3 (on the Y plane).
2. Region candidates: boxes of active (non-parked) tracks, using their predicted position, plus
   motion boxes not inside those, clustered with Frigate's `get_cluster_candidates` /
   `get_cluster_region` (`frigate/util/object.py`). Each region is square and at least the
   model input (320), up to 2x the cluster, clamped to the frame. No learned `region_grid` at
   first; add it later if regions come out too small or large.
3. Startup: one full-frame grid scan (`get_startup_regions`) so parked cars are found at once.
4. Each region: Y+UV crop -> RGB (`yuv_region_2_rgb`), resize to the model input, detect, map
   boxes back to frame pixels; drop boxes clipped by the region edge unless no other region
   sees them (Frigate's 0.6 confidence for clipped boxes).
5. Merge across regions with Frigate's `reduce_detections` (per-label NMS plus dropping boxes
   mostly inside a larger same-label box), which also covers today's inside-parked rule.
6. Normalise to 0-1000 with the real detect width and height: no more 640/1000 on both axes.
7. Parked tracks aren't re-detected every frame: every `stationary.interval` frames
   (Frigate: 10 s worth), or when motion overlaps them.

**What else changes:** `_scene_changed` (replaced by motion), `enrich.vehicle_attributes`
(takes the RGB crop rather than the 640 frame), `_classify_frame_detections` (regions loop),
`DETECT_IMG_SIZE` uses, and `detector.Detector.detect` (gets the crop; it already resizes to
the model's input).

**Cost.** One inference per region, 3.4 ms each for YOLOv9-s on the M2. With 1-3 regions per
camera per frame at 10 fps on three cameras, that's 30-90 inferences a second, 0.1-0.3 s of
ANE time per second. Frigate defaults to 5 detect fps; measure at 10 and 5. Python-side YUV
cropping is small (320x320 crops).

**Pi / RK3588.** The NPU model is fixed at 640x640 and slow per call, so keep today's
whole-frame path there: `AIPORT_REGIONS=0` (default off on Linux, on on macOS).

**Expected on the golden clips:** the Camera C background walker detected for most of the
walk; the Camera B sedan on most frames; the cut-off bus kept to the frame edge.

Size: large. Risk: medium; timing, coordinates and the JPEG pairing all touch this path.
Done behind `AIPORT_REGIONS` so it can be switched off live.

### Step 6. Norfair tracker (item 6): decide after step 5

Port the matching in `frigate/track/norfair_tracker.py`:

- One `norfair.Tracker` per object type, with Frigate's distance thresholds (2-3.75, its
  `frigate_distance` on box corners and centre), `initialization_delay` = fps/2,
  `hit_counter_max` = 5 s of frames, and colour-histogram re-ID for people.
- pi-port's track dict (event, parked state, best frame, attributes, line crossings) stays;
  Norfair only decides which detection belongs to which tracker ID.
- Replaces: IoU matching, nearest-neighbour fallback, resume-recent, the miss tolerance.
- New dependency: `norfair` (BSD-3-Clause) plus its `filterpy` dependency.
- Go/no-go: compare matching on the replay clips against today's matcher (ID switches, split
  tracks). If steps 2-5 already leave clean tracks, skip it.

Size: medium-large. Risk: medium (ID assignment drives every event).

## Not ported

- Frigate's object masks and per-object `min_area`/`max_area`/ratio filters: Protect has no
  UI for them; revisit if needed (an `AIPORT_FILTERS` env var).
- Recording, review items, audio, semantic search, GenAI: not part of an AI Port.
- PTZ autotracking: no PTZ cameras here.

## Settings after the port

| Setting | Default | Notes |
| --- | --- | --- |
| `AIPORT_OBJ_THRESH` | 0.5 | per-detection minimum (Frigate `min_score`) |
| `AIPORT_TRUE_THRESH` | 0.7 | median of last 10 to confirm, per type (Frigate `threshold`) |
| `AIPORT_MOTION_GATE` | on | box-based after step 3, so on everywhere again |
| `AIPORT_REGIONS` | on (macOS), off (Linux) | step 5 |
| `AIPORT_DETECT_SIZE` | 1280 | long side of the detect frame |
| `AIPORT_DETECT_FPS` | 10 (macOS) | measure 5 vs 10 after step 5 |
| removed | | `AIPORT_KEEP_THRESH`, must-move, weak-box rules (step 2); `AIPORT_RESUME_S` (step 6, if done) |
