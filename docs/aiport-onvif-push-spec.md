<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
# AI Port v5.1.12 ONVIF path: implementation spec for go-restreamer

Tags: **[D]** decompiled, **[S]** strings or rootfs files, **[I]** inferred.
Addresses are Ghidra offsets. S: = `ubnt_streamer`, AV: = `ubnt_av_aiport`, OD: = `ubnt_onvifd`.
Decompile outputs referenced below (not in the repo): `e1`..`e5`, `d1`..`d15`, `a1`, `a2`, `o1`, `container_range.c`, `transport_range.c`.

**Corrections to an earlier draft**
- The ForceIDR-on-connect point was overstated. The streamer calls `Encoder::ForceIDR` on connect (S:FUN_001a4890 -> FUN_0018f970), but no ForceIDR consumer exists in `ubnt_av_aiport`, `libgstubntstream` or `libgstubntfpscontrol` [S: no matching strings]. The only `cid,force_key_frame=1` in AV is the recording-stop path (FUN_001137dc).
- The real stream therefore joins on a natural IDR, which the encoder GOPs make about 1 s (see §Encode). Our diff is "real GOPs are 1 s; ours follow the camera's GOP", not "real forces an IDR".
- `onClockSync` and `onMpma` objects are not in the order first assumed. All AMF objects are alphabetical (see §FLV).

## 1. Process model

```
Protect ws -> ubnt_avclient --IPC unix--> ubnt_onvifd -> /usr/bin/ubnt_av.sh -> systemctl restart ubnt_av_aiport@<mac>
ubnt_av_aiport@<mac>: rtspsrc -> hw decode -> 3x hw encode + audio encoders -> ubnt_stream shm sinks
  -> ubnt_av_dep_mod.sh -> systemctl restart ubnt_streamer@<mac> (+ audio_events@, smart_detect@)
ubnt_streamer@<mac>: reads shm, writes extendedFlv over tcp://console:7550, one TCP per video channel
```
- IPC addressing is `<app>.<mac>` (avclient `SendRequestToMultiCamera`, `/etc/persistent/cameras.conf`; `ubnt_av_adjust.sh` uses `ubnt_ipc_cli -T=ubnt_av_aiport.<mac>`) [S].
- Units [S]:
  - `ubnt_av_aiport@.service`: `ubnt_av_aiport -i=%i`, Restart=on-failure, WatchdogSec=60.
  - `ubnt_streamer@.service`: `ubnt_streamer -c=/tmp/ubnt_streamer_sysid_a5f1.%i.json -m=%i`, Restart=always, WatchdogSec=20.
- `ubnt_av.sh` [S]:
  - Writes `/tmp/ubnt_av_aiport.<mac>.conf` (and a copy under `/etc/persistent/`) as `{uri, video_encode, raw_data, lq_fps, yuv_fps}`.
  - Copies `/etc/features.conf` to `/etc/<mac>.features.conf`.
  - Restarts `ubnt_av_aiport@<mac>`.
- `ubnt_av_dep_mod.sh -i mac [-v] [-a] -h WxH -m WxH -l WxH -e codec` [S]:
  - Defaults are HQ 3840x2160, AQ 1280x720, LQ 640x360.
  - It jq-edits `/etc/ubnt_streamer_sysid_a5f1.json` into `/tmp/ubnt_streamer_sysid_a5f1.<mac>.json`: vin0 size, the three video sizes, the codec, and `audio.enabled`.
  - It restarts the streamer. It stops `audio_events@` when there is no audio.
  - AV runs it on first streaming and again (`-o restart`, FUN_001192b0) whenever the decoded caps change, i.e. a resolution change restarts the streamer.

## 2. Ingest (AV)

- One RTSP session on the camera main stream (single `uri`). Properties (FUN_001124f8) [D]:

| Property | Value |
|---|---|
| `protocols` | 4 (tcp) |
| `latency` | 500 |
| `tcp-timeout` | 5000000 µs |
| `teardown-timeout` | 2000000000 ns |

