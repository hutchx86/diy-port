# Frame timestamps and video time sync: changes required in pi-port

Status: proposed (2026-10-20). Changes 1 to 3 are not in the code: a trial of change 3
(frame times from the stream's timestamps) was rolled back on 2026-10-23. Companion to
[protect-timesync.md](protect-timesync.md), which covers putting pi-port's clock on
Protect's time. That one isn't needed while the pi-port host is NTP-synced (see
"Order" below). Change 3 here removes per-frame jitter. Change 4 (RTCP) was tested on a
native and an emulated camera and can't replace the hard-coded `AIPORT_BOX_DELAY_MS`
through wall time; a `clockStream` lead there is untested.

Protect places smart-detect boxes in playback using the times on each event:
`clockWall`, `clockBestWall`, `clockStream` and `clockMonotonic`. This page sets out how
the real AI Port arrives at those times, what pi-port does now, and what needs to change
for them to be accurate.

## How the real AI Port does it

Observed in the aiport-fw lab (firmware v5.1.12, offline and with a live Protect camera)
and in real-camera WebSocket captures:

- **Frames are stamped on the local clock as they're decoded.** The per-camera ingest
  (`ubnt_av_aiport`) writes each decoded frame to shared memory with a descriptor that
  holds `seq` and a `pts` in `CLOCK_MONOTONIC` nanoseconds: the GStreamer system clock,
  taken about 0.7 s before `ubnt_smart_detect` read the frame. It isn't the RTSP/RTP time
  or the camera's own clock.
- **`smart_detect` keeps track of frame times itself.** Its "time tables"
  (`smart_cv_utils.h:GetFrameTimes`) are built from the frames it reads. Nothing else
  feeds it: with a live Protect camera, 60 s of tracing showed no IPC received. When a
  frame isn't in the tables it logs "Target time not found in time tables, use current
  time instead", which happened once per process start, on the first frame.
- **Event logic runs on monotonic milliseconds:** start and stop delays
  (`eventStartMSec`, `eventStopMSec`), the 500 ms track check, and staleness.
- **Wall time comes from Protect.** `ubnt_avclient` syncs with the console
  (`ubnt_avclient_timeSync`) before its hello and sets the system clock from it, then
  stops `systemd-timesyncd` after adoption. See [protect-timesync.md](protect-timesync.md).
- **Not confirmed:** the time fields in an actual `EventSmartDetect` from the firmware.
  The lab can't produce real detections, so none was ever sent.

## What pi-port does now (`piport/avclient.py`)

Already matches the firmware:

- One ffmpeg process per camera pulls RTSP over TCP, decodes it, cuts it to
  `DETECT_FPS`, and splits it into a full-resolution JPEG and a 640x640 RGB detection
  frame (`_detect_filter_graph()`, around line 533; the command line around line 553).
  Both outputs are numbered the same (`seq`).
- The RGB reader stamps each frame with `_now_ms()` as it leaves ffmpeg
  (`_latest_frame_time`, around line 489).
- The detector copies that stamp for the frame it analyses (`_analysed_frame_time`,
  around line 597). `_frame_wall_ms()` turns it into the event's `clockWall`, minus
  `AIPORT_BOX_DELAY_MS`. Events describe the analysed frame, not the moment they're sent.
- The best-frame JPEG is matched to the same `seq` (`_frame_jpegs`, `_frame_jpeg()`).

The gaps:

1. The clock is the host's, not Protect's (see [protect-timesync.md](protect-timesync.md)).
2. **The stamp is arrival time, not frame time.** It's the frame's capture time plus
   Protect's relay, RTSP over TCP, decode, the `fps`, `split` and `scale` filters, and the
   pipe. That's tens to hundreds of ms, and it varies from frame to frame.
   `AIPORT_BOX_DELAY_MS` removes a fixed amount only.
3. **`clockStream` is a copy of `clockMonotonic`.** `_stream_clocks()` (around line 1961)
   returns monotonic ms since the pull started for both. Real cameras send them
   separately (a native camera's `EventSmartDetect` had `clockStream` 69938862 against
   `clockMonotonic` 69964728). What the AI Port firmware sends hasn't been captured, so
   it's not known whether this matters.
4. **The tracker measures durations in wall ms.** `_motion_state` (`started_ms`, the
   confirm and miss windows, around line 1277) uses `_frame_wall_ms()`, so a step in the
   wall clock (for example a time-sync correction) distorts live tracks.

## Changes

### 1. Put the clock on Protect's time (only if the host isn't NTP-synced)

Implement [protect-timesync.md](protect-timesync.md): the `ubnt_avclient_timeSync` batch
at connect and every 600 s, with the measured offset applied in `_now_ms()` and
`send_msg()`. Without it, `clockWall` is only as good as the host's NTP. With NTP on the
host and the console, the two clocks are normally within a few to tens of ms on a LAN:
less than one frame at `DETECT_FPS` (100 ms at 10 fps) and less than the decode jitter
in the arrival stamp. So skip this while the host runs NTP. Not measured directly: the
lab ran on the Mac's NTP time and the captures measured real cameras, not the pi-port
host.

### 2. Tracker durations on monotonic time (only with 1)

Move the tracker's timing (`started_ms`, the confirm and miss windows, track ages,
staleness, `first_shown_ms` used as a duration) to the monotonic part of the frame stamp
(`_frame_time(device_id)[1]`), and keep wall time only for the values sent to Protect.
That's how the firmware's event logic works, and it means a clock correction can't
open, close or stretch a track. Without change 1, the only steps come from the host's
NTP, which slews small corrections, so this can wait for change 1.

