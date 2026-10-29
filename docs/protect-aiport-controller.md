# The AI Port from Protect's side (7.1.75)

What the UniFi Protect controller does to connect, manage and use an AI Port, read
from the extracted service bundle in `~/Code/up/app/modules`
(`unifi-protect.Debian11_arm64.v7.1.75`). File names below are those extracted
modules. The device side of the same protocol is in
[aiport-interface.md](aiport-interface.md).

At the end is a list of places where pi-port does not match what this code
expects.

## Where the code lives

| Module | Role |
|--------|------|
| `AiPortDecalModel_et_al.js` | The `aiport` model: extends the camera model; adds `pairedCameras`, `featureFlagsMap`, capacity fields |
| `aiPortsMiddleware_et_al.js` | Wires everything up: the `aiport.*` command table, decal listeners, the overridable feature-flag lists |
| `aiportUpdateHandler.js` | Runs on every AI Port save: the **on-connect sequence** |
| `aiportsRouter.js` | User REST API: `/aiports`, `/:id/adopt`, `/readopt`, `/locate`, `/update`, `/reboot`, `/pair`, `/unpair` |
| `pairCamera_et_al.js` | Pair / unpair checks and error codes |
| `restartStream_et_al.js` | Builds `UiStreamControl` / `OnvifStreamControl` |
| `unknown/module_45770.js` | `streamControlRequest`: sends it, reads `status` and `usedPoints` |
| `getStreamList.js`, `clearStreams.js` | `GetStreamList`, `ResetAIPortStreams` (or reboot) |
| `handleCameraMessage_et_al.js` | Routes avclient messages from an AI Port to the paired camera |
| `statusUpdateStreaming.js`, `statusUpdateSmartServices.js`, `statusUpdateAudioServices.js` | React to `EventAIPortStatus` |
| `handleFeatureFlagsUpdate.js` (+ `unknown/module_28164.js` `deserializeFromCamera`) | Store `EventFeatureFlagsUpdated` in `featureFlagsMap` |
| `handleFeaturesFlagUpdate.js` | Copy the overridable subset onto each paired camera |
| `updateSmartDetectSettings.js`, `updateCameraLprFlag.js` | `ChangeSmartDetectSettings` to the AI Port |
| `removePairedCamera.js`, `cameraDelete.js`, `cleanupAiPortPairedCameras.js` | Unpair and clean-up |
| `reSyncAiPortOnHostChange.js` | Re-push RTSP URLs and settings when the NVR's host changes |
| `isDetectionHandledByAiPort.js`, `filterFeatureFlagsForAiPortCamera.js`, `adjustForAiPortCompatibility.js` | Helpers |

## The model

`AiPortDecalModel` is a `CameraDecalModel` with `modelKey: "aiport"`, `product: "AI-Port"`,
and platform `"UVC AI Port"`. It adds:

- `pairedCameras` — the cameras this AI Port analyses.
- `featureFlagsMap` — `{ <camera MAC>: featureFlags }`, filled from the AI Port's
  `EventFeatureFlagsUpdated`.
- `maxCameraCapacity` — always **1**.
- `cameraUtilization` — what the AI Port last reported (`usedPoints / 10`).
- `cameraCapacity` — Protect's own estimate. State is `ok`, `inconsistent` (estimate
  ≠ reported), or `overloaded` (> 1).
- `AI_PORT_MAX_WIDTH × AI_PORT_MAX_HEIGHT` = 3840 × 2160. Bigger cameras must
  downscale first.

### Capacity points

Each paired camera costs a fraction of the single point, based on
`width × height × fps` of its primary high-quality channel:

| Class | Pixel rate (w·h·fps) | UniFi camera | Third-party (ONVIF) |
|-------|----------------------|--------------|---------------------|
| K5 | > 248,832,000 | 1 | 1 |
| K4 | > 121,927,680 | 0.5 | 1 |
| K2 | > 62,208,000 | 0.25 | 0.5 |
| K1 | otherwise | 0.2 | 0.3 |

So 1080p30 is K1 (0.2), 4K30 is K4 (0.5). Pairing is refused with
`estimated_capacity_exceeded` when `cameraUtilization + camera points > 1`.