- Video chain [D/S]: `rtspsrc ! rtph264depay|rtph265depay ! h264parse|h265parse ! v4l2h264dec|v4l2h265dec ! dec_queue ! videorate ! capsfilter(framerate=24/1) ! capssetter ! v4l2convert ! capsfilter(WxH,NV12) ! convert_queue ! ubnt_mask(pmask) ! tee video_tee`.
  - The `v4l2convert` I/O modes are output 5, capture 4.
  - The source is normalised to 24 fps by `videorate`. The `validFpsValues` list in the streamer defaults tops out at 24.
- Branches [D]:

| Channel | Elements | Size/fps | shm sink |
|---|---|---|---|
| HQ (video1) | `hq_queue ! ubnt_fpscontrol(hq fps) ! capssetter ! v4l2h264enc\|h265enc hq_v4l2enc ! hq_enc_queue ! ubnt_stream` | source size | `/tmp/<mac>.C0S0`, 8 MiB, 300 frames |
| AQ (video2) | `aq_queue ! aq_videorate ! caps framerate=24/1 ! aq_v4l2convert ! caps 1280x720 ! aq_v4l2enc ! ubnt_stream` | width 1280 | `/tmp/<mac>.C0S1`, 2 MiB, 300 frames |
| LQ (video3) | `lq_queue ! ubnt_fpscontrol(lq_fps) ! lq_v4l2convert ! caps 640x360 ! lq_v4l2enc ! ubnt_stream` | width 640 | `/tmp/<mac>.C0S2`, 1 MiB, 300 frames |
| snapshot | `snap_videorate(2/1) ! v4l2jpegenc` | | `/tmp/<mac>.C0S3`, 4 MiB, 2 frames |
| YUV | `yuv_fps_control ! yuv_v4l2convert ! NV12` | | `/tmp/<mac>.video_yuv`, 0x2398800 bytes, 3 frames |

- Scaling sizes (FUN_001101c8, pad-probe on the first decoded caps):
  - HQ = source dimensions, capped at a long side of 0x870 (2160).
  - AQ = width 1280 (portrait: height 1280).
  - LQ = width 640 (portrait: height 640).
  - The other dimension keeps the aspect via helper FUN_0010e698. Its rounding is not decompiled [I: mod-2 or mod-16].
  - These are written into the `*_capsfilter`/`capssetter` elements, and the YUV size is set to the HQ size.
- Recording branches (`mux_*`, `splitmuxsink`, `mp4mux`) are for edge recording and are not used for the push [S].
- fps: HQ fps (`param+0x44`, from the `fps` in OnvifStreamControl) is applied by `hq_fps_control` (FUN_00111c3c, which also re-writes the HQ GOP). `lq_fps` defaults to 24, and OD lowers it to 8 once a second stream exists. `yuv_fps` is 10, and 5 with two or more streams (FUN_001188c0).
- Resolution change / RTSP error [D]:
  - `GetBusMsg` (FUN_001131fc) treats EOS, "Internal data stream error", "unsupported" and "Pipeline is busy" as relaunch triggers.
  - `Restart` (FUN_00111500) sets the pipeline to NULL, then PLAYING, with a count log.
  - The systemd watchdog (60 s) restarts the process if the loop stalls.
  - The caps probe re-runs `ubnt_av_dep_mod.sh` so the streamer reconfigures (FUN_001192b0).

## 3. Encode (hardware V4L2)

`extra-controls` is built by FUN_00111b20 as `cid,video_gop_size=<gop>,video_bitrate=<bps>,sequence_header_mode=1`. `sequence_header_mode=1` means SPS/PPS come joined to the IDR [D].

| Channel | GOP (frames) | Bitrate | Other |
|---|---|---|---|
| HQ | HQ fps (about 1 s; hq fps = camera fps from OnvifStreamControl) | `param+0x48`, set by `ChangeVideoParams{bitrate}` -> `OnHqBitrateChanged` (FUN_00111e44) | codec h264 or h265 by `video_encode` |
| AQ | 24 | 2,000,000 | fixed 24 fps |
| LQ | lq_fps (24, or 8) | 300,000 | |

