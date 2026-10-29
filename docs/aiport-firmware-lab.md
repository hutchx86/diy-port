# Running the real AI Port firmware: status and next steps

Status as of 2026-10-18 (paused). Ubiquiti's own AI Port firmware (v5.1.12, the image
in [protect-aiport-controller.md](protect-aiport-controller.md#firmware-source)) runs in
a Docker container on a Mac. The console adopted it as an AI Port, and it reached the
point of pulling camera streams. The aim is to check pi-port's emulation against the
real thing; see [aiport-interface.md](aiport-interface.md).

The lab is its own repo, **aiport-fw** (`~/Code/aiport-fw`). Its README has the lab's
status, open issues, next steps and how to resume; `docker/README.md` there has the
details. Like [aiport-firmware-notes.md](aiport-firmware-notes.md), this page holds
findings for pi-port only. No firmware files, extracts or Ubiquiti code go in either repo.

## How it runs (summary)

The firmware's rootfs runs unchanged in Docker (arm64, native on Apple Silicon), with
its own systemd as PID 1. Lab files stand in for the missing MT8390 hardware: board
info, U-Boot environment, reset button, DHCP lease, and software V4L2 video elements.
In LAN mode it can reach only the console. A relay on the Mac answers Protect's
discovery for `192.0.2.16`, an alias on the Mac's Wi-Fi. The aiport-fw repo has
the details.

## What works

| Step | Result |
|------|--------|
| Boot | systemd `running`; every enabled `ubnt_*` daemon up with no restarts |
| Discovery | Protect lists it as an AI Port at 192.0.2.16 |
| Adoption | Adopted from the Protect UI; `ubnt_avclient` reaches "Connected to UniFi Video Completely"; survives restarts |
| Camera assignment | Protect assigned two cameras within 20 s of adoption |
| Per-camera ingest | Pulls RTSP from the console, decodes in software, writes frames into the firmware's YUV shared memory, reports "streaming status 1" and starts the dependent per-camera modules |

## Findings that matter for pi-port

1. **Discovery reply from the real firmware** (once it knows its board):
   - platform TLV `0x0C` is **`UP AI Port`**; pi-port sends `UVC AI Port` or the
     `discovery_platform` override;
   - sysid `0x10` is `0xa5f1` (little-endian), name `0x0B` is `UVC AI Port`;
   - there's **no `0x3F` (ucp4) TLV**, which pi-port sends;
   - default-credentials `0x2C` is **`0x02`**; pi-port sends `0x03`;
   - TLVs in order: `0x02, 0x01, 0x0A, 0x0B, 0x0C, 0x17, 0x03, 0x10, 0x20, 0x2C`;
   - it only answers probes sent to the multicast group, not unicast ones.

   Protect listed and adopted it with these values. So pi-port's platform string and
   `0x3F` may not be required; worth testing before changing anything.
2. **Without a known sysid, the reply says `UVC Unknown` with sysid `0x0001`, and
   Protect ignores the device.** The firmware reads the sysid from the U-Boot
   environment, not from `board.info`.
3. **Avclient handshake headers match pi-port's:** `Camera-MAC` (no separators),
   `Camera-IP`, `Camera-Model: 0xa5f1`, `Camera-Firmware: 5.1.12`, `Device-ID`,
   `Adopted`. Protect accepted a `Camera-IP` (`172.30.99.2`) different from the
   address the connection came from.
4. **The firmware doesn't contact the console on its own when unadopted.** It waits
   in `WAITING_FOR_CONTROLLER_ADDRESS` until Protect adopts it. pi-port's unsolicited
   pre-adopt connection isn't something the real device does here; discovery alone was
   enough for Protect to list it.
5. **After adoption, Protect:** sends the face DB
   (`https://<console>:7444/internal/face-db/latest?type=known&mac=<MAC>`), has the
   device stop its SSH service, and assigns paired cameras right away. On reconnect it
   asks `GetStreamList` and doesn't necessarily re-send `UiStreamControl`.
6. **Per-camera ingest (`ubnt_av_aiport@<camera MAC>`):**
   - It receives the stream URI, and the firmware saves it per camera with `lq_fps:
     24`, `yuv_fps: 10` and `raw_data: true`.
   - It analyses 10 fps of YUV per camera, matching pi-port's default detection rate
     on macOS.
   - It scales to 2560x1440 / 1280x720 / 640x360.
   - Once streaming, it starts the per-camera `ubnt_streamer@`,
     `ubnt_smart_detect@`, `ubnt_smart_motion@` and `ubnt_audio_events@` itself.

## Limits

The real firmware won't produce object detections here. `ubnt_smart_detect`'s models
are compiled for the MT8390's AI accelerator. A stand-in for MediaTek's Neuron Runtime
gets it running: it loads its models and runs on frames, but every result is empty.
That's enough to watch its control flow (ready status, rules, snapshot slots), not its
detection events. The ingest's snapshot stage also still fails, so the per-camera
pipeline restarts every few seconds and takes `smart_detect` down with it. The
lab's open issues are tracked in aiport-fw's README.

## Next steps for pi-port

1. **Compare discovery replies.** Try pi-port's discovery with the real firmware's
   values (`UP AI Port`, no `0x3F`, `0x2C = 0x02`) and see whether Protect still lists
   and adopts it. That settles findings 1 and 4.
2. **Revisit the pre-adopt avclient connection.** The real firmware doesn't make one
   (finding 4). Check whether pi-port still needs it, now that discovery alone got the
   real device listed.
3. **Use the lab's message captures** once aiport-fw logs the firmware's IPC (its next
   step 2): compare the hello, `EventAIPortStatus` and `EventFeatureFlagsUpdated` with
   [aiport-interface.md](aiport-interface.md).
4. **Per-camera motion** (gap 2 in
   [protect-aiport-controller.md](protect-aiport-controller.md#gaps-in-pi-port)), once
   the lab gets `ubnt_smart_motion` running and shows its `EventSmartMotion` shape.