### 3. Frame times from the stream's own timestamps (removes jitter, not the delay)

Get each frame's stream timestamp out of ffmpeg and stamp frames from it, not from
arrival. This removes the frame-to-frame variation in decode, filter and pipe time. It
does **not** replace `AIPORT_BOX_DELAY_MS`: the anchor below is taken from arrival
times, so `clockWall` stays late by the smallest pipeline delay (Protect's relay, RTSP
over TCP, decode, filters, pipe). The box delay remains a constant to calibrate, but a
steadier one.

**A small frame table** is needed for this, because ffmpeg reports the timestamps on a
different channel (stderr) from the frames (the pipes):

- **Keep the source timestamps through the frame-rate cut.** The `fps` filter
  regenerates timestamps on a fixed 1/`DETECT_FPS` grid (output frame k gets k/fps), so
  a `showinfo` after it reports a frame counter, not the source frame's time. Cut to
  `DETECT_FPS` with a filter that passes the original timestamps through, e.g. `select`
  on `pts` (keep a frame when it's at least 1/`DETECT_FPS` after the last kept one),
  then `split`, and add `showinfo` on the detection branch after it. `-fps_mode
  passthrough` on the outputs stops ffmpeg duplicating or re-timing frames. `select`
  doesn't duplicate frames as `fps` does, so a camera running below `DETECT_FPS` gives
  fewer frames rather than repeats.
- `showinfo` logs each frame's number `n` and `pts_time` on stderr at INFO level, so it
  needs `-loglevel info` (or a per-filter log level). Today's `-loglevel warning` hides
  it.
- A stderr reader thread parses those lines into a per-camera ring buffer:
  `seq -> (pts_ms, arrival_wall_ms, arrival_mono_ms)`, kept to a few seconds of frames
  (about `DETECT_FPS * 5` entries). Keep other stderr lines going to the log as they do
  now.
- When the RGB reader takes frame `seq`, it looks that frame up in the table. `showinfo`
  counts from 0 and the reader from 1, so align them. If the entry isn't there yet (a
  race between the two pipes), wait briefly, or fall back to the arrival stamp as now:
  the same fallback the firmware has.
- Compute the frame's times from it:
  - `anchor` = wall time at arrival minus `pts_ms`, fixed from the first frames of the
    pull (take the smallest difference over the first second: the frame that waited
    least). Re-anchor slowly towards the minimum of the last few seconds, to absorb drift
    between the camera's clock and the host's without adding jitter.
  - `clockWall` = `anchor + pts_ms - AIPORT_BOX_DELAY_MS`. Recalibrate the box delay
    after this change: it no longer has to cover the average jitter, only the smallest
    pipeline delay.
  - `clockStream` and `clockMonotonic`: leave as they are. A `pts` counted from the
    start of the pull is close to what `_stream_clocks()` sends today, and what the
    firmware sends as `clockStream` hasn't been captured, so there's no evidence that
    changing it helps.
- Reset the table and the anchor whenever the pull restarts (ffmpeg's timestamps
  restart too).
- `seq` already ties the JPEG to the detection frame, so `clockBestWall` can come from
  the same table entry as the best frame's detection.

Check it by lining boxes up in playback against a fast-moving object, before and after,
with the box delay recalibrated.

### 4. Frame times through RTCP sender reports (tested: doesn't replace the delay)

The idea: Protect's RTSP relay sends RTCP sender reports, which pair an NTP wall time with
an RTP timestamp. Mapping each frame's RTP time through them would give the time Protect
assigned to that frame, with no pipeline delay, and so no `AIPORT_BOX_DELAY_MS`.

**Tested 2026-10-20** with a small RTSP client (RTP over TCP) on camera `02000000000D`
through `rtsp://<console>:7447/<alias>`, 60 s, 1201 frames, 13 sender reports, on a
Mac within 2 ms of NTP:

- **The sender reports carry no wall time.** Their NTP field read 42,558.6 s after
  1970-01-01 (11.82 h), and the RTP timestamp was 11.82 h at 90 kHz: the relay fills the
  NTP field from the stream's own clock (RTP / 90,000, counted from 1970). The two agreed
  to 0.0 ms across all 13 reports.