- Nothing else is set (profile, level, B-frames, rate control): hardware defaults [S]. The streamer template says M=1, N=30, nMultiplier 5, `isCbr` false.
- Only the HQ bitrate follows the console at runtime. AQ and LQ ignore `bitRateVbrMax` changes [D: only `OnHq*Changed` handlers exist; also `OnLqFpsChanged`/`OnYuvFpsChanged`].
- Effective GOP is about 1 s on all channels, so a new connection waits at most about 1 s for an IDR.
- ForceIDR path: S `Encoder::ForceIDR` sets shm `/forceIDR`. No ingest-side consumer exists [S]. Treat it as best-effort.

## 4. FLV container (S: containerflv.cpp)

### 4.1 AMF encoding rules [D: FUN_00200f80, variant_amf.cpp]
- Every object is an AMF0 object (`03` ... `00 00 09`) written from a `std::map<string, variant>`. **Keys are therefore sorted ascending, byte-wise.**
- Variant types serialize as number (double, `00` + 8 bytes BE), bool (`01 xx`) or string (`02`, u16 length, bytes). Ints are stored as doubles [I].
- Marker 10 (strict array) is used only for array-typed variants, which are not used here.
- Script tag body: `02 <u16 len> <name>` followed by the object.

### 4.2 Header + `onMetaData` (FUN_001cc770, FUN_00198b00 CreateOutStream)

FLV header (13 bytes): `46 4C 56 01 <flags> 00 00 00 09 00 00 00 00`.
`flags = (hasAudio && !suppressAudio ? 0x04) | (hasVideo && !suppressVideo ? 0x01) | (extendedFormat ? 0x02)`. It is 0x07 with audio, 0x03 without.

Object keys, in wire order. Alphabetical, so `audio*` first:

| Key | Type | Source |
|---|---|---|
| `audioBandwidth` | num | audio encoder bitrate (64000 in the template); forced 0 if no audio |
| `audioChannels` | num (float setter) | audio encoder channels (1); 0 if no audio |
| `audioFrequency` | num | audio encoder sampleRate (16000 template); 0 if no audio |
| `channelId` | num | `streamOrdinal`: 0, 1, 2 for video1/2/3 |
| `extendedFormat` | bool | true for the extendedFlv serializer |
| `hasAudio` | bool | audio track exists && !suppressAudio |
| `hasVideo` | bool | video encoder exists |
| `streamId` | num | settings `streamId`: 1, 2, 4 for video1/2/3 (mjpg 8) |
| `streamName` | string | `avSerializer.parameters.streamName`; if empty, `"%s_%u"` of (device/stream name, ordinal) |
| `videoBandwidth` | num | `bitRateVbrMax` of the channel; 0 if no video |
| `videoFps` | num | `fps` (float getter); 0 if no video |
| `videoHeight` | num | channel `height`; 0 if no video |
| `videoWidth` | num | channel `width`; 0 if no video |

- Tag timestamp = `latest_pts_ms - baseDtsMillis` (0 on the first frame). Header and onMetaData are written in one write.
- Defaults (`/etc/changevideosetttings-default-response.json`) give the numbers: video1 3840x2160, bitRateVbrMax 8,000,000, streamId 1; video2 1280x720, 2,000,000, streamId 2; video3 640x360, 300,000, streamId 4; fps 24; tos 184; `name` video0.0/.1/.2.
- No ffmpeg keys (`width`, `framerate`, `duration`, `encoder`, ...) exist.

### 4.3 Tag header and trailer [D: FUN_001c7420, FUN_001c5c30]
Video tag: tag header (11 bytes) + body (`frame/codec byte`, `AVCPacketType`, 24-bit `cts`, then the NAL data) + PrevTagSize. Then the 16-byte trailer, only when extendedFormat.

