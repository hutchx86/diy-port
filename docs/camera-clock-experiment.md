# Camera-clock experiment

Status: done (2026-10-18). Protect places boxes by `clockWall` only, so the camera's clock
doesn't help; the experiment's code is off unless `AIPORT_CAMERA_CLOCK` names a camera.

Can Protect place boxes from the camera's own clock instead of a fixed `AIPORT_BOX_DELAY_MS`?

## What we know

- Protect's RTSP relay passes on a native UniFi camera's RTCP sender reports. Their clock is
  the camera's uptime: 339676 s on Camera A and 843376 s on Camera C, steady to about 2 ms.
  Camera B, which isn't a UniFi camera, gets a relay clock (413 s, 0.2 s of jitter).
- A native camera's `EventSmartDetect` carries `clockMonotonic`/`clockStream` in ms on that
  clock (seen: `clockStream 69938862`, `clockMonotonic 69964728`).
- No wall-clock capture time anywhere else: no RTP header extensions, no SEI, and only a
  whole-second `Date:` header in the RTSP replies.
- ffmpeg's frame timestamps start at zero for each session, even with `-copyts`.

## Phase 1: does Protect use these clocks at all?

`AIPORT_CAMERA_CLOCK=<deviceID>[,...]` starts `camera_clock.Reader` for those cameras: its own
RTSP session to the relay, reading sender reports and keeping
`offset = local wall ms - camera ms`. `clockMonotonic` and `clockStream` become
`frame stamp - offset` (off by the frame's decode time, fine for this test).
`AIPORT_CLOCK_WALL_SKEW_MS` (`"<ms>[,<deviceID>=<ms>]"`) shifts `clockWall`.

- **Variant A:** camera clock on, no skew. Walk past Camera A; check playback.
- **Variant B:** camera clock on, `AIPORT_CLOCK_WALL_SKEW_MS=02000000000A=5000`. Walk again.
  If the boxes are still about right, Protect places them by the camera clock: go to Phase 2.
  If they are 5 s late, Protect uses `clockWall`: stop, the fixed delay stays.

## Phase 2 (only if Phase 1 passes)

pi-port does its own RTSP pull and feeds ffmpeg through a pipe, so each frame's RTP time is
known (UniFi streams have no B-frames: output frame n is input frame n). Each frame's camera
time is its RTP time mapped through the latest sender report; send that as
`clockMonotonic`/`clockStream`, derive `clockWall` from it, and set the box delay to 0 on those
cameras. To handle:
- RTP timestamps wrapping every 13.25 h
- a camera reboot resetting its uptime
- the relay restarting a session
- no report yet (fall back to today's clocks)

Tests replay a clip recorded with its sender reports.

## Result (2026-10-18, Camera A)

- **Variant A** (camera clock, no skew), walk at 19:29: boxes early in playback, as with the
  normal clocks; not the ~1.5 s late that placement by the camera clock would have given.
- **Variant B** (camera clock, `clockWall` +5 s), walk at 19:32: boxes late.

Protect places boxes by `clockWall`; the camera's clock in `clockMonotonic`/`clockStream` doesn't
change where they're drawn. Phase 2 is not worth doing. The experiment is off (neither setting
in `~/.piport.env`); the code stays, unused, for a later look.

Walk A also measured Camera A's real delay: the event's first box was at 19:29:54.007 in
playback, and the person appears at 19:29:55.307, 1.3 s early with the 1500 ms default. The
Camera A's residual is about 0.2 s, so its delay is now 200 ms (`02000000000A=200`).