- So mapping frames through them gives the stream's timeline, not a time on Protect's
  clock: arrival minus mapped time was a constant date offset. **This can't replace
  `AIPORT_BOX_DELAY_MS`.**
- Side results: the stream is a clean 20 fps timeline (49.3 ms per frame by RTP), and
  frames arrived with 23 ms standard deviation of jitter (575 to 731 ms around the
  constant; 10th to 90th percentile 49 ms). That's the size of what change 3 would
  remove, small next to a 1.5 s box delay.

**Native camera, same day:** a native Ubiquiti camera (HEVC, 1504x2016, 24 fps) through
Protect's plain RTSP relay (port 7447, same alias as its `rtsps://...:7441/...?enableSrtp`
address, which encrypts RTP and RTCP), 60 s, 1441 frames, 13 sender reports. Same result:
the NTP field read 438,357 s after 1970-01-01 (121.77 h), and the RTP timestamp 2.46 h at
90 kHz, which is the same value after nine 32-bit wraps (every 13.26 h). They agreed to
0.0 ms. Jitter: 21 ms standard deviation, 10th to 90th percentile 447 to 490 ms around the
constant. **For native and emulated cameras alike, the relay's sender reports carry the
stream's own clock, not wall time.**

**What the clocks are on a native camera** (from the real-camera captures, a G5 Bullet,
two connections): `clockMonotonic` is the camera's uptime in ms (it tracks the hello's
`uptime`, 0.6 to 1.2 s behind arrival because it's the analysed frame's time), and
`clockStream` = `clockMonotonic` − 21,066 ms, constant to the ms across every event and a
reconnect: the same monotonic clock, offset by when the video timeline started.
`clockStreamRate` is 1000 (ms). The emulated cameras in the same captures (unifi-cam-proxy)
send seconds while claiming rate 1000, keep `clockStream` = `clockMonotonic`, and their
`clockWall` runs 1.2 to 1.4 s behind arrival. pi-port sends ms since the RTSP pull started
for both.

**Lead, not tested:** the NTP field gives the camera's stream clock with RTP's wraps
restored: days long for the native camera, so probably counted since it started
streaming. Native cameras send a `clockStream` of the same kind in `EventSmartDetect`
(69,938,862 ms = 19.4 h in one capture). If `clockStream` is that same stream clock,
pi-port could send each analysed frame's stream time, taken from the RTP timestamp
unwrapped with the sender report, as `clockStream`, and Protect, which joins tracks to the
media timeline with it, might place boxes exactly without any delay. To check: compare
a native camera's RTSP stream clock (from the probe) with its uptime as Protect shows it.
`clockStream` would be about uptime minus a few tens of seconds. Or, better, capture the
camera's `EventSmartDetect` and its RTSP sender reports at the same moment. The camera
tested by RTSP read 121.77 h; its uptime wasn't checked. The probe was a standalone script: OPTIONS,
DESCRIBE, SETUP with `RTP/AVP/TCP;interleaved=0-1`, PLAY, then logging RTCP type 200
packets (NTP and RTP fields) and each frame's RTP timestamp with its arrival time.

Using the sender reports at all would need PyAV or GStreamer in place of the ffmpeg
subprocess (the ffmpeg command line doesn't expose them), or a small RTSP/RTP reader
alongside ffmpeg.

## Order

1. `AIPORT_BOX_DELAY_MS` stays: the RTCP test (change 4) found no wall time in Protect's
   relay sender reports, for native and emulated cameras. The `clockStream` lead under
   change 4 is the only open route to dropping the delay; check it before building
   anything on it.
2. Change 3 if the remaining jitter matters: it removes tens of ms of frame-to-frame
   variation (23 ms standard deviation measured at the network, plus decode and pipe
   variation on top), so the box delay can be calibrated tighter.
3. Changes 1 and 2 only if the host can't run NTP or is seen to drift from the console.

## Tests (`piport/tests/test_avclient.py`)

- `showinfo` parsing: lines into `seq -> pts_ms`, including the counting offset and a
  line that arrives after its frame.
- Anchor: the smallest arrival-minus-pts over the first second wins; a slow re-anchor
  step stays bounded; a pull restart resets it.
- `clockWall` = anchor + pts minus `AIPORT_BOX_DELAY_MS`; `clockStream` and
  `clockMonotonic` unchanged.
- The frame-rate cut keeps source timestamps: frames from a stream with uneven source
  times keep those times (not a 1/`DETECT_FPS` grid).
- Fallback: a frame with no table entry uses the arrival stamp.
- Tracker: a step in the wall clock while a track is open doesn't change when it
  confirms or ends.

## Docs

When these land, update [aiport-interface.md](aiport-interface.md): how `clockWall`,
`clockBestWall`, `clockStream` and `clockMonotonic` are derived.