| Offset | Size | Field |
|---|---|---|
| 0 | 4 | u32 BE timescale (video 90000 = `00 01 5F 90`) |
| 4 | 4 | u32 BE `cts` in ticks of that timescale (0 for audio and script tags) |
| 8 | 8 | u64 BE pts ticks, absolute CLOCK_MONOTONIC |

- FLV `ts = dts_ms - baseDtsMillis`. `baseDtsMillis` is set by the first video frame (audio is dropped until then, FUN_001c7fd0) [D: FUN_001c4c90].
- So for every tag `trailer_ms - flv_ts == streamClockBase`.
- Video first byte: `17/27` H.264 key/inter, `18/28` H.265 (codec id 8), `19/29` AV1. AVCPacketType 1, 24-bit cts in **ms** = `(pts - dts) * 1000 / timescale`.
- Video ticks: `monotonic_ns * 90 / 1e6` (FUN_001abca0). Audio ticks: `ns * 48 / 1e6`, then rescaled to the track timescale (FUN_001aa980).
- Script tags use the timescale and value of the **timeline's last pts** (the max of audio and video seen), so 90000 after a video frame or the audio rate after an audio frame. They are not a fixed 11025.
- Backward dts is skipped. A big backward jump on a key frame resets the timeline ("Force reset timeline", FUN_001cae50), which means a new `baseDtsMillis`.
- Limits: at most 10 slices per frame, "Frame too large" warning above 4 MiB.

### 4.4 Config tags
- **AVC** config: tag type 9, `17 00 00 00 00 <AVCDecoderConfigurationRecord>` (SPS/PPS validated, FUN_001c86c0). H.265 uses `18 00 ...` with VPS/SPS/PPS [I, layout standard].
- **AAC** config (FUN_001c5720), 19 bytes: `08 00 00 04 | 00 00 00 00 | 00 00 00 | AF 00 <ASC1> <ASC2> | 00 00 00 0F`.
  - ASC1 = `(adts[2]>>3)&7 | ((adts[2]>>6)+1)*8`.
  - ASC2 = `(((adts[2]&1)<<2 | adts[3]>>6)<<3) | ((adts[2]>>2)<<7)`.
  - Both are derived from the first ADTS frame's profile, rate index and channels. Per the caps, the rate is 48 kHz mono.
- **AAC** data (FUN_001c6920): type 8, `AF 01` + the frame payload **skipping 7 ADTS bytes**, PrevTagSize = len + 13.
- **Opus** config (FUN_001c58f0), 19 bytes: `0A 00 00 04 | 00 00 00 00 | 00 00 00 | CF 00 03 <rateTag> | 00 00 00 0F`.
  - `rateTag`: 48000 -> 0, 24000 -> 1, 16000 -> 2, 12000 -> 3, 8000 -> 4; anything else falls back to 0.
  - The rate comes from the track's sample rate (= `opusSampleRate`).
  - It is sent once per connection before the first Opus frame (FUN_001c5e50, flag +0xdb9).
- **Opus** data: type **10**, body = raw Opus packet (TOC first, no prefix), PrevTagSize = len + 11, then the trailer with timescale = opusSampleRate.
  - Packets come from `opusenc` at 48 kHz mono, so 20 ms packets by default.
  - The timestamp comes from shm pts via `ns -> opusSampleRate ticks` [I].

### 4.5 Script tags
**onClockSync** (builder FUN_001cd1f0, sender FUN_001c7e60; keys alphabetical):
`{streamClock: num, streamClockBase: num, wallClock: num}`
- `streamClock` = `latest_pts_ms - base`, and is also the tag ts.
- `streamClockBase` = `baseDtsMillis` (absolute monotonic ms).
- `wallClock` = `gettimeofday` in ms (FUN_0022dd60).
- Layout offsets confirm that order (the 8-byte patch slots follow the keys `streamClock`, `streamClockBase`, `wallClock`).