These match Ubiquiti's published limits (UniFi: 4K ×2, HD ×5; ONVIF: 4K ×1, 2K ×2,
HD ×3). The one exception is UniFi 2K: the published limit is 3 cameras, but 0.25
points each allows 4. The published figure may just be conservative, or a real AI
Port may report more `usedPoints` than Protect's estimate.

ONVIF cameras cost more because the AI Port does more work for them, not because
UniFi cameras help with detection:

- **UniFi camera:** the camera still streams to the NVR as usual. The AI Port only
  pulls a copy from the NVR's RTSP relay (`:7447`) to analyse it.
- **ONVIF camera:** Protect stops ingesting the camera itself
  (`ems.streams.removeAllConfigs`) and the AI Port takes over as its gateway. It pulls
  `streamUrl` from the camera and also pushes the video to Protect: `pushStream`
  addresses a paired ONVIF camera's streams to the AI Port with `deviceID`. Protect also
  sends that camera's `ChangeVideoSettings`/`ChangeIspSettings` to the AI Port, and the
  AI Port's connection state drives the camera's `CONNECTING` → `CONNECTED`.

Nothing in the code uses a UniFi camera's own analysis while it is paired: Protect
drops its smart-detect, motion and audio events (see below).

## Connecting

The AI Port is a camera-class device, so it uses the camera adoption and connection
plumbing:

- **Adopt** (`adopt.js`): if the device has `supportUcp4` (set from discovery TLV
  `0x3F`), Protect POSTs `https://<device>/api/adopt` with
  `{username, password, ...mgmt}` merged flat. Otherwise it POSTs
  `/api/1.2/manage` with `{username, password, mgmt}`. `mgmt` = `{token, hosts, protocol: "wss", ...}`.
- **Connections:** `ucp4` WebSockets go through `verifyUcpClient`, which updates
  `anonymousDeviceId`, `guid`, `sysid` and `host`, rejects an unadopted device without
  a token (`403 not adopted`), and adopts on a valid token. The classic avclient
  connection is the camera path; its messages go to `handleCameraMessage`.

### On-connect sequence (`aiportUpdateHandler`)

When the AI Port's `state` becomes `CONNECTED`, Protect runs, in order:

1. `cameraUtilization = 0`.
2. `GetStreamList`. If the list is non-empty, clear it: `ResetAIPortStreams` on
   firmware ≥ 4.75.6, otherwise **reboot** the AI Port. Then stop for now.
3. Provision (`cameras.provision`) if not yet provisioned.
4. `aiport.enableSSH` / `disableSSH`, then `UpdateUsernamePassword`
   (`{username, hashedPassword}`), firmware update check, LED settings
   (`POST settings/led {ledEnabled}`), `ChangeDeviceSettings {timezone}`.
5. Mark every paired camera `isPairedWithAiPort`.
6. `runAllStreams`: one `restartStream` (→ `UiStreamControl`) per paired camera.
7. `ChangeSmartDetectSettings {isLprCamera: 0|1, deviceID}` per paired camera.
8. Copy feature flags from `featureFlagsMap` to the paired cameras.

On `DISCONNECTED`, per-camera status is reset and a camera disconnect event is
emitted. If `isAdopted` becomes false, every paired camera is unpaired.

## Pairing and streaming

`pairCamera` (from `POST /aiports/:id/pair`) checks: camera adopted, not already
paired, capacity points computable, capacity not exceeded, resolution can be
downscaled to ≤ 4K. Then it adds the camera to `pairedCameras` and calls
`restartStream`. If that fails, the camera is unpaired again. On success it sends
`EventAudioStatus {audioInclude: <camera hasMic>, deviceID}` (firmware ≥ 4.73.8).

`restartStream` sends one of:

```js
// UniFi camera
UiStreamControl { streaming: true, ip: <camera.connectionHost or nvr.host>,
                  port: "7447", uri: <rtspAlias>, deviceID: <camera MAC>,
                  width, height, fps }
// Third-party (ONVIF) camera
OnvifStreamControl { streaming: true, streamUrl: "rtsp://user:pass@...",
                     width, height, fps, audio, deviceID: <camera MAC> }
```

It turns RTSP on for the camera's channel if needed. The request waits 10 s for a
reply. `streamControlRequest` then reads the reply:

