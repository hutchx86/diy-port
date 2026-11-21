# ONVIF video modes

How an ONVIF camera's video reaches Protect, and how to choose. Set `AIPORT_ONVIF_MODE` in the
instance's `diyport.env`, then `touch ~/.diy-port/<instance>/restart`. This briefly interrupts
all of that instance's cameras.

| Mode | Video | Pushes run in | Camera sessions | Cost per camera |
|---|---|---|---|---|
| `copy` (default) | ffmpeg copies the streams | `avclient.py` | 3 | About 1% of a core, 11-22 MB |
| `transcode` | GStreamer encodes each channel | `avclient.py` | 1 (2 with shared decode off) | About 8-15% of a core, 130-290 MB |
| `isolated` | As `transcode` | A process per camera | 1 (2 with shared decode off) | As `transcode`, plus 15 MB |
| `go` | A Go sidecar (`go-restreamer/`, run by `run_all.py`) pulls and pushes the proven ffmpeg-era channels | The sidecar | As `copy` | |

Move one camera at a time: `copy,<deviceID>=isolated`.

In go mode this process only sends the sidecar `configure_camera` / `remove_camera` over a
unix socket (`restreamer.sock` in the state dir); shared decode reads the sidecar's DetectServer
at the same `detect_socket()` path. It needs no gst-launch.

The transcode needs GStreamer. Without it, the camera logs an error and uses `copy`:

```bash
brew install gstreamer
for e in vtdec vtenc_h264 rtspsrc rtph264depay flvmux h264parse videoscale videorate videoconvert; do
  gst-inspect-1.0 --exists $e && echo ok $e || echo MISSING $e
done
```

A root daemon should also test the encoder as root:

```bash
sudo /opt/homebrew/bin/gst-launch-1.0 videotestsrc num-buffers=100 ! video/x-raw,width=640,height=360 ! vtenc_h264 ! fakesink
```

For a transcoded camera:

- Healthy: one `transcode ... started` line with three sizes, and `pull ... video1 over 60 s` at
  the camera's rate.
- Take it off the transcode: repeating `stalled` or `restarting`, zero frames, or sustained frames
  below the camera's rate.
- Set `AIPORT_BOX_DELAY_MS=0` for these cameras: shared decode (on by default) gives boxes
  each frame's time from the video pushed to Protect. With `AIPORT_ONVIF_SHARED_DECODE=0`,
  recalibrate the box delay by checking boxes in playback instead.
- Watch CPU, memory and swap as cameras add up. They share the hardware media engine with
  anything else using it, such as Frigate.

## Moving a camera between AI Ports

Remove it from the old AI Port before pairing it to the new one: pairing fails silently if it's
still paired elsewhere. Protect issues new stream names, so expect about a minute of
`ended after ... BrokenPipeError` before it settles. The camera's per-camera settings don't
follow it; add them to the new instance's `diyport.env`.

## Implementation notes

The mode is `AIPORT_ONVIF_MODE`: a default, then per-camera exceptions such as
`isolated,AABBCCDDEEFF=copy`, so cameras move one at a time.

`copy` (the default) has ffmpeg pull the camera's main stream and substream and copy them;
pushes run in `avclient.py` and it works anywhere.

`transcode` follows the real AI Port. Its `ubnt_av_aiport` (v5.1.12, Ghidra) pulls the main
stream once with `rtspsrc protocols=tcp latency=500 tcp-timeout=5000000`, decodes it once
(`v4l2dec`) and re-encodes one stream per channel (S0 with a 1 s GOP, S1 GOP 24 at 2 Mbps,
S2 at 300 kbps), so every channel Protect plays was made by its own encoder at a steady
rate. Here one `gst-launch` does the same with VideoToolbox (`vtdec`, one `vtenc_h264` per
channel), each channel on its own pipe: one RTSP session per camera instead of main +
substream. `video2` is encoded at the substream's size and `video3` at
`TRANSCODE_VIDEO3_HEIGHT`, as Protect is told. macOS only; needs `brew install gstreamer`
(1.28.8 tested). Live 2026-11-03/04 on Driveway: more stable than ffmpeg or a copied
GStreamer pull, with a better picture, and it held 20 fps for 10 h 48 min.

It also sends clock syncs as yi-protect's `FlvPush` does, matching a real G3: `onMpma` +
`onClockSync` at connection start (after `onMetaData`, before the codec configs), then
`onClockSync` + `onMpma` before every key frame, 10 s fallback. Live 02:22 on Driveway
(2026-11-03): Protect plays this camera through an fMP4 player with `useWallClock=1`
(unifi-cam-proxy's Driveway gets a UBNT one), and with a sync every 5 s a cold start played
~1 s then paused ~5 s, as if waiting for the next sync to place frames on the wall clock.
A sync every 1 s was worse; G3 sync was "very stable".

## Known limits

- A wedged `copy` pull logs `restream: no video to Protect`, but only a transcode has a pipeline
  restart. A quick reconnect loop may also hide from that warning.
- Starting several cameras at once can send Protect a fallback `1920x1080` reply before the right
  size. A later reply corrects it.
- Restarts drop the recording segment in progress on the main channel, so expect a gap of up to
  about 9 s in that channel's recording.
- If Protect re-sends the settings for every configured channel at once, each push opens a
  fresh Protect connection in place; the shared gst pull and its DetectServer stay up, so
  detection does not pause for the re-send.
- `transcode`/`isolated` need an H.264 main stream. A probed H.265 camera logs one
  error and uses `copy`; the copy push still rejects HEVC with its normal message.
- `isolated` only contains failures one way: if `avclient.py` dies, the pushes stop too.
  With shared decode on, detection reads the pushed video1, so a restream process crash
  also pauses detection until its replacement starts.