**onMpma** (builder FUN_001cd780, values from FUN_001c4940, throttle FUN_001c7d20):
`{cs:{cur,max,min}, m:{cur,max,min}, r: 0.0, sp:{cur,max,min}, t: num}`, all doubles, keys alphabetical. In the object, the first `{cur,max,min}` object is `cs`.
- Slots in the order `cs.cur, cs.max, cs.min, m.cur, m.max, m.min, r, sp.cur, sp.max, sp.min, t`.
- Source struct offsets (uint to double): `cs.{min,max,cur}` = +0x20/+0x24/+0x28; `sp.{min,max,cur}` = +0x50/+0x54/+0x58; `m.{min,max,cur}` = +0x80/+0x84/+0x88. `r` = 0.0.
- `t` = `min(+0xb8 (if nonzero, else +4), +4)` when flag +0xb0 == 0, else `cs.min`.
- Meaning: the encoder's adaptive-bitrate state. `cs` is camera-server-adaptive, `sp` server-player-adaptive, `m` motion-adaptive, `t` the target. Mapping to `bitRateVbrMin/Max`, `minClientAdaptiveBitRate` and `minMotionAdaptiveBitRate` is [I] (the string "Unable to set the new values on MPMA: ..." lists exactly those four settings).
- Initial template value is -1.0 until set.
- Tag ts = current stream time. Trailer as §4.3.
- **Throttle** [D]:
  - Before every key frame: send unconditionally (SendMpma(0)).
  - Before non-key frames: only if at least 500 ms since the last send when any MPMA value changed, otherwise at least 10 s.

**onRtt** (builder FUN_001cd4d0, sender FUN_001c5cf0): `{encDelay: num, id: string(4 raw bytes), rttMillis: num}`.
- Wire order is alphabetical. `id` is a 4-byte string, `02 00 04` + the token bytes echoed from the console.
- `rttMillis = (local_monotonic_ticks - c) * 1000 / b`, or -1.0 (`BF F0 00 ...`) if invalid. `encDelay` = encoder delay ms (+0x3f8). The tag ts and trailer follow the timeline.

### 4.6 Send sequences
- **First connect** (S:FUN_001cae50, FUN_001c9fc0) [D]:
  1. Frames are skipped until SPS/PPS are known and a key frame arrives. The first frame sets `baseDtsMillis`.
  2. Header + `onMetaData`.
  3. `onMpma`, then `onClockSync`.
  4. The AVC (or HEVC) config tag.
  5. The key frame.
  6. Audio: AAC config, then Opus config (if `withOpus`), then audio frames (after the video base exists).
- **Every later key frame**: `onMpma`, `onClockSync`, (config tag only if SPS/PPS changed), the frame. Non-key frames get a throttled `onMpma` only. There is no timer-driven `onClockSync` for A/V streams.
- **Reconnect** (OutStream FUN_001a4890 -> `OnTransportReady` FUN_001cc360) [D]:
  - The streamer calls ForceIDR (a no-op in practice, see the corrections above).
  - It sends header + `onMetaData` (FUN_001c7c20, only if a base exists), the video config (FUN_001c85d0), the AAC config and the Opus config.
  - `baseDtsMillis` is **not reset** on reconnect. It only resets on a backward-timestamp reset or an AV-drift rebase (`RebaseTimestamps`).
  - So `streamClock` continues from the old base.

## 5. Transport (S: transporttcp.cpp)

Behaviour from FUN_001d5fc0 and the transport range [D unless tagged]:
- **Connection.**
  - One non-blocking TCP socket per channel, worked by an epoll thread plus an eventfd.
  - `socket(AF_INET, SOCK_STREAM)`, with the `IP_TOS` option set from the channel `tos` (184 in the template).
  - Parsed URI keys: `retryInterval` (strtol, default 1 s), `connectTimeout` (clamped 1..60 s, default 1 s), `encrypted` (presence enables TLS with `/var/etc/persistent/server.pem`).
  - After a failed or lost connection it waits `retryInterval` and reconnects (a "Disconnected! URI was ..." log).
  - NODELAY and other socket options were not seen.