- `usedPoints` → `cameraUtilization = usedPoints / 10`. **If `usedPoints` is missing,
  utilization is set to `maxCameraCapacity` (1)**, which blocks any further pairing.
- `status` must be `started` or `stopped`; anything else fails with
  `payload.description`, which may be `connection_exceed` or `mixed_device_type`
  (both make Protect unpair the camera) or `unknown`.

Unpairing (`removePairedCamera`) removes the camera from `pairedCameras` and its
`featureFlagsMap` entry, clears its `smartDetectTypes`/`smartDetectAudioTypes`, and
for a UniFi camera asks it for fresh feature flags (or reboots it on firmware
< 4.73.90). Then it closes the camera's streams.

`GetStreamList` replies are validated as
`{payload: {list: [{deviceID, points?, streamUrl?, type?}]}}`.

## Messages from the AI Port

`handleCameraMessage` maps a message from an AI Port to a paired camera by
`payload.deviceID` (upper-cased), **falling back to the first paired camera** when
`deviceID` is missing.

| Message | What Protect does |
|---------|-------------------|
| `EventSmartDetect` | `enter`/`moving`/`leave`/`packageDetected` → `events.cameras.object*` **on the paired camera**. `none` is only used when Insights is on. |
| `EventSmartMotion` / `EventAnalytics` | `motion.start` / `motion.pulse` / `motion.stop` on the paired camera |
| `EventSmartAudio` | audio alarm on the paired camera |
| `EventFeatureFlagsUpdated` | Parsed with the camera feature-flag parser and stored as `featureFlagsMap[<camera MAC>]` |
| `EventAIPortStatus` | `isStreaming` → `statusUpdateStreaming`; `isSmartDetectReady` → push smart-detect and enhanced-motion settings (only once streaming); `isAudioEventReady` → push audio-event settings |
| `EventUpdateFirmwareStatus` / `...Failure` | Firmware update state, as for cameras |

