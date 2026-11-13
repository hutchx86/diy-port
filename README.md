# diy-port: a DIY UniFi Protect AI Port for Orange Pi, Raspberry Pi, x86 and Mac

<div align="center">

<a href="LICENSE"><img src="https://img.shields.io/badge/license-AGPL--3.0--or--later-blue" alt="License: AGPL-3.0-or-later"></a>
<a href="https://github.com/hutchx86/diy-port/actions/workflows/ci.yml"><img src="https://github.com/hutchx86/diy-port/actions/workflows/ci.yml/badge.svg" alt="CI"></a>

**A reverse-engineering / interoperability proof of concept that makes a stock
SBC or PC present itself to a real UniFi Protect console as genuine AI Port
hardware (catalog model `UVC AI Port`, sysid `0xa5f1`).**
**It speaks the vendor wire protocol only - discovery, adoption, camera pairing,
video relay, pluggable object detection and detection events with thumbnails -
with no firmware modification, no controller database writes and no Ubiquiti
code or binaries.**

</div>

> [!WARNING]
> Proof of concept, not production software. It binds privileged ports
> (UDP/10001, TCP/443) and manages macvlan interfaces and DHCP leases;
> misconfiguration can disrupt your network or device. Use it only on hardware
> and consoles you own. See [Disclaimer](#disclaimer).

> [!NOTE]
> Not affiliated with, endorsed by, or connected to Ubiquiti Inc. "UniFi",
> "UniFi Protect" and "AI Port" are trademarks of Ubiquiti Inc. No Ubiquiti
> firmware or binaries are distributed here. Built with AI assistance under
> human review - verify anything you rely on. See [Legal](#legal).

## Quick start

```bash
git clone https://github.com/hutchx86/diy-port && cd diy-port

# Orange Pi 5 Plus (RK3588, NPU):
sudo scripts/install.sh --console <console-ip> [--parent-iface <iface>]

# Raspberry Pi 5 + Coral, or any Linux CPU host (bare metal):
sudo scripts/install.sh --console <console-ip> --accel edgetpu   # or cpu
# (Pi 5 install.sh path is untested against a console; Docker bridge is verified)

# x86_64 / Raspberry Pi 4/5 with Docker:
AIPORT_CONSOLE_HOST=<console-ip> docker compose up -d --build

# Apple-silicon Mac, bare metal (Docker Desktop can't do L2 discovery):
# see "macOS (bare metal)" below

# ...or the console runs on the same host as Docker: see docker/README.md

# then adopt "AI Port" in Protect and pair a camera to it
```

## At a glance

|  |  |
| --- | --- |
| **What** | Emulates the UniFi Protect AI Port device role |
| **Hardware** | Orange Pi 5 Plus (RK3588 NPU) - verified live; Raspberry Pi 5 + Coral (Docker bridge) - verified; x86_64 + Raspberry Pi 4/5 (CPU, Docker); Apple-silicon Mac (bare metal) |
| **Protocol / interface** | UBNT discovery UDP/10001; mTLS WSS :7442 (`ucp4` + classic avclient); RTSP relay :7447; ONVIF video push to :7550 |
| **Detection** | Pluggable COCO detectors: RKNN (RK3588 NPU), Edge TPU (Coral), or `onnxruntime` on CPU/Core ML |
| **Language** | Python 3 |
| **License** | AGPL-3.0-or-later |

## How it compares

|  | Real AI Port | DIY Port |
| --- | --- | --- |
| **What it is** | Ubiquiti AI Port appliance (Ambarella) | This repo's Python, on an SBC/PC |
| **Firmware** | Ubiquiti, signed | None - the emulator itself |
| **Detection model** | Ubiquiti's own classifier | Stock COCO model mapped to person/vehicle/animal (YOLOv5 on the RK3588 NPU, SSDLite MobileDet on a Coral, YOLOv5n on CPU/Core ML), plus optional plates, faces and vehicle attributes |
| **Output** | Full Ubiquiti product behaviour | The subset this project implements |

It is a protocol-compatible stand-in built on the same shared camera/chime
middleware, not a clone of the device.

## How it works

The host answers the console's L2 discovery, then adopts like a classic camera
(`POST /api/1.2/manage` on HTTPS :443). It keeps two mTLS WebSocket connections
to the console - the generic `ucp4` channel for management RPCs, and the classic
avclient channel for camera pairing and detection events. When a camera is
paired, the console hands the host an RTSP URL on its own internal relay
(`:7447`); the host pulls that stream, runs the configured detector, and pushes
`EventSmartDetect` events and snapshot uploads back to the console.

An assigned third-party (ONVIF) camera is handled the other way round: Protect
stops ingesting it and asks the AI Port to push its video to `tcp://<console>:7550`
as `extendedFlv`, while the AI Port also pulls the camera's RTSP for detection.

<img src="docs/images/architecture.svg" width="760" alt="DIY Port data path: cameras to Protect console to emulated AI Port">

| Component | Role |
| --- | --- |
| `discovery.py` | UDP/10001 UBNT discovery responder |
| `http_api.py` | HTTPS :443 adopt / control / snapshot API |
| `ucp4_client.py` | Generic `ucp4` device-management WebSocket |
| `avclient.py` | Pairing, streaming and smart-detect WebSocket + detection loop |
| `detectors/` | Pluggable detector backends: `rknn` NPU, `edgetpu` Coral, `cpu` (onnxruntime/Core ML) |
| `enrich.py`, `facedb.py` | Optional vehicle attributes, plates and faces (Protect's face DB) |
| `motion.py`, `regions.py`, `stationary.py`, `norfair_match.py` | Frigate-derived motion gate, region crops, parked-object check, Norfair matching |
| `replay.py` | Offline clip replay and scenario checks |
| `onvif_push.py` | ONVIF camera video push to Protect (`extendedFlv`) |
| `onvif_media.py` | ONVIF camera's media profiles (substream for `video3`) |
| `sysinfo_server.py` | Dev-box CPU/mem/GPU/NPU/Coral status page (`webui.html`) |
| `instance_manager.py`, `run_all.py` | Multi-instance orchestration |

## Supported hardware

| Model | SoC / variant | Status | Notes |
| --- | --- | --- | --- |
| Orange Pi 5 Plus | RK3588 | Verified | Target board; NPU; 4 cameras have been paired and streamed live |
| Other RK3588 boards | RK3588 | Untested | The NPU and ffmpeg paths are RK3588-specific |
| Raspberry Pi 5 + Coral USB | BCM2712 + Edge TPU | Verified (Docker bridge) | Adoption, a UniFi camera and a 4K ONVIF camera with MobileDet detection (~33 ms/frame on USB 2); `install.sh` path untested against a console; no H.264 hardware decoder |
| Raspberry Pi 4 | BCM2711 | Verified | `docker/` image; H.264 hardware decode bare-metal only |
| x86_64 server / PC | amd64 | Verified | `docker/` CPU image; no NPU |
| Apple-silicon Mac | M-series | Verified | Bare metal only (Docker Desktop has no LAN L2); Core ML on the Neural Engine |
| Intel x86_64 (iGPU / Arc / Core Ultra NPU) | `openvino` | Detector verified, not yet against a console | `--accel openvino` or the `ACCEL=openvino` image; YOLOv5n ~13 ms/frame on a 12th-gen Iris Xe iGPU (vs ~33 ms for the `cpu` backend on the same host), bare metal and Docker; NPU path untested |
| Any Linux CPU host | `onnxruntime` | Untested | `--accel cpu`; fine for a camera or two |

The detector is pluggable (`diyport/detectors/`, `[hardware] detector` in
`aiport.cfg`): `rknn`, `edgetpu`, `openvino`, `cpu`, or `auto`, which takes the
first one whose runtime, hardware and model are present, in that order. A new
accelerator is a
`base.Detector` subclass registered in `BACKENDS` (and `AUTO_ORDER` for `auto`)
in `diyport/detectors/__init__.py`.

## Features

- **Emulated AI Port device role** - L2 discovery, adoption, and both WebSocket
  connections a real console expects.
- **Camera pairing and video relay** - pairs real cameras and pulls their stream
  from Protect's internal RTSP relay.
- **Third-party (ONVIF) cameras** - pushes their video to Protect for live view
  and recording, and runs detection on the same stream.
- **Pluggable real object detection** - RKNN on the RK3588 NPU, SSDLite
  MobileDet (or a YOLOv5 Edge TPU export) on a Coral, and onnxruntime on CPU or
  the Apple-silicon Neural Engine (Core ML).
- **Detection events with thumbnails** - `EventSmartDetect` with per-object
  snapshots and Full-FoV images, plus the overview/timeline thumbnails. One event
  per camera, with batched leaves, real zone ids and per-camera box timing.
- **Frigate-derived detection quality** - motion gate, native-size region crops,
  true-positive score gating, parked-object pixel checks and an optional Norfair
  tracker; all off or unchanged unless enabled.
- **Optional enrichment** - license plates (`fast-alpr`), faces (YuNet/SFace,
  matched against Protect's known-faces DB) and vehicle type/colour (Apple's model
  on macOS, YOLO class + HSV elsewhere).
- **Replay harness** - recorded clips run through the full pipeline with `--check`
  expectations, so detection changes are tested before going live.
- **Observability** - per-camera frames/inference lines and a host load line
  (CPU per process group, GPU, NPU, power where readable); a launchd service on
  macOS.
- **Multi-instance** - one reboot-surviving systemd unit and macvlan interface
  per instance on the board, or one container per host. Board/macvlan instances
  are distinct L2 devices; a host-network container shares the host's IP. A
  small web UI manages the board's instances.
- **Honest state handling** - reflects real adopted state and clears local state
  on the console's reset.

## Requirements

- An Orange Pi 5 Plus (RK3588) with root and `rknn-toolkit-lite2` for NPU
  inference; **or** a Linux host (Raspberry Pi 5 + Coral, x86_64, Pi 4/5) for
  the Edge TPU or CPU path; **or** an Apple-silicon Mac bare metal for the
  Core ML/CPU path.
- The backend's runtime: `rknn-toolkit-lite2` (NPU), `ai-edge-litert` +
  `libedgetpu` (Coral), `openvino` + Intel's GPU compute runtime (Intel
  GPU/NPU), or `onnxruntime` (CPU/Core ML). `install.sh` installs
  only the selected backend; Docker builds it into the image.
- A UniFi Protect console you own and control, on the same LAN (except the
  same-host Docker bridge setup).
- For model conversion: an x86_64 Linux host with `rknn-toolkit2` (the RKNN
  converter does not run on the board).
- Optional enrichment: `opencv-python-headless` for the motion gate, stationary
  checks and faces; `fast-alpr[onnx]` for plates; `norfair` for the Kalman
  tracker. Recipes and extras are in `diyport/requirements.txt`; every stage
  degrades cleanly when absent.

## Repository layout

```
diyport/               the emulator
  discovery.py          UDP/10001 UBNT discovery responder
  http_api.py           HTTPS :443 adopt/control API
  ucp4_client.py        generic ucp4 device-management websocket
  avclient.py           pairing/streaming/smart-detect websocket + detection
  detectors/            pluggable detector backends (rknn / edgetpu / openvino / cpu)
  enrich.py / facedb.py optional plates, faces and vehicle attributes
  motion.py / regions.py / stationary.py / norfair_match.py   Frigate-derived detection
  replay.py             offline clip replay and scenario checks
  hoststats.py / camera_clock.py / apple_fm.py   host load, RTSP clock, macOS vehicle model
  apple/                Swift helper for the macOS vehicle model
  onvif_push.py         ONVIF camera video push (extendedFlv)
  onvif_media.py        ONVIF camera media profiles (substream lookup)
  sysinfo_server.py     dev-box CPU/mem/GPU/NPU/Coral status page (+ webui.html)
  models/               anchors + COCO labels (model binaries fetched, not stored)
  tests/                unit suite
  requirements.txt      Python dependencies (optional extras documented inline)
  instance_manager.py / run_all.py   multi-instance orchestration
compose.yaml          CPU stack / Komodo Stack (host net; x86_64 + arm64)
compose.macvlan.yaml  same, but a dedicated LAN IP/MAC per container
compose.macos.yaml    macOS container (NAT + pf; see docker/README.md)
docker/               Dockerfile (CPU/Coral), bridge compose, smoke test + notes
scripts/              install.sh, fetch_models.py, record_clip.py, log_writer.py;
                      diyport_service.sh + macos_launchd.sh / macos_move_host.sh (macOS)
rknn_convert/         YOLOv5s -> RKNN conversion recipe (x86 build host)
docs/                 protocol notes and design records (docs/images/ holds the diagram)
```

Model binaries (`.onnx`/`.rknn`/`.tflite`) and all Ubiquiti firmware are **not**
stored here; see [Model assets](#model-assets-not-tracked). `diyport/README.md`
has the protocol detail and per-component notes.

## Install / Usage

### Orange Pi 5 Plus (RK3588)

`scripts/install.sh` does the whole stand-up on the board: system packages, a
venv + dependencies, model assets, the required `librknnrt.so` upgrade, a
reboot-surviving systemd instance (via `instance_manager.py`), and the web UI.

```bash
sudo scripts/install.sh --console <UNVR-IP> [--parent-iface <iface>]
```

- Run from the checkout root, as root. `--parent-iface` defaults to the
  auto-detected default-route NIC.
- `--accel auto|rknn|edgetpu|openvino|cpu|none` picks the detector backend
  (default `auto`: rknn on an RK3588, edgetpu when a Coral is present, openvino
  on an x86_64 host with an Intel GPU/NPU, else cpu) and
  installs only that backend's runtime and model. `--no-npu` is an alias for
  `--accel none`.
- The NPU model cannot be built on the Pi; build it on an x86 host
  (`scripts/fetch_models.py --convert`) or let the installer fetch it from
  `--rknn-url` / `DIYPORT_RKNN_URL` / its default URL. `--rknn <path>` takes a
  local build, and a local `models-rknn/` copy (e.g. a checkout of
  `hutchx86/pi-port-models`) is also detected.
- Opens the web UI on `127.0.0.1:8090` by default (`--webui-port` and
  `--webui-bind` to change) for board stats and instance management. Use
  `--webui-bind 0.0.0.0` to expose it on the LAN; it has no auth. Instance
  create/destroy is localhost-only unless `AIPORT_WEBUI_ALLOW_REMOTE=1` is set.

Then, in Protect: adopt "AI Port" like any camera, and pair a real camera to it
from the camera's own settings page. Detections appear as dashboard events with
thumbnails, and the console's own person/vehicle/animal filters work.

`run_all.py` needs root (binds UDP/10001 and TCP/443) and must be able to send L2
broadcast/multicast on the LAN, so run it on the host, not in a NAT'd container
(exception: the same-host bridge setup below, where the console reaches the
container IP directly).
It resolves its own interface's IP by default, so multiple instances on one box
don't collide on `:443`.

### Raspberry Pi 5 + Coral (bare metal)

`install.sh --accel edgetpu` installs `ai-edge-litert`, feranick's maintained
`libedgetpu1-std` build (sha256-pinned per distro) and Google's SSDLite MobileDet
model, then writes `[hardware] detector = edgetpu`:

```bash
sudo scripts/install.sh --console <console-ip> --accel edgetpu
```

- Only the USB Coral is handled; a PCIe/M.2 Coral additionally needs the
  `gasket`/`apex` kernel driver, which the installer does not set up.
- The Pi 5 has no H.264 hardware decoder, so camera streams are decoded in
  software for detection (~half a core for 4K at 12 fps).
- Plug the Coral into a USB 3 port if you have one free; USB 2 works
  (~33 ms/frame for MobileDet, far below the 1.5 s detection tick).

For the same Pi 5 running the console in Docker, use the bridge setup instead
(see [Docker (console on the same host)](#docker-console-on-the-same-host)).

### Intel GPU / NPU (OpenVINO)

`install.sh --accel openvino` (x86_64) installs `openvino`, the YOLOv5n ONNX
model (the same file as `cpu`), and, when an Intel GPU is present, the OpenCL
compute runtime that OpenVINO's GPU plugin needs. That runtime is the distro's
`intel-opencl-icd` where apt has it (Ubuntu, Debian bookworm), else Intel's
sha256-pinned `compute-runtime` + IGC release debs (Debian trixie dropped the
package). It then writes `[hardware] detector = openvino`:

```bash
sudo scripts/install.sh --console <console-ip> --accel openvino
python3 scripts/detector_check.py --backend openvino --image bus.jpg --expect person   # quick check
```

- `AIPORT_OV_DEVICE` picks the device: `auto` (default) takes the first GPU,
  then an NPU, then the CPU. `GPU`, `GPU.1`, `NPU`, `CPU` or OpenVINO's own
  `AUTO:GPU,CPU` pin one. A device that can't compile the model falls back to
  the CPU, with the error logged.
- The first start compiles the model for the GPU (~2 s on an Iris Xe); the
  result is cached in `diyport/models/openvino_cache` (`AIPORT_OV_CACHE_DIR`),
  so later starts load in ~0.2 s.
- The Core Ultra NPU also needs Intel's user-space driver
  ([intel/linux-npu-driver](https://github.com/intel/linux-npu-driver)) and a
  kernel with `intel_vpu`; the installer doesn't set these up.
- `auto` only picks `openvino` when an Intel GPU (`/dev/dri/renderD*`, vendor
  `0x8086`) or NPU (`/dev/accel/accel*`) is visible. On a plain CPU,
  `detector = openvino` still works, on OpenVINO's CPU plugin.
- `detector_model` takes any model the `cpu` backend does (Ultralytics YOLOv5,
  Rockchip raw heads, anchor-free YOLOv8/v9/v11 ONNX) or an OpenVINO IR `.xml`
  (with its `.bin` next to it). A GPU has the headroom for a bigger model than
  `yolov5n`.

### x86 / Raspberry Pi (CPU Docker) and Komodo

`docker/` builds one image for `linux/amd64` and `linux/arm64` (Pi 4/5); the
detector backend is selected at build time (`ACCEL=cpu` by default,
`edgetpu`, or `openvino` for an Intel GPU on amd64; pass `/dev/dri` in, see
the commented `devices:` in `compose.yaml`). The repo-root `compose.yaml` is
the single-instance stack (host networking) and the **Komodo** Stack file:

```bash
docker build -f docker/Dockerfile -t diyport-cpu .
AIPORT_CONSOLE_HOST=<console-ip> docker compose up -d --build
./docker/run-smoke-test.sh   # no console/camera needed
```

`compose.yaml` uses host networking (one instance per host: it shares the host's
`:443`/`UDP-10001`); `compose.macvlan.yaml` gives each container its own LAN
IP/MAC for several instances on one host. See [`docker/README.md`](docker/README.md)
for Komodo import, networking caveats, and the confirmed/untested matrix.

### Raspberry Pi (bare metal, optional)

The Docker stack above is the recommended Pi path and is the same command as
x86. Run bare metal only if you want the Pi 4's hardware H.264 decode (it stalls
inside a container) or no Docker. Differences from the RK3588 board: **no NPU**,
so install `onnxruntime` instead of `rknn-toolkit-lite2`, and there is **no
`.rknn`** - the CPU `yolov5n.onnx` is used.

```bash
sudo apt-get install -y ffmpeg python3-venv
python3 -m venv venv
venv/bin/pip install -r diyport/requirements.txt onnxruntime
python3 scripts/fetch_models.py --backend cpu    # downloads yolov5n.onnx
$EDITOR diyport/aiport.cfg                        # set [console] host (+ identity)
sudo venv/bin/python diyport/run_all.py --iface <iface> [--state-dir <dir>]
```

`detectors/__init__.py` selects onnxruntime automatically. On a **Pi 4**,
`avclient.py` also enables `h264_v4l2m2m` hardware decode; on a **Pi 5** it does
not (no H.264 hardware decoder - it software-decodes, which the faster CPU
handles). For reboot survival, wrap `run_all.py` in a systemd unit with
`KillMode=control-group`:

```ini
[Unit]
Description=UniFi AI Port emulator
After=network-online.target
Wants=network-online.target
[Service]
Type=simple
ExecStart=/opt/diy-port/venv/bin/python /opt/diy-port/diyport/run_all.py --iface <iface> --state-dir /var/lib/diy-port
Restart=on-failure
KillMode=control-group
[Install]
WantedBy=multi-user.target
```

### macOS (bare metal)

Verified on an Apple-silicon Mac (M1 Pro): adopted by a Protect console, 3 cameras
paired and streamed, person/vehicle detections with thumbnails. Run it on the host -
Docker Desktop's Linux VM can't put a container on the LAN as its own L2 device, so
discovery and adoption fail there. With no `/sys`, `/proc` or `ip`, `config.py` finds
the interface, MAC and IP through `route`, `networksetup` and `ifconfig` (`--iface en0`
to pick one explicitly). The detector uses onnxruntime's Core ML provider on the
Neural Engine by default: ~6.5 ms vs ~37 ms per frame on CPU (M1 Pro, `yolov5n`),
with the same detections; float32 models such as YOLOv9 run on Core ML's
NeuralNetwork format (~3 ms, M2). `AIPORT_ORT_PROVIDER=cpu` forces CPU; `coreml` requests
it (and warns, then uses CPU, if onnxruntime has no Core ML provider).

```bash
brew install ffmpeg
python3 -m venv venv
venv/bin/pip install -r diyport/requirements.txt onnxruntime
venv/bin/pip install "fast-alpr[onnx]" opencv-python-headless   # optional: plates + faces
venv/bin/python scripts/fetch_models.py --backend cpu      # downloads yolov5n.onnx
venv/bin/python scripts/fetch_models.py --face-models-only # optional: YuNet/SFace face models
$EDITOR diyport/aiport.cfg                                   # set [console] host + device_id
caffeinate -is venv/bin/python diyport/run_all.py --iface en0 --state-dir ~/.diy-port/native/state
```

No sudo is needed on macOS: since 10.14 a normal user can bind ports below 1024. Running it
with sudo leaves the state dir and log root-owned, and the launchd agent then refuses to start.

To run it as a service instead, install the launchd agent (no sudo):
`scripts/macos_launchd.sh install` starts it at login, restarts it on exit, and re-reads
the instance's `diyport.env` on touching its `restart` file; logs rotate in its
`logs/diyport.log`. The defaults are under `~/.diy-port/native/`; see `scripts/diyport_paths.sh`.
`scripts/diyport_service.sh` does the work and can also run in a terminal. See `diyport/README.md`.

- `caffeinate -is` stops the Mac sleeping while it runs (a sleeping Mac drops off the console).
- `--iface` is the default-route interface (`route -n get default`); the MAC is derived from it,
  so each Mac gets its own.
- **Every AI Port needs its own, stable `device_id`.** The default in `aiport.cfg` is shared, so on
  a second machine set a fresh one once (e.g. `uuidgen | tr A-Z a-z`) in `aiport.cfg`, or pass the
  same `--device-id` on every start; changing it makes Protect see a different device.
- A new machine (empty `--state-dir`) appears in Protect as a new "AI Port": adopt it and pair
  cameras to it. Adoption state lives in the state dir, so keep it across restarts.
- Without the optional enrichment packages it runs person/vehicle/animal only; plate models
  download to `~/.cache` on the first plate read.

### Docker (console on the same host)

When the Protect console itself runs in a container on the same box (for example
`dciancu/unifi-protect-unvr-docker-arm64` with `network_mode: host`), host
networking clashes on `:443` and a macvlan interface cannot talk to its own
host. `docker/docker-compose.yml` instead puts the AI Port on a private bridge
network: it gets its own `:443`, and host ↔ container traffic is routed without
NAT, so the console reaches it at `AIPORT_IP`.

```bash
cd docker
echo "AIPORT_CONSOLE=<console-ip>" > .env
docker compose up -d --build
```

The default backend is Coral (`AIPORT_ACCEL=edgetpu`); set `AIPORT_ACCEL=cpu`
or `none` to build a CPU image instead, or `openvino` for an Intel GPU (and
uncomment its `devices:` in `docker/docker-compose.yml`). Device identity and state persist in the
`aiport-data` volume. The trade-off: the AI Port lives on the Docker subnet
(default `172.30.10.0/24`), so only that host can reach it. See
[`docker/README.md`](docker/README.md).

### Manual install

```bash
python3 scripts/fetch_models.py --backend cpu    # or --backend edgetpu / openvino / rknn assets
pip install -r diyport/requirements.txt           # + rknn-toolkit-lite2 / ai-edge-litert / openvino / onnxruntime
$EDITOR diyport/aiport.cfg                        # identity, console IP, [hardware] detector
sudo python3 diyport/run_all.py --iface <iface>
```

### Multiple instances (board)

`instance_manager.py` creates one systemd unit per instance, each with its own
macvlan sub-interface, MAC, and DHCP lease - a distinct L2 device:

```bash
sudo python3 diyport/instance_manager.py create lab2 --parent-iface <iface>
python3 diyport/instance_manager.py list
sudo python3 diyport/instance_manager.py destroy lab2
```

### ONVIF cameras

Assigning a third-party (ONVIF) camera to the AI Port hands that camera's video
ingest to the AI Port: Protect sends the camera's RTSP URL (`OnvifStreamControl`)
and asks for each channel to be pushed to `tcp://<console>:7550` in Ubiquiti's
`extendedFlv` format (`ChangeVideoSettings`). `diyport/onvif_push.py` does that,
and the AI Port runs detection on the camera's stream like on a UniFi camera's.

There are three ways to run this video, set by `AIPORT_ONVIF_MODE`: a default, then any
per-camera exceptions by device ID (MAC), such as `isolated,AABBCCDDEEFF=copy`.

| Mode | What it does | Pushes run in | Camera sessions | Sizes sent to Protect |
|---|---|---|---|---|
| `copy` (default) | `ffmpeg` copies the camera's main stream and substream | `avclient.py` | 3, with detection | The camera's own |
| `transcode` | One `gst-launch` decodes the main stream once and encodes each channel with VideoToolbox, as the real AI Port does | `avclient.py` | 1 (2 with `AIPORT_ONVIF_SHARED_DECODE=0`) | `video1` main size at 4000 kbps, `video2` substream size at 2000 kbps, `video3` 360 lines at 500 kbps |
| `isolated` | As `transcode` | A process per camera | 1 (2 with `AIPORT_ONVIF_SHARED_DECODE=0`) | As `transcode` |

`transcode` and `isolated` need macOS and `brew install gstreamer`. On an M1 Pro a transcoded
camera used about 11% of one core and up to about 290 MB, against about 1% and 11-22 MB for
`copy`. On a machine without them, the camera logs an error and uses `copy`.

In `isolated`, a crash in the push costs that camera's video for a few seconds and the
process is replaced. With shared decode on (the default) detection reads the video1 that
process pushes, so detection also pauses until the replacement starts; with
`AIPORT_ONVIF_SHARED_DECODE=0` detection keeps its own pull. It doesn't help the other way
round: if detection's process dies, the push stops with it.

Protect doesn't always tell diy-port it was removed: it just closes each connection without
a message. An adopted instance logs `was this AI Port removed in Protect?` once this has
gone on for `AIPORT_REFUSED_WARN_S` seconds (default 180, `0` turns this off). It doesn't
clear the adoption itself, since a console problem could look the same.

The video and detection restart separately: each has its own camera pull, so losing
one leaves the other running. A transcoded camera's pipeline is replaced when a channel
has produced no video for `AIPORT_ONVIF_STALL_S` seconds (default 30, `0` turns this
off). A settings re-send from Protect reconnects only that channel's push, unless a
channel has been silent for 5 s. In every mode, the log warns
`restream: no video to Protect` when a channel has delivered nothing for
`AIPORT_STREAM_STALL_S` seconds.

`AIPORT_ONVIF_AUDIO_OFF` (empty or `0` for none, `1` for all, or a list of device IDs) is an experiment: the listed cameras' replies tell
Protect audio is off, while their pushes keep the audio track.

`AIPORT_ONVIF_CAMERA_TIMESTAMPS` (same list format) keeps the listed cameras' own frame
timestamps on the copy pull instead of stamping each frame when it arrives. Arrival stamps
(the default) pass network and RTSP-over-TCP jitter into the video Protect plays; use camera
timestamps for a camera or restream whose timestamps are regular.

If the camera's ONVIF profiles can't be read, diy-port tries again: twice within a few
seconds, then every 30 s, 2 minutes and 10 minutes. Until it succeeds, `video2` and
`video3` use the main stream.

Verified on a Raspberry Pi 5 (Docker, Coral) with a Hikvision 4K camera and
Protect 7.2: live view in the app and browser, recording, and smart-detect
events.

Tested with audio (2026-10-30), on a MacBook Pro (M1 Pro, macOS 27.0.1, native,
ffmpeg 8.0.1) as an AI Port X, Protect 7.3.70 on a UCG-Max: browser live view
plays video and audio smoothly on High and Low. The test camera, a Hikvision
ECI-T24F2, was set up as follows:

| Setting | Main stream (`video1`) | Sub-stream (`video2`/`video3`) |
|---|---|---|
| Video Type | Video and Audio Stream | Video and Audio Stream |
| Resolution | 2560×1440 | 1280×720 |
| Bit Rate Type | Variable | Variable |
| Picture Quality | Medium | Medium |
| Frame Rate | 20 fps | 20 fps |
| Max. Bit Rate | 3072 kbps | 2048 kbps |
| Video Encoding | H.264 | H.264 |
| H.264+ | Off | - |
| Profile | Medium | Medium |
| I-Frame Interval | 20 (1 s) | 20 (1 s) |
| Smoothing | 50 | 25 |
| Audio Encoding | AAC, 16 kHz (shared by both streams) | |

The camera's audio should be AAC or G.711: ffmpeg can't copy G.722.1, G.726, PCM
or MP2 into FLV, and the pull then fails.

Limits:

- **If the AI Port stops, the camera stops recording.** Protect no longer pulls
  it; unassign the camera (or remove the AI Port) to hand ingest back.
- **H.264 only.** Video is copied, not re-encoded. An H.265/HEVC camera is muxed
  by ffmpeg as Enhanced RTMP (an `hvc1` FourCC), which Protect's `ms` does not
  ingest: live view resets and the push is closed after a few hundred bytes,
  retried every second. Set the camera's stream to H.264. UniFi's own HEVC
  framing (FLV codec id 8 with a `0x68 0x01` VPS/SPS/PPS tag) is not
  implemented; such a stream is now refused with one clear error.
- `video1` carries the camera's main stream; `video2` and `video3` (UniFi's
  medium and low channels) carry its substream when the camera's ONVIF Media
  service lists a smaller H.264 profile (looked up with the RTSP URL's login;
  otherwise the main stream). The resolution
  reported to Protect is probed from the main stream, and the substream's is its
  ONVIF profile's.
- The camera's audio is copied as it is (G.711, AAC or MP3), like unifi-cam-proxy
  with `-c:a copy`: re-encoding G.711 to AAC made live view stop and start every
  second. Other audio (e.g. Opus) is re-encoded to AAC 32 kHz mono, and a camera
  without audio gets a silent AAC track. Protect asks for Opus, which plain FLV
  cannot carry. **Audio does not play in Protect yet:** G.711 is accepted but
  Protect's browser player has no audio, and the camera's AAC plays in stutters.
- Channels carrying the same stream share one RTSP session (one ffmpeg, fanned
  out); the detector opens its own. With a substream that is three sessions:
  main (`video1`), sub (`video2`/`video3`) and detection. Cameras limit
  concurrent sessions: a Hikvision answers `500` to RTSP SETUP past three, or
  `401` past its login limit.
- On a Pi 5 the camera's H.264 is decoded in software for detection (~half a
  core for 4K at 12 fps).

### Model assets (not tracked)

Each backend needs an open model. Because the binaries are large and
reproducible, they are not committed; `scripts/fetch_models.py` downloads them
(`--backend rknn|edgetpu|openvino|cpu`), sha256-pinned where a single file:

| Asset | Used by | Source |
| --- | --- | --- |
| `ssdlite_mobiledet_coco_qat_postprocess_edgetpu.tflite` (5.1 MiB) | `edgetpu` backend | Google Coral test data (Apache-2.0), the model Frigate ships: <https://github.com/google-coral/test_data> (`release-frogfish`) |
| `yolov5n.onnx` (3.8 MiB) | `cpu` / Core ML and `openvino` backends | Ultralytics YOLOv5 `v7.0` release (AGPL-3.0): <https://github.com/ultralytics/yolov5/releases/download/v7.0/yolov5n.onnx> |
| `yolov5s_relu.onnx` (27.6 MiB) | RKNN conversion (`.rknn`) | Rockchip model zoo delivery: <https://ftrg.zbox.filez.com/v2/delivery/data/95f00b0fc900458ba134f8b180b3f7a1/examples/yolov5/yolov5s_relu.onnx> |
| anchors, COCO labels, bus.jpg, calibration subset | RKNN conversion | <https://github.com/airockchip/rknn_model_zoo> (tag `v2.3.2`) |

The CPU backend uses Ultralytics `yolov5n.onnx` and still accepts the Rockchip
`yolov5s_relu.onnx` via `AIPORT_MODEL_PATH`, and anchor-free YOLOv8/v9 exports such as
Frigate's `yolov9-s-320.onnx` (all three layouts are auto-detected; the input size is read
from the model). The
Dockerfile downloads and sha256-verifies the CPU model at build time.
The `edgetpu` backend also takes an Ultralytics YOLOv5 Edge TPU export via `[hardware]
detector_model` / `AIPORT_MODEL_PATH`. `diyport/models/coco_90_labels.txt` (the MobileDet
class ids) is Google's `coco_labels.txt`, Apache-2.0.

`yolov5s_relu.rknn` is produced on an x86_64 machine with `rknn-toolkit2`
(see `rknn_convert/convert.py` and `diyport/README.md`):

```bash
python3 scripts/fetch_models.py --convert
```

`scripts/install.sh` resolves the prebuilt `.rknn` in order: `--rknn <path>`, a
local `models-rknn/` copy (e.g. a checkout of `hutchx86/pi-port-models`) or
`rknn_convert/` copy, then a download from
`--rknn-url` / `DIYPORT_RKNN_URL` / `DEFAULT_RKNN_URL`. The digest is verified
when you pass `--rknn-sha256`, or always for the default URL; a custom URL or
local copy without the flag is installed unverified and logged as such.
The default source is the separate AGPL-3.0 model repo
[`hutchx86/pi-port-models`](https://github.com/hutchx86/pi-port-models) (tag
`v1`); the model is never committed here.

**Model license.** The YOLOv5 models derive from Ultralytics YOLOv5 (AGPL-3.0)
via Rockchip's model zoo (Apache-2.0), so any redistributed `.rknn` is an
AGPL-3.0 work, the same license as this project. The MobileDet model and the
90-id COCO label list are Apache-2.0 (Google Coral). Include the relevant license
text and attribution if you redistribute them; the corresponding source for the
`.rknn` is this repo's conversion recipe plus the ONNX above.

## Configuration

Device identity, network mode, the console address and the detector live in
`diyport/aiport.cfg`; CLI flags on each script override it, and a small set of
`AIPORT_*` environment variables override it too (container-friendly).

| Key | Default | Meaning |
| --- | --- | --- |
| `[identity] mac` | derived | Device MAC (12 hex chars). Blank derives `FC:EC:DA:<interface's last 3 octets>` |
| `[identity] name` | `AI Port` | Advertised device name (env `AIPORT_NAME`); a Protect rename is persisted separately |
| `[identity] platform` | `UVC AI Port` | Catalog model string the console matches (must be exact) |
| `[identity] discovery_platform` | `UP-AI-Port` | Discovery TLV `0x0C` SKU string (blank falls back to `platform`) |
| `[identity] sysid` | `0xa5f1` | Hardware/model id |
| `[identity] guid` | fixed catalog GUID | AI Port SKU GUID; must not be random |
| `[identity] device_id` | UUID in `aiport.cfg` | Persistent device UUID; `instance_manager.py`/the container entrypoint generate a unique one per instance |
| `[network] iface` / `mode` / `ip` | auto / `dhcp` / - | Interface to bind, and `dhcp` or `static` addressing |
| `[console] host` / `port` | - / `7442` | The Protect console to dial when not yet adopted |
| `[hardware] detector` | `auto` | `auto`, `rknn`, `edgetpu`, `openvino`, `cpu` or `none` (env `AIPORT_DETECTOR`) |
| `[hardware] detector_model` | - | Model file for that backend; blank = its default in `models/` (env `AIPORT_MODEL_PATH`) |

Detector/runtime environment overrides: `AIPORT_DETECTOR`, `AIPORT_MODEL_PATH`,
`AIPORT_OBJ_THRESH`, `AIPORT_HWDEC`, `AIPORT_HW_SCALE`, `AIPORT_FRAME_JPEG_FPS`,
`AIPORT_ORT_INTRA_THREADS`, `AIPORT_ORT_PROVIDER`, `AIPORT_OV_DEVICE`, `AIPORT_OV_CACHE_DIR`,
`AIPORT_TICK_S`, `AIPORT_DETECT_FPS`,
`AIPORT_CONFIRM_MS`, `AIPORT_MISS_S`, `AIPORT_TRUE_THRESH`, `AIPORT_CROSS_TYPE_IOU`, `AIPORT_RESUME_S`,
`AIPORT_MIN_INITIALIZED`, `AIPORT_BOX_DELAY_MS`, `AIPORT_MOTION_GATE` /
`AIPORT_MOTION_THRESHOLD` / `AIPORT_MOTION_CONTOUR_AREA` / `AIPORT_MOTION_GATE_THRESH`,
`AIPORT_REGIONS` / `AIPORT_DETECT_SIZE`, `AIPORT_TRACKER`, `AIPORT_LENS_FILTER` /
`AIPORT_LENS_MIN_HEIGHT`, `AIPORT_SNAPSHOT_URL_GUARD` (default on; 0 disables the
controller-upload target check), `AIPORT_RESET_GRACE_S` (default 15 s; 0 stops
streams immediately on `ResetAIPortStreams`).

Enrichment: `AIPORT_VEHICLE_ATTRS`, `AIPORT_VEHICLE_FM`, `AIPORT_LPR` /
`AIPORT_LPR_MIN_PX` / `AIPORT_LPR_THRESH`, `AIPORT_FACE` / `AIPORT_FACE_THRESH` /
`AIPORT_FACE_MATCH_*` / `AIPORT_FACE_EMBED_MIN_*` / `AIPORT_FACE_DB_THRESH`,
`AIPORT_FACES_DIR` / `AIPORT_FACES_SEEN_DIR`.

Host and operations: `AIPORT_HOST_STATS`, `AIPORT_POINTS_SCALE`, `AIPORT_STREAM_STALL_S`,
`AIPORT_RTSP_TIMEOUT_S`, `AIPORT_ADVERTISE_IP` (NAT/Docker), and the experimental
`AIPORT_CAMERA_CLOCK` / `AIPORT_CLOCK_WALL_SKEW_MS`. Details in `diyport/README.md`.
The old `AIPORT_BACKEND=auto|rknn|onnx` name is still accepted (`onnx` = `cpu`).
`AIPORT_ORT_PROVIDER` is `auto` by default (Core ML when onnxruntime offers it, else
CPU); `cpu` forces CPU and `coreml` requests Core ML (CPU with a warning if absent).

## Verification

- Unit suite (no hardware required):

  ```bash
  cd diyport && python3 -m unittest discover -s tests
  ```

  Tests needing OpenCV, ffmpeg, Norfair or macOS skip without them. The enrichment check
  on real images runs when `AIPORT_ENRICH_SAMPLES` names a folder holding `plate.png` and
  `lena.jpg`, with fast-alpr and the face models installed.

- Container smoke test (no console/camera required):

  ```bash
  ./docker/run-smoke-test.sh
  ```

- Replay harness (tracking, faces, regions): record a clip with
  `scripts/record_clip.py`, then run `python3 diyport/replay.py <clip> --check <dir>`.
  This is the pre-change check for detection/tracking work; see `diyport/README.md`.

- On-device: run the stack and watch it discover, adopt and pair (see
  `diyport/README.md` and `scripts/live_watch.py`).

## Roadmap / known limitations

- **Known:** detection is a stock COCO model (person/vehicle/animal), not Ubiquiti's
  classifier. Optional enrichment (`enrich.py`) adds vehicle type/colour, plates and
  faces; faces are matched against Protect's known-faces DB (`UpdateFaceDBRequest`), but
  the attribute and plate field mapping is a **best-guess wire format** not confirmed
  against a real AI Port capture.
- **Gap: no motion events for paired cameras.** While a camera is paired, Protect
  ignores the camera's own motion events (`EventSmartMotion`/`EventAnalytics`) and
  takes them from the AI Port, but diy-port doesn't send any yet. Paired cameras show
  no motion-only events, motion-triggered recording only follows smart detections,
  and motion alerts don't fire. diy-port already runs a motion detector on every
  frame; the missing piece is the exact `EventSmartMotion` start/pulse/stop payload
  Protect expects. See `docs/protect-aiport-controller.md`.
- **Gap: no audio detection for paired cameras.** Protect also drops a paired camera's
  own `EventSmartAudio` (smoke/CO alarm, glass break, etc.), and diy-port's feature
  flags list no audio types, so those detections are turned off while the camera is
  paired.
- **Known:** an assigned ONVIF camera depends on the AI Port for its video (no
  recording while the AI Port is down). video1 carries the main stream and
  video2/video3 the camera's substream; audio is copied (G.711, AAC or MP3) or
  re-encoded to AAC when FLV cannot carry it. Opus is not pushed.
- **Known:** in the same-host Docker setup the AI Port is only reachable from
  its own host, so it only suits a console running on that host.
- **Known:** on the Raspberry Pi 4 the H.264 hardware decoder is instance-limited
  and known-flaky; the Pi 5 has no H.264 hardware decoder at all. See
  `docker/README.md` and `diyport/README.md`.
- **Known:** the Pi 5 `install.sh` path is untested against a console (the Docker
  path is verified); the CPU container has been verified live on a Pi 4 and the
  smoke test covers x86_64 (macOS bare metal is verified live).
- **Known:** macOS is verified on Apple silicon only, bare metal; Intel Macs are
  untested. Docker Desktop can't provide the L2 device the console expects.
- **Planned:** stress-testing beyond the four-camera live setup.

Release history: [docs/changelog.md](docs/changelog.md).

## Troubleshooting

<details>
<summary><b>Troubleshooting / operational notes</b></summary>

**A partial restart leaves stale connections.** Restart the full four-process
stack (discovery, adopt HTTP, ucp4, avclient). `run_all.py` starts all four
together.

**`pkill -f 'run_all.py'` kills itself.** Use the bracket trick, and kill
orphaned ffmpeg children too:

```bash
pkill -9 -f '[r]un_all.py'
pkill -9 -f '[f]fmpeg.*split=2'        # paired-camera/ONVIF detection pulls
pkill -9 -f '[f]fmpeg.*streamName='    # ONVIF video pushes
```

**Starting detached over SSH hangs.** Use
`setsid -f <command> >log 2>&1 </dev/null`.

**Multiple instances collide on `:443`.** Bind each instance to its own
interface's real IP (the default) rather than `0.0.0.0`.

**The Coral stops answering ("Failed to load delegate").** An earlier failed
open can wedge the USB stick until it is replugged. Unplug and replug it; no
driver install fixes it.

**The Mac drops off the console when it sleeps.** Run under
`caffeinate -is` (see [macOS](#macos-bare-metal)).

</details>

## Credits

Special thanks to
[dciancu/unifi-protect-unvr-docker-arm64](https://github.com/dciancu/unifi-protect-unvr-docker-arm64),
the inspiration for getting into UniFi tinkering, and whose methods helped in
learning how parts of it work. Modular detector backends, Coral Edge TPU support
and the ONVIF video push were contributed by [@issue](https://github.com/issue);
macOS support (interface/MAC/IP detection and the Core ML detector) by
[@NorthernMan54](https://github.com/NorthernMan54).
Protocol work was informed by the open-source UniFi community; the detection
models and conversion recipe come from Rockchip's model zoo and Google Coral.
Third-party components and licenses are listed in [CREDITS.md](CREDITS.md).

<a id="legal"></a>
<details>
<summary><b>Legal</b></summary>

- **Not affiliated with, or endorsed by, Ubiquiti Inc.** "UniFi", "UniFi
  Protect" and "AI Port" are trademarks of Ubiquiti Inc.
- **No Ubiquiti firmware or binaries are distributed here.** You supply your own
  console, cameras and (for reverse engineering) firmware images.
- Intended for interoperability and personal use on hardware and consoles you
  own. Reverse-engineering may be restricted in your jurisdiction, and you are
  responsible for how you use this. Nothing here targets third-party systems.

</details>

<a id="disclaimer"></a>
<details>
<summary><b>Disclaimer</b></summary>

**This is a proof-of-concept project, not a production-ready system.** Large
parts were produced with LLMs under human supervision, review, and real-hardware
testing - review and verify everything yourself. **This software is provided "as
is", without warranty of any kind.** It binds privileged ports, manipulates
network interfaces (macvlan/DHCP), and runs detection hardware; misconfiguration
can disrupt your network or device. By using it you accept full responsibility for
any damage, data loss, or other consequences. **The authors and contributors are
not responsible or liable for any loss or damage arising from its use.**

</details>

## Security

See [SECURITY.md](SECURITY.md) for the trusted-LAN boundary. Report
vulnerabilities privately via GitHub Security Advisories. Runtime state -
the adoption token and the mTLS client certificate/key - belongs in the instance
state directory passed as `--state-dir`; the service and launchd installers
always pass one. A manual run defaults to `$XDG_STATE_HOME/diyport` (or
`~/.local/state/diyport`); on first start `run_all.py` copies any old
repo-local state there. No secrets are stored in this repository. Logged payloads are passed through a redaction filter for camera
RTSP credentials, the console's login password and adoption tokens.

## License

AGPL-3.0-or-later. See [LICENSE](LICENSE). Because this is a network service,
anyone who runs a modified version for others to interact with over a network
must offer them the corresponding source.