- **Queue.** Output is a bounded byte queue.
  - `_maxNetworkOutBuffer` defaults to 0x500000 (5 MiB), chosen from a table keyed on pixel count (`> 0x7e8fff`) and a platform flag; the larger entries were not read [I].
  - `_minReportingBufferPercent` is 10 or 20 depending on the platform flag.
  - When `queued + frame > max`, the frame is dropped. The log "Frames are skipped! The current bandwidth is not enough!" appears at most every 30 s (FUN_001d2140).
  - Drop is per frame, and the next written frame is the next one produced. There is no forced resync [I].
- **Disabled channel.** A channel without `avSerializer`/destination, or with `enabled=false`, gets no transport (`Invalid ingest point`, `Transport removed via API`) and no stream exists, so nothing connects [S, I for details].

## 6. Feedback channel (console to streamer on the same socket)

Frame format (FUN_001d3020): `u16 type BE | u16 len BE | payload[len]`, repeated, with partial frames kept in the buffer.

| Type | Payload (BE) | Handling |
|---|---|---|
| 0 | `i32 bitrate-ish`, `u8`, `u32`, `u8`, `u16` (shorter forms accepted) | MPMA / bandwidth hint, calls `AdaptCameraServerBitrate` / `AdaptServerPlayerBitrate` (FUN_00190240) [meaning I] |
| 1 | none | queued, ignored in the first pass [I] |
| 2 | `u16` + optional `u16` | player hint: a bool is set from the second word [I] |
| 3 | none | sets the stream's stop flag (+0xb8) and logs at error level |
| 4 | `u32 token, u32 timescale, u64 ts` | stamp the local monotonic arrival time, then reply with `onRtt` (§4.5) |
| 5 | `u16 event code` | logged as `FEEDBACK_MESSAGE_TYPE_EVENT_NOTIFICATION : %d (%s)` |
| other | | "Invalid message received" |

- Bandwidth hints change the MPMA `cur` values (which appear in the next `onMpma`) and, via `Encoder::AdaptServerPlayerBitrate`, can call `SetBitRate` on the encoder. For medium (video2) it forces **150 kbps** after start ("Forcibly set medium stream to 150Kbps for fast startup").
- On AI Port, only the HQ bitrate reaches the V4L2 encoder (`ChangeVideoParams`), so for AQ and LQ these hints do not change the encoded bitrate [I].
- A new tag may be sent whatever the type, so draining the socket and never replying is accepted [observed in our live runs].

## 7. Audio

- **Decode.** `decodebin` handles any camera codec (G.711, AAC, ...) [D: FUN_00117544]. Then `audioconvert ! audiorate ! tee`.
- **AAC branch.** `audioresample ! caps audio/x-raw rate=48000 ! avenc_aac ! aacparse ! audio/mpeg(mpegversion=4, ADTS) ! ubnt_stream -> /tmp/<mac>.audio_aac` (1 MiB, 300 frames). `avenc_aac` has no properties set, so it uses GStreamer defaults (about 128 kbps AAC-LC) [I]. The streamer's JSON says aac 64 kbps, 1 ch, 16000, but the AAC from the pipeline is 48 kHz.
- **Opus branch.** `audio/x-raw rate=48000 S16LE mono ! opusenc complexity=0 max-payload-size=1275 ! opusparse -> /tmp/<mac>.audio_opus` (1 MiB, 300 frames). The Opus rate is therefore always 48 kHz.
- **PCM branch.** 16 kHz F32LE mono -> `/tmp/<mac>.audio_pcm` for audio events.
- **Transport to the streamer.** Shared-memory ring buffers written by the `ubnt_stream` gst plugin. The streamer maps them (`shmget`/`shmat`, ftok on `/tmp/<mac>.*`) and polls with `usleep(5000)`.
- **No audio.** There is no silent or test source [S]. The flags are `hasAudio=false` and header 0x03, with no audio tags.
- **Advertised capabilities.** The real `/etc/features.conf` contains `audioCodecs:["aac","opus"]` and `opusSampleRates:[48000]` [S, rootfs]. Our emulator advertises neither, and Protect then asked for `opusSampleRate: 24000`. Advertise `opusSampleRates:[48000]` and see whether Protect asks for 48000 (config byte 0, trailer 48000).

