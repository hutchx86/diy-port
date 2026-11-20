<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
# go-restreamer: build spec (condensed)

A Go sidecar for diy-port ONVIF cameras: pulls each camera's RTSP **once** and pushes the
streams to Protect, as `onvif_push.py` does today with ffmpeg/GStreamer. Python supervises it
(`run_all.py`) and drives it over JSON-RPC on a Unix socket. Selected per camera with
`AIPORT_ONVIF_MODE=go` (same "<default>[,<deviceID>=<mode>]" syntax as the existing modes).

## Ground truth
`diyport/onvif_push.py` (+ `onvif_restream.py`, `shared_decode.py`) is what Protect is proven
to accept. Where this spec and that code disagree on the wire (handshake, FLV tags, metadata
fields, timestamps, channel names, how video1/2/3 map to camera streams, audio), **the
Python wins**. Read it before writing wire code.

## Packages (module `diyport/go-restreamer`, Go 1.27, darwin/arm64 first, Linux must build)
- `internal/media`: shared types (`Tracks`, `VideoAU`, `AudioFrame`, `Subscriber`). Fixed
  contract; don't change it without need, and keep changes additive.
- `internal/flv`: AMF0 onMetaData, Annex-B→AVCC, AVCDecoderConfigurationRecord, the
  extendedFlv writer, and `Pusher` (a `media.Subscriber`) that dials `dest_host:dest_port`,
  waits for an IDR, writes header/metadata/AVC+AAC sequence headers, then tags with
  ms timestamps from one base PTS (first IDR), clamped monotonic. Reconnects with backoff.
- `internal/audio`: normalize to AAC-LC: passthrough AAC; G.711/Opus → AAC via a supervised
  `ffmpeg` subprocess (no CGo); silent AAC fallback (pre-encoded frames, 16 kHz mono,
  ASC `0x14 0x08`) when there's no audio track or `audio_enabled=false`, paced on video PTS.
- `internal/rtsp`: `Pool` keyed by exact rtsp_url, refcounted, one gortsplib v4 client per
  URL (TCP transport) fanning out to subscribers; reconnect 1,2,4..15 s keeping subscribers.
- `internal/transcode`: H.265 input, or `mode: "transcode"`: one supervised
  `ffmpeg -hwaccel videotoolbox` per camera that decodes once and encodes H.264
  (`h264_videotoolbox -realtime 1 -bf 0 -profile:v main -g <fps>`) for each channel's profile,
  re-emitting `media.VideoAU`s as a `media.Subscriber` chain. Linux: probe `h264_rkmpp` /
  `h264_vaapi`, else HEVC fails with `ERR_UNSUPPORTED_CODEC_HEVC`, H.264 falls back to copy.
- `internal/rpc` + `cmd/restreamer`: newline-delimited JSON-RPC 2.0 on `--sock` (no default
  in /tmp: several instances run per Mac, Python passes a per-instance path under the
  instance's state dir). Flags `--sock --hwaccel auto|videotoolbox|none --log-level`.
  Methods: `configure_camera` (idempotent desired state, params as below), `remove_camera`,
  `get_stats` (per channel: bytes sent, last_frame_age_ms, reconnects, RTSP sessions, hw
  status). SIGTERM: close everything cleanly. Exit if the socket's parent dies is Python's job.

```json
{"device_id":"AABBCCDDEEFF","mode":"copy|transcode",
 "shared_decode":{"enabled":true,"detect_fps":10},
 "channels":[{"channel":"video1","stream_name":"video1","rtsp_url":"rtsp://u:p@h/…",
   "dest_host":"192.0.2.1","dest_port":7550,"audio_enabled":true,
   "width":0,"height":0,"bitrate_kbps":0}]}
```
Channel fields beyond the RPC spec (`width/height/bitrate_kbps`, 0 = source) are for transcode
profiles. If Python's existing push needs more per-channel fields (keys, tokens, extra
metadata), add them; Python and Go agree via this file and `internal/rpc/protocol.go`.

## Shared decode (deviation from the original spec: no POSIX shm)
diy-port already does shared decode: `shared_decode.py` reads the pushed video1 from
`onvif_push`'s **DetectServer** socket. The Go restreamer serves the **same DetectServer
protocol** for video1 (path given in `shared_decode.socket` in `configure_camera`), so
`avclient`/`shared_decode.py` work unchanged. Match the Python DetectServer byte for byte.

