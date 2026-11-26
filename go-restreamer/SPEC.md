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
- `internal/audio`: normalize to AAC-LC: passthrough AAC and (by default) G.711 copy; Opus,
  and G.711 with `--g711 aac`, → AAC via a supervised `ffmpeg` subprocess (no CGo); silent
  AAC fallback (pre-encoded frames, 16 kHz mono, ASC `0x14 0x08`) when there's no audio track
  or `audio_enabled=false`, paced on video PTS.
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