## 8. Control messages

- **OnvifStreamControl**.
  - Request (from our live log): `{streamUrl, width, height, fps, audio, streaming, deviceID}`.
  - Reply: `{status, usedPoints}`. `status` is one of started, stopped, connect_fail, parse_error, start_fail, stop_fail, onvif_url_fail, resolution_exceed, mixed_device_type, connection_exceed, connection_duplicate [S: OD strings]. Optional `description` string.
  - OD also sends an `EventAudioStatus {audioInclude}` to `ubnt_avclient`.
  - Points tiers are in `aiport-firmware-notes.md`. Adjustments to `lq_fps` and `yuv_fps` happen at launch.
- **ChangeVideoSettings**.
  - Request per channel: `video{videoN:{avSerializer:{type:extendedFlv, parameters:{streamName, withOpus, opusSampleRate}, destinations:[tcp://host:7550?retryInterval=1&connectTimeout=5]}, type, ...}}`.
  - It also carries: `enabled`, `fps`, `bitRateVbrMax`, `isCbr`, `nMultiplier`, `dynamicFpsMode`, `autoFps`, `autoBitrate`, `minClientAdaptiveBitRate`, `minMotionAdaptiveBitRate`.
  - Streamer behaviour: it validates bitrate ranges (rejects `bitRateVbrMax > validBitrateRangeMax`, falls back to defaults when the margin is too small) and `UpdateTransports` per channel.
  - Reply shape: the per-channel settings from `/etc/changevideosetttings-default-response.json`, with these keys: `M, N, autoBitrate, autoFps, avSerializer{destinations, parameters{audioId 1000, videoId 1001, suppressAudio, suppressVideo, enableTimestampsOverlapAvoidance, streamName}, type}, bitRateCbrAvg, bitRateVbrMax, bitRateVbrMin, bufferId, description, dynamicFpsMode, enabled, fps, gopModel, height, horizontalFlip, idleFps, isCbr, latencyTestSID, maxFps, minClientAdaptiveBitRate, minMotionAdaptiveBitRate, nMultiplier, name (video0.0/.1/.2), sourceId, streamId (1/2/4), streamOrdinal (0/1/2), tos 184, type, validBitrateRangeMin/Max, validFpsValues, verticalFlip, width`, plus top-level `audio{agc, bitRate, channels, enabled, sampleRate, type, volume ...}`, `cfgver 4`, `videoMode`, `vinFps`.
  - The stream settings (`width`, `height`, `fps`, `bitRateVbrMax`, `streamId`, `streamOrdinal`) feed `onMetaData` (§4.2).
  - Our compact reply (`{video:{videoN:{enabled,width,height,fps,bitrate}}, audio}`) is what we send today and Protect accepts it.
- **Timing.** Reply immediately. The real flow is ChangeVideoSettings first (streamer running after dep_mod), then OnvifStreamControl `started` after the RTSP pipeline reports streaming. Our log shows `started` about 1.5 to 2 s after the request.

## 9. Open questions
1. Exact rounding helper FUN_0010e698 (mod-2 or mod-16), and the 5 MiB queue table's larger entries.
2. Meaning of feedback types 0, 1, 2 and 3 beyond the handlers listed; what ms does if `onRtt` is never answered.
3. Whether integers in the maps serialize as AMF0 number (assumed) or something else; the variant number path was not read to the end.
4. Which launches pass `--raw_data` (OD FUN_001173f0 flag +0x48).
5. The MPMA struct to bitrate-setting mapping (§4.5).
6. HEVC config tag layout. The FLV 8 / `18 00` form is inferred from our existing notes and the `BuildVideoCodecBytesH265` string.
7. The real GOP effect on cold-switch latency: needs a live capture, since the lab never ran an ONVIF flow.
8. HQ AAC audio bitrate (`avenc_aac` defaults) and Opus bitrate (`opusenc` default) are inferred, not set in code.