The reverse rule matters as much: **while a camera is paired, Protect ignores that
camera's own `EventSmartDetect`, `EventSmartMotion`, `EventAnalytics` and
`EventSmartAudio`** (except a package camera's `packageDetected`). Everything comes
from the AI Port.

### Feature flags

`EventFeatureFlagsUpdated` is parsed exactly like a camera's capabilities:
`smartDetect` → `smartDetectTypes` (filtered to known object types) and
`smartDetectAudioTypes`; `lineCrossing`, `lineCrossingCounting`, `liveviewTracking`
in `smartDetect` set the matching `has*` flags; `motionDetect` → `motionAlgorithms`
(default `["stable"]`) and `hasMotionDetection`; `privacyMask`,
`squareEventThumbnail`, `mic`, `ledStatus`, etc.

Only these keys are copied onto a paired UniFi camera:
`hasMotionZones, motionAlgorithms, hasSmartDetect, hasLineCrossing,
hasLineCrossingCounting, smartDetectTypes, smartDetectAudioTypes, excludeZones,
hasPrivacyMask, privacyMaskCapability, hasSquareEventThumbnail, hasLiveviewTracking,
hasIcrSensitivity, hasVerticalFlip, hasHorizontalFlip`. Third-party cameras also get
audio/video codec and mode keys. A doorbell always keeps `package`. The first
`motionAlgorithms` entry becomes the camera's recording motion algorithm.

### Settings pushed to the AI Port

`updateSmartDetectSettings` sends `ChangeSmartDetectSettings` to the AI Port **and**
to the UniFi camera itself. The AI Port copy has the full set — `zones`, `lines`,
`loiterZones`, `excludeZones`, `secondLensZones`, `enableSmartDetect`,
`eventStartMSec`, `eventStopMSec`, `recognitionAccuracy`, `reVerificationPolicy`,
… — plus `deviceID` (and `algoVersion: "beta"` when applicable).

`updateCameraLprFlag` sends a **separate, partial** `ChangeSmartDetectSettings`:
only `{isLprCamera, deviceID}`.

### Snapshots for a paired camera

`snapshot_et_al.js` sends a paired camera's snapshot request **to the AI Port**:

- AI Port has `supportUcp4` → `ucp4 getSnapshot {quality}`. **No camera id.**
- Otherwise → avclient `GetRequest` with `deviceId = <camera MAC>`.
- Then REST snapshot from the AI Port, then the database.

### Host change

When the NVR's host changes, Protect re-sends `UiStreamControl` for every paired
camera on every connected AI Port, then the smart-detect and enhanced-motion settings.

## Firmware source

Protect finds firmware with `findRemoteUpdates.js`: a GET to
`firmwareUpdatesUrl` (`https://fw-update.ui.com/api/firmware-latest`, from
`config/default.json`) with `filter=eq~~product~~<product>`,
`eq~~platform~~<platform>` and `eq~~channel~~<channel>`. The release channel needs
no token. For the AI Port the model gives `product: "AI-Port"`, `platform: "all"`:

```
https://fw-update.ui.com/api/firmware-latest?filter=eq~~product~~AI-Port&filter=eq~~platform~~all&filter=eq~~channel~~release
```

On 2026-10-16 this returned v5.1.12 (~878 MiB `.bin`, md5 and sha256 included) on
`fw-download.ubnt.com`. `/api/firmware` with the same filters lists older
releases back to v4.73.8. The `UpdateFirmwareRequest` URI a device receives points
at the console's own `/internal/update/...` copy of the same file.

## Gaps in pi-port

Ranked by impact. Each was checked against both codebases.

1. **The LPR-flag message wipes zones and lines.** On every AI Port connect, step 7
   above sends `ChangeSmartDetectSettings {isLprCamera, deviceID}`. pi-port's handler
   (`avclient.py` `handle_function`) replaces `excludeZones`, `zones` and `lines` with
   whatever the payload has, which is nothing. So after a reconnect, exclude zones
   stop filtering, detect zones stop restricting, and Line Crossing stops, until
   Protect happens to resend the full settings. Fix: only update keys that are present.

2. **No motion events for paired cameras.** Protect drops a paired camera's own
   `EventSmartMotion`/`EventAnalytics`, and pi-port never sends them. Motion-only
   events and motion-triggered recording for paired cameras likely stop. pi-port needs
   to send `EventSmartMotion` (`eventType: "motion"`, `edgeType: start|stop`, or
   `eventType: "pulse"`) with `deviceID`. The exact payload fields still need to be
   read from the `events.cameras.motionStart` handler.

3. **Camera audio detections are lost.** The same rule drops the camera's own
   `EventSmartAudio`, and pi-port's feature flags have no audio types, so the camera's
   `smartDetectAudioTypes` becomes `[]` (smoke/CO alarm, glass break etc.).

4. **Snapshots can come from the wrong camera.** pi-port advertises `supportUcp4`, so
   Protect asks for paired-camera snapshots with `ucp4 getSnapshot {quality}`, which has
   no camera id. pi-port returns the newest frame from any camera. With two or more
   paired cameras, previews can show the wrong camera. Not advertising TLV `0x3F`
   would make Protect use avclient `GetRequest` with `deviceId`, which pi-port already
   resolves per camera; that flag only gates update-before-adoption for Access
   devices, but it also picks the adopt path (`/api/adopt` vs `/api/1.2/manage`), so
   re-test adoption.

5. **Third-party (ONVIF) cameras pair but get no detection and likely no video.**
   pi-port replies `started` to `OnvifStreamControl` without pulling `streamUrl`.
   Protect has stopped ingesting that camera directly and expects the AI Port to push
   its video, so the camera probably stops recording as well as getting no detections.
   Until it's supported, pi-port should reply with a failure
   (`description: "mixed_device_type"`) so Protect unpairs the camera.

6. ~~**Capacity reporting doesn't match Protect's estimate.**~~ Fixed: pi-port now
   reports the real device's per-stream points (measured from its firmware, equal to
   Protect's estimate × 10), lists active streams in `GetStreamList`, and handles
   `ResetAIPortStreams` and `GetUiStreamPoints`.

Smaller notes:

- `EventAudioStatus` is sent (pi-port reports firmware 5.1.x, above the 4.73.8 gate)
  and falls through to pi-port's generic `{}` ack.
- `ChangeDeviceSettings` on connect carries only `{timezone}`; pi-port's handler
  correctly ignores the missing `name`.
- `deviceID` on pi-port's events is the MAC Protect sent, so the upper-case
  match works.
- `discovery.py` says TLV `0x3F` "gates needUpdateBeforeAdoption"; in 7.1.75 that
  gate only applies to Access devices.
