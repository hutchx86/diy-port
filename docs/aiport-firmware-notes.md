# AI Port firmware v5.1.12: notes

Facts read from Ubiquiti's public AI Port firmware image (v5.1.12, see
[protect-aiport-controller.md](protect-aiport-controller.md#firmware-source)) to guide
pi-port's behaviour. Nothing here is copied code: only file names, setting names,
default values and log-message wording, read with `strings` and `debugfs` without
running anything. The image and the extracted binaries stay outside this repo.
For the firmware actually running (adopted by Protect), see
[aiport-firmware-lab.md](aiport-firmware-lab.md).

## Image layout

- A `UBNT` header (`aiport4G.mt8390.v5.1.12.67...`), a small tar holding the updater
  (`bin/fwupdate.real`, `bin/precheck`, `hooks/`), a boot image (device-tree blob),
  and a gzip of a 3.8 GB **ext4 root filesystem** labelled `rootfs`.
- Systemd-based Linux on a MediaTek MT8390. Neural-network models are
  MediaTek `.dla` files in `/usr/etc/ai_model/`:
  `SmartDetect_normal_20240810`, `SmartAnalyze_20221114`,
  `SmartFace_detect_fast_20250220`, `SmartFace_dense_lmk_20250429`. They're compiled
  for that chip and can't be used by pi-port.

## Process layout

| Process | Role |
|---------|------|
| `ubnt_avclient` | The camera-protocol WebSocket to Protect. Routes messages to the other processes over IPC. |
| `ubnt_ctlserver` | HTTPS control API on :443 |
| `ubnt_onvifd` | **Stream control and capacity:** `UiStreamControl`, `OnvifStreamControl`, `GetStreamList`, `GetUiStreamPoints`, `ResetAIPortStreams` |
| `ubnt_av_aiport@<n>` | Per-camera RTSP ingest. Scales the source to the sizes the analysers need and notices resolution changes. |
| `ubnt_streamer@<n>` | Per-camera video pipeline |
| `ubnt_smart_detect@<n>` | Per-camera object detection and tracking (`-m=<n>`) |
| `ubnt_smart_motion@<n>` | Per-camera motion detection (`EventSmartMotion`) |
| `ubnt_audio_events@<n>` | Per-camera audio detection (`EventSmartAudio`) |

So the real AI Port runs motion, object and audio detection **separately for each
paired camera**.

## Stream control and capacity (`ubnt_onvifd`)

- Points are floats with one decimal (`used points: %.1f`); Protect divides
  `usedPoints` by 10. pi-port's 2 per camera matches Protect's HD rate. A 4K
  UniFi camera would be 5.0.
- Error codes: `resolution_exceed`, `mixed_device_type`, `connection_exceed`.
  Protect 7.1.75 only knows the last two, so `resolution_exceed` falls into its
  "unknown" branch.
- It checks resolution against a limit (`resolution %dx%d exceed limitation(%dx%d)`)
  and **lowers the frame rate it analyses** when needed (`Adjust LQ FPS`,
  `Adjust YUV FPS`). It doesn't analyse every frame.
- `GetStreamList` reports `Used points` and the active `deviceID`s.
  `GetUiStreamPoints` takes `width`, `height`, `fps`, `deviceID` and returns the
  cost. Protect 7.1.75 never calls it, but the AI Port supports it.
- `ResetAIPortStreams` stops every stream (`All streams should be stopped`).
- ONVIF: it reads the camera's own stream URL and resolution
  (`ONVIF StreamURL:%s, audio:%s, width:%d, height:%d, fps:%d`).

### Measured stream points

Measured by running the real `ubnt_onvifd` in an offline arm64 Linux sandbox
(chroot of the firmware rootfs, loopback-only network namespace) and sending it
`GetUiStreamPoints` with `ubnt_ipc_cli`. Request:
`{"functionName":"GetUiStreamPoints","list":[{"width","height","fps","deviceID"}]}`;
reply: `{"list":[{"deviceID","points"}]}`.

| Resolution | 10 fps | 15 | 20 | 25 | 30 |
|------------|--------|----|----|----|----|
| 640×360 – 1920×1080 (and 1080×1920) | 2 | 2 | 2 | 2 | 2 |
| 2304×1296 | 2 | 2 | 2 | 2.5 | 2.5 |
| 2560×1440, 2688×1512 | 2 | 2 | 2.5 | 2.5 | 2.5 |
| 2592×1944, 3072×1728, 3200×1800 | 2 | 2.5 | 2.5 | 5 | 5 |
| 3840×2160 (and 2160×3840) | 2.5 | 5 | 5 | 5 | 5 |
| 4096×2160 | 2.5 | 5 | 5 | 5 | 10 |
| 4000×3000 | 2.5 | 5 | 5 | 10 | 10 |

This is exactly Protect's own estimate ×10, tiered on width × height × fps:
≤ 62,208,000 → 2; ≤ 121,927,680 → 2.5; ≤ 248,832,000 → 5; above → 10. Orientation
doesn't matter. pi-port can sum this per active stream, using the `width`, `height`
and `fps` that `UiStreamControl` sends, and report it as `usedPoints`.

## Object detection and tracking (`ubnt_smart_detect`)

Default settings (`/usr/etc/newplat/smartdetect-default-response.json`):

| Setting | Default |
|---------|---------|
| `eventStartMSec` | 1000 |
| `eventStopMSec` | 3000 |
| `eventMaxDurationMSec` | 300000 |
| `eventCoolDownMSec` | 0 |
| `detectMode` | 60 |
| `eventTranMode` | 2 |
| `excludeNonTriggerDescriptors` | true |
| `personInFrontMovingThrottleMs` | 1000 |
| zone `1` | full frame, `sensitivity` 50, all object types |

From the log messages:

- **Tracker:** `IoUHungarianTracker`, with a first match on high-confidence
  detections and a second on low-confidence ones (ByteTrack-style), plus `KCFTracker`
  and `ObjectTracker`.
- **Lost tracks become stationary:** `Lost track {} set to stationary after {}ms`,
  `Track is outlier, keeping stationary`, `Static filter applied, force to stationary`,
  `Stepped into stationary zone`, `ForceHighTendencyStationary`.
- **Warmup:** `After {}ms warmup`, `END_WARMUP`, matching the `warmup` edge type in
  the protocol notes.
- **Second-pass check:** `Object verification failed - ... Original / Verified`,
  confidence modes (single detector, single classifier, multi-label).
- **Events:** `ENTER`, `MOVING`, `LEAVE`, `STATIONARY`; loiter (`loiterTriggerTime`,
  `Trigger Loiter Event`), line rules, package (`LEAVE_PACKAGE_AT_DOOR`), tamper,
  person-in-front, face identity (`Assigned final identity ... subject_id`), plate
  linked to vehicle (`No LP trackerID {} found for vehicle trackerID`).
- The device keeps `/var/log/smart_detections.log`.

## Motion detection (`ubnt_smart_motion`)

Defaults (`smartmotion-default-response.json`): `lingerEventStartMSec` 1000,
`lingerEventStopMSec` 3000, `eventMaxDurationMSec` 300000, `sendEvents` 2,
`sendPulse` 2, `interruptTimeoutMs` 5000, `bgmodel: "default"`, zone `level` 50.
It sends `EventSmartMotion` start/stop and periodic pulses (`pulsePeriodMs`) with
per-zone `levels`.

## Other files worth a look

- `/etc/changevideosetttings-default-response.json` — a real `ChangeVideoSettings`
  reply. pi-port's own reply is a guess.
- `/usr/etc/newplat/ispserver-default-response.json`,
  `osd-default-response.json`, `audioevents-default-response.json`.
- `/etc/features.conf` — `motionDetect: ["enhanced"]`, `privacyMasks`,
  `squareEventThumbnail`, `videoCodecs: h264/h265/mjpg`, `edgeRecording: true`.

## What this means for pi-port

1. **Timing should come from settings, not constants.** A track should open after
   `eventStartMSec` and close `eventStopMSec` after the object was last seen (default
   1 s / 3 s). Protect sends both in `ChangeSmartDetectSettings`. pi-port's
   `AIPORT_MISS_S = 3` matches the default by coincidence. The G5 Bullet's ~2 s may
   mean Protect sends a different value.
2. **Add motion detection per camera.** The AI Port runs `ubnt_smart_motion` for
   each paired camera, and Protect ignores the camera's own motion events.
   Implementing `EventSmartMotion` with `lingerEventStartMSec`/`StopMSec` and pulses
   closes gap 2 in [protect-aiport-controller.md](protect-aiport-controller.md#gaps-in-pi-port).
3. **Tracking could move closer to the real one:** two-pass IoU + Hungarian matching
   (high- then low-confidence), lost tracks turning stationary after a timeout, and a
   warmup period.
4. ~~**Report points the way the AI Port does.**~~ Done: `avclient.py` uses the
   measured tiers for `usedPoints`, `GetStreamList` and `GetUiStreamPoints`, and
   handles `ResetAIPortStreams`.
5. **Use the real `ChangeVideoSettings` reply shape** from the default-response file.