## Rules
- Every source file starts with `// SPDX-License-Identifier: AGPL-3.0-or-later` (Python: `#`).
- Never log credentials: redact `user:pass@` in every logged or returned URL.
- No CGo. ffmpeg is at `/opt/homebrew/bin/ffmpeg` on the Mac; find it on PATH.
- Tests: `go test ./...` with table tests for AMF0, AVCC, AVCDecoderConfigurationRecord,
  pool dedup (fake dialer), RPC. Hardware tests behind `//go:build darwin && arm64` and skip
  when ffmpeg lacks videotoolbox.
- Dense explanatory comments in the repo's style; no speculative abstractions.

## AI Port exact mode (`mode: "aiport"`, Python `AIPORT_ONVIF_MODE=go-aiport`)
A second, switchable push path that rebuilds the real AI Port's ONVIF video chain from the
decompile in `docs/aiport-onvif-push-spec.md` (firmware v5.1.12). `go` stays the proven
ffmpeg-era behaviour above; enable this per camera with `AIPORT_ONVIF_MODE=go-aiport` (same
`<default>[,<deviceID>=<mode>]` syntax, e.g. `go,AABBCCDDEEFF=go-aiport`). Python sends
`mode: "aiport"`; the sidecar rebuilds the camera if its mode crosses that boundary.

- **Ingest and encode** (spec 2-3): one RTSP pull of the main stream (every channel uses the same
  URL), decoded once, three encodes through `internal/transcode` (VideoToolbox on darwin, still
  builds on Linux; no hardware encoder: H.264 is passed through at source size, H.265 fails):
  video1 source size (short side capped at 2160) at the console's fps (else the camera's), GOP
  one second, bitrate from the console (default 8 Mbps); video2 width 1280 (portrait: height),
  aspect kept, even, 24 fps, GOP 24, 2 Mbps; video3 width 640, 24 fps, GOP 24, 300 kbps.
  Resampled profiles stamp AUs on their own 1/fps grid (`Profile.FPS`, `FitWidth`,
  `MaxShortSide`). Audio: one shared ffmpeg per camera decodes any codec to AAC 48 kHz mono
  128 kbps (`audio.NewEncoderAAC`) and, when Protect asks `withOpus`, Opus at `opus_sample_rate`
  mono 20 ms; no audio track at all when the camera has none (header flags 0x03).
- **Push** (`flv.AIPortPusher`, spec 4-6): AMF0 objects with byte-wise sorted keys, header flags
  0x07/0x03, onMetaData with exactly the spec's 13 keys (audio numbers are the streamer's
  template 64000/1/16000), tag ts = dts_ms - baseDtsMillis (base from the first video frame, kept
  across reconnects), 16-byte trailers `u32 timescale, u32 cts, u64 absolute pts ticks`
  (video 90000, AAC 48000, Opus = opusSampleRate, script tags the timeline's last pts). Per key
  frame: onMpma, onClockSync, config tag only if SPS/PPS changed, frame; non-key frames get a
  throttled onMpma (500 ms if a value changed, else 10 s). No timer-driven onClockSync. Config
  tags once per connection (AVC `17 00`, AAC `AF 00 11 88`, Opus `CF 00 03 <rateTag>`); a
  reconnect sends header+onMetaData, video, AAC and Opus configs, then resumes at the next key
  frame. One TCP per channel, flat `retry_interval_s`, `connect_timeout_s` (1..60), IP_TOS 184,
  5 MiB output queue dropping whole frames (log at most every 30 s). The console's feedback
  frames (`u16 type, u16 len, payload`) are read on the same socket: type 4 gets an `onRtt`
  reply, 3 and 5 are logged, the rest ignored.
- **onMpma values** are a guess (spec 4.5, open question 5): every cur/max/min slot and `t` hold
  the channel's configured bitrate in bit/s; `r` is 0.
- **Channel fields** used only here: `fps`, `retry_interval_s`, `connect_timeout_s`, and
  `bitrate_kbps` (video1's console bitrate; a change is applied with `SetBitrate`, no restart).
- Not implemented: an H.265 hvcC record (the path always re-encodes to H.264), runtime lq_fps
  lowering to 8, feedback type 0-2 adaptation.
