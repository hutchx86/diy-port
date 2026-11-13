# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 hutchx86
"""Video push for third-party (ONVIF) cameras assigned to the AI Port.

When an ONVIF camera is assigned, Protect stops ingesting it itself and hands
the job to the AI Port: OnvifStreamControl carries the camera's RTSP URL, and
ChangeVideoSettings asks for each channel (video1/2/3) as an "extendedFlv"
stream pushed to tcp://<console>:7550 under a given streamName. Without that
push the camera shows no video and records nothing.

OnvifStreamControl names only the camera's main profile. video2 and video3, the
medium (720p) and low channels on a UniFi camera, get the camera's substream
instead when its ONVIF Media service lists a smaller H.264 profile
(onvif_media.py); video1 stays on the main one. Both share one pull, so the
camera sees no extra RTSP session.

Each push is `ffmpeg -c:v copy -f flv -` (no re-encode) piped through
rewrite_flv() into a TCP socket. The rewrite mirrors unifi-cam-proxy's
clock_sync.py (MIT, keshavdv/unifi-cam-proxy): header flags byte 7, a 16-byte
timing trailer after every tag, and onClockSync/onMpma script tags every 5s.
One deliberate difference: the trailer's elapsed time is written in the
timescale the trailer itself declares (90 kHz video, 11025 otherwise).
clock_sync.py writes 10-microsecond units for both, which Protect 7.x reads as
video running 11% and audio 9x too fast; its media server then drops the frames
as "from the future" and the web player never starts.
"""
import collections
import hashlib
import logging
import os
import shutil
import queue
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import parse_qs, urlsplit

import envutil
import onvif_media
from procutil import start_stderr_drain

log = logging.getLogger("aiport-onvif")

# video2/video3 carry the camera's substream when it has one; UniFi medium/low are 720p/360p.
SUB_CHANNELS = ("video2", "video3")

# AIPORT_ONVIF_MODE: copy, transcode or isolated (per-camera exceptions allowed). See
# docs/onvif-modes.md for the pipelines, G3 clock syncs and the live measurements.
COPY, TRANSCODE, ISOLATED = "copy", "transcode", "isolated"
_MODE_NAMES = (COPY, TRANSCODE, ISOLATED)


def _camera_id(value):
    return value.strip().replace(":", "").upper()


def parse_camera_list(value):
    """A setting naming cameras: empty or 0 for none, 1 for all, else False, True or their IDs."""
    value = (value or "").strip().lower()
    if value in ("", "0", "false", "no", "off"):
        return False
    if value in ("1", "true", "yes", "on"):
        return True
    return {_camera_id(d) for d in value.split(",") if d.strip()}


# AIPORT_ONVIF_CAMERA_TIMESTAMPS (camera list, as AIPORT_ONVIF_AUDIO_OFF): keep those cameras'
# own frame timestamps instead of arrival stamps, which carry network jitter into the video.
CAMERA_TIMESTAMPS = parse_camera_list(os.environ.get("AIPORT_ONVIF_CAMERA_TIMESTAMPS"))


def use_camera_timestamps(device_ids, setting=None):
    """Whether a pull feeding these cameras keeps the camera's timestamps."""
    setting = CAMERA_TIMESTAMPS if setting is None else setting
    if setting is True:
        return True
    return bool(setting) and any(_camera_id(d) in setting for d in device_ids)


def parse_modes(value):
    """AIPORT_ONVIF_MODE, "<default>[,<deviceID>=<mode>...]" -> (default, {deviceID: mode}). A bad
    entry is logged and left out, so that camera gets the default."""
    default, per_camera = COPY, {}
    for part in (value or "").split(","):
        part = part.strip()
        if not part:
            continue
        camera, _, mode = part.rpartition("=")
        mode = mode.strip().lower()
        if mode not in _MODE_NAMES:
            log.warning("ignoring bad AIPORT_ONVIF_MODE entry %r: the modes are %s", part,
                        ", ".join(_MODE_NAMES))
        elif camera:
            per_camera[_camera_id(camera)] = mode
        else:
            default = mode
    return default, per_camera


MODES = parse_modes(os.environ.get("AIPORT_ONVIF_MODE"))
_fell_back = set()  # cameras already logged as falling back to copy
_codecs = {}        # camera id -> probed video codec (h264, hevc, ...)
_codec_fell_back = set()


def note_codec(device_id, codec):
    """Remember the probed video codec; None leaves the last known value alone."""
    if codec:
        _codecs[_camera_id(device_id)] = str(codec).lower()


def transcode_available():
    """Option C needs macOS (VideoToolbox) and gst-launch. Before this, a camera set to it on a
    machine without them had no video at all, retrying forever."""
    return sys.platform == "darwin" and os.path.exists(GST_BIN)


def mode_of(device_id):
    """How this camera's video runs: copy where a transcode can't run here."""
    default, per_camera = MODES
    mode = per_camera.get(_camera_id(device_id), default)
    if mode != COPY and not transcode_available():
        if device_id not in _fell_back:
            _fell_back.add(device_id)
            log.error("camera %s is set to %s, which needs macOS and gst-launch (%s; `brew install "
                      "gstreamer`): using copy instead", device_id, mode, GST_BIN)
        return COPY
    return mode


def transcodes(device_id):
    mode = mode_of(device_id)
    if mode == COPY:
        return False
    key = _camera_id(device_id)
    codec = _codecs.get(key)
    if codec and codec not in ("h264", "avc"):
        if key not in _codec_fell_back:
            _codec_fell_back.add(key)
            log.error("camera %s is %s; transcode/isolated need H.264, using copy", device_id,
                      codec)
        return False
    return True


def warn_old_settings(environ=None):
    """Settings AIPORT_ONVIF_MODE replaced: left in place, they would silently do nothing."""
    environ = os.environ if environ is None else environ
    for old in ("AIPORT_ONVIF_TRANSCODE", "AIPORT_ONVIF_SUBPROCESS"):
        if (environ.get(old) or "").strip():
            log.warning("%s is no longer read: use AIPORT_ONVIF_MODE (%s)", old,
                        ", ".join(_MODE_NAMES))


TRANSCODE_CHANNELS = ("video1", "video2", "video3")
TRANSCODE_KBPS = dict(zip(TRANSCODE_CHANNELS, (4000, 2000, 500)))
G3_SYNC_FALLBACK_S = 10.0
CLOCK_SYNC_INTERVAL_S = 5.0
GST_LATENCY_MS = 500
GST_BIN = (shutil.which("gst-launch-1.0")
           or "/opt/homebrew/bin/gst-launch-1.0")  # launchd's PATH has no Homebrew
TRANSCODE_VIDEO3_HEIGHT = 360
# Replace a transcode pipeline with no video for this long (0: never). A gst hang can
# keep its RTSP socket closed with no output; Protect re-sends alone will not end it.
TRANSCODE_STALL_S = envutil.env_float("AIPORT_ONVIF_STALL_S", 30)
# A re-send only replaces the pipeline when a channel has been silent this long; otherwise
# just reconnect that push, since a restart costs all channels and a camera session.
RESEND_RESTART_QUIET_S = 5.0


def transcode_video3_size(width, height):
    """video3's (width, height): 360 lines at the camera's aspect, width even for H.264
    (2560x1440 gives 640x360). None if the camera's size isn't known."""
    if not width or not height:
        return None
    return round(TRANSCODE_VIDEO3_HEIGHT * width / height / 2) * 2, TRANSCODE_VIDEO3_HEIGHT


_TAG_AUDIO = 8
_TAG_VIDEO = 9
_TAG_SCRIPT = 18
# Trailer timescales as captured from real cameras by unifi-cam-proxy:
# 0x015F90 = 90000 (video clock), 0x2B11 = 11025 for everything else.
_TIMESCALE_VIDEO = 90000
_TIMESCALE_OTHER = 11025


# --- AMF0 / FLV encoding (just what the two script tags need) ----------------

def _amf_string(s):
    raw = s.encode()
    return struct.pack(">H", len(raw)) + raw


def _amf_value(value):
    if isinstance(value, dict):  # AMF0 anonymous object
        body = b"".join(_amf_string(k) + _amf_value(v) for k, v in value.items())
        return b"\x03" + body + b"\x00\x00\x09"
    return b"\x00" + struct.pack(">d", float(value))  # AMF0 number


def _flv_tag(tag_type, payload, timestamp):
    """One FLV tag plus its trailing PreviousTagSize."""
    ts = struct.pack(">i", int(timestamp))
    return (bytes([tag_type]) + struct.pack(">I", len(payload))[1:]
            + ts[1:] + ts[:1] + b"\x00\x00\x00"
            + payload + struct.pack(">I", len(payload) + 11))


def script_tag(name, data, timestamp=0):
    return _flv_tag(_TAG_SCRIPT, b"\x02" + _amf_string(name) + _amf_value(data), timestamp)


def timing_trailer(is_video, elapsed_s):
    """16 bytes: 0, the 24-bit timescale, 4 zero bytes, then elapsed time in
    that timescale as 64-bit big-endian (identical to the 32-bit layout below
    2**32 ticks, ~13 h of video; no wrap after that)."""
    scale = _TIMESCALE_VIDEO if is_video else _TIMESCALE_OTHER
    return (b"\x00" + scale.to_bytes(3, "big") + b"\x00" * 4
            + struct.pack(">Q", int(elapsed_s * scale)))


_MPMA = {
    "cs": {"cur": 1500000, "max": 1500000, "min": 1500000},
    "m": {"cur": 1500000, "max": 1500000, "min": 1500000},
    "r": 0,
    "sp": {"cur": 1500000, "max": 1500000, "min": 1500000},
    "t": 75000.0,
}


class UnsupportedCodec(ValueError):
    """The source's FLV video codec is not H.264.

    Protect's extendedFlv ingest (ms) only takes AVC here. ffmpeg's FLV muxer
    writes HEVC as Enhanced RTMP: byte0 0x90/0x91/0xA1 with an `hvc1` FourCC
    after it and an hvcC record, which ms does not ingest at all -- it reports
    videoCodec=VUNK, resets live view and closes the push. UniFi's own HEVC
    flavour uses FLV codec id 8 plus a `0x68 0x01` VPS/SPS/PPS config tag, which
    this module does not emit.
    """

    def __init__(self, codec):
        self.codec = codec
        super().__init__(
            f"unsupported video codec ({codec}): Protect only ingests H.264 "
            f"here -- an H.265/HEVC camera must be set to H.264")


def video_codec(payload):
    """Codec label for a FLV video-tag payload; 'h264' is the one we can push.

    Classic tags carry the codec id in the low nibble (7 = AVC). FFmpeg muxes
    HEVC/AV1/VP9 as Enhanced RTMP instead: byte0 has 0x80 set and a FourCC
    follows ('hvc1', ...), so there is no codec id nibble at all.
    """
    if not payload:
        return "empty"
    if payload[0] & 0x80:
        fourcc = bytes(payload[1:5]).decode("latin-1", "replace")
        return "hevc (enhanced flv)" if fourcc in ("hvc1", "hev1") else fourcc
    codec_id = payload[0] & 0x0F
    return "h264" if codec_id == 7 else f"FLV codec id {codec_id}"


def _read_exact(read, n):
    buf = b""
    while len(buf) < n:
        chunk = read(n - len(buf))
        if not chunk:
            return buf
        buf += chunk
    return buf


def _tag_timestamp(tag_header):
    return struct.unpack(">i", tag_header[7:8] + tag_header[4:7])[0]


def _retime(tag_header, timestamp):
    """The tag header with its timestamp (24 bits + extended byte) replaced."""
    ts = struct.pack(">i", max(0, int(timestamp)))
    return tag_header[:4] + ts[1:] + ts[:1] + tag_header[8:]


def read_flv_header(read):
    """FLV header plus PreviousTagSize0; ValueError on a non-FLV source."""
    header = _read_exact(read, 9)
    if len(header) < 9 or header[:3] != b"FLV":
        raise ValueError(f"not an FLV stream: {header[:3]!r}")
    return header + _read_exact(read, 4)


class ExtendedFlvWriter:
    """Protect's extendedFlv to write(bytes): header flags 7, a timing trailer
    after every tag, onClockSync/onMpma every 5 s. One per connection."""

    def __init__(self, write, clock=time.time, early_sync=False, g3_sync=False):
        self.write = write
        self.g3_sync = g3_sync
        self.clock = clock
        self.now = self.start = self.last_sync = None
        # Sync immediately after the first video frame: Protect's ms drops a recording that closes
        # before its first sync.
        self.early_sync = early_sync
        self._sync_next = False
        self.on_sync = None  # (streamClock ms, wallClock ms) of each onClockSync sent

    def _synced(self, stream_ms, wall_ms):
        if self.on_sync:
            self.on_sync(stream_ms, wall_ms)

    def header(self, header):
        self.write(header[:4] + b"\x07" + header[5:9])
        self.write(header[9:13])  # PreviousTagSize0
        # Never before the first frames, as clock_sync.py has it: Protect closes the
        # connection when an onClockSync arrives ahead of them.
        self.last_sync = self.clock()
        self.start = self.clock()  # read separately, as clock_sync.py does

    def initial_sync(self):
        """G3 connection start: onMpma then onClockSync at stream time 0 (yi-protect)."""
        now = self.now = self.last_sync = self.clock()
        self.write(script_tag("onMpma", _MPMA, 0))
        self.write(timing_trailer(False, now - self.start))
        self.write(script_tag("onClockSync", {"streamClock": 0, "streamClockBase": 0,
                                              "wallClock": now * 1000}, 0))
        self.write(timing_trailer(False, now - self.start))
        self._synced(0, now * 1000)

    def sync_if_due(self, timestamp, keyframe=False):
        """Once per tag, before it: the clock sync when it is due."""
        now = self.now = self.clock()
        due = ((keyframe or now - self.last_sync >= G3_SYNC_FALLBACK_S) if self.g3_sync
               else now - self.last_sync >= CLOCK_SYNC_INTERVAL_S)
        if self._sync_next or due:
            self._sync_next = False
            self.last_sync = now
            self.write(script_tag("onClockSync", {"streamClock": int(timestamp),
                                                  "streamClockBase": 0,
                                                  "wallClock": now * 1000}, timestamp))
            self.write(timing_trailer(False, now - self.start))
            self._synced(int(timestamp), now * 1000)
            self.write(script_tag("onMpma", _MPMA, 0))
            self.write(timing_trailer(False, now - self.start))

    def tag(self, tag_header, body):
        """A whole tag (body = payload + PreviousTagSize) and its trailer."""
        self.write(tag_header + body)
        self.write(timing_trailer(tag_header[0] == _TAG_VIDEO, self.now - self.start))
        if self.early_sync and tag_header[0] == _TAG_VIDEO and len(body) > 5 and body[1] == 1:
            self.early_sync = False  # the first video frame (AVC NALU, not its config)
            self._sync_next = True


def read_tags(read):
    """(tag_header, body) for each whole tag after the header; body is the
    payload plus PreviousTagSize. Ends with the source (a cut-off tag is dropped)."""
    while True:
        tag_header = _read_exact(read, 11)
        if len(tag_header) < 11:
            return
        body = _read_exact(read, int.from_bytes(tag_header[1:4], "big") + 4)
        yield tag_header, body
        if len(body) < int.from_bytes(tag_header[1:4], "big") + 4:
            return


def drain_socket(sock, name=""):
    """Read and discard what Protect sends back on a push until the socket closes.
    Live 2026-11-01: Protect writes ~25 B/s on some pushes and nothing read it, so
    140 KB had piled up per socket after 90 min; once the window closes Protect's
    writes block. unifi-cam-proxy's nc drains its socket the same way. Still piling
    up at 21:36 with the audio changes reverted, so it isn't an audio side effect."""
    def run():
        while True:
            try:
                if not sock.recv(4096):
                    return
            except socket.timeout:  # the push's 30 s send timeout; quiet is fine here
                continue
            except OSError:
                return
    thread = threading.Thread(target=run, daemon=True, name=f"push-drain-{name}")
    thread.start()
    return thread


def check_video_codec(tag_header, body):
    if tag_header[0] == _TAG_VIDEO and len(body) > 4:
        codec = video_codec(body)
        if codec != "h264":
            raise UnsupportedCodec(codec)


def rewrite_flv(read, write, clock=time.time):
    """Copy a plain FLV stream from read(n) to write(bytes) as Protect's
    extendedFlv. Returns when the source ends; raises ValueError on a non-FLV
    source. write() errors (socket gone) propagate."""
    writer = ExtendedFlvWriter(write, clock)
    writer.header(read_flv_header(read))
    for tag_header, body in read_tags(read):
        writer.sync_if_due(_tag_timestamp(tag_header))
        if len(body) < int.from_bytes(tag_header[1:4], "big") + 4:
            write(tag_header + body)
            return
        check_video_codec(tag_header, body)
        writer.tag(tag_header, body)


def set_stream_name(tag_header, body, name):
    """ffmpeg's onMetaData tag with its streamName value replaced: one pull
    feeds several pushes, each under the streamName Protect gave its channel."""
    payload = body[:-4]
    key = b"\x00\x0astreamName\x02"
    at = payload.find(key)
    if tag_header[0] != _TAG_SCRIPT:
        return tag_header, body
    if at >= 0:
        start = at + len(key)
        end = start + 2 + struct.unpack(">H", payload[start:start + 2])[0]
        payload = payload[:start] + _amf_string(name) + payload[end:]
    else:
        # GStreamer's flvmux writes no streamName: add it as the ECMA array's first entry.
        head = b"\x02" + _amf_string("onMetaData") + b"\x08"
        if not payload.startswith(head):
            return tag_header, body
        count = struct.unpack(">I", payload[len(head):len(head) + 4])[0] + 1
        payload = (head + struct.pack(">I", count) + key + _amf_string(name)
                   + payload[len(head) + 4:])
    tag = _flv_tag(_TAG_SCRIPT, payload, _tag_timestamp(tag_header))
    return tag[:11], tag[11:]


# --- push processes -----------------------------------------------------------

def parse_destination(dest):
    """tcp://host:port?retryInterval=1&connectTimeout=5 -> (host, port, retry_s, timeout_s)."""
    parts = urlsplit(dest)
    if parts.scheme != "tcp" or not parts.hostname or not parts.port:
        raise ValueError(f"unsupported destination {dest!r}")
    query = parse_qs(parts.query)
    retry = float(query.get("retryInterval", ["1"])[0])
    timeout = float(query.get("connectTimeout", ["5"])[0])
    return parts.hostname, parts.port, retry, timeout


_BAD_URL_CHARS = set(' \t\r\n!"\\')


def validate_stream_url(url):
    """Only a normal RTSP(S) URL may reach ffmpeg or gst-launch."""
    if not isinstance(url, str) or not url or any(c in _BAD_URL_CHARS for c in url) \
            or any(ord(c) < 32 for c in url):
        raise ValueError("stream URL is not a plain RTSP URL")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as exc:
        raise ValueError(f"bad stream URL: {exc}") from exc
    if parts.scheme.lower() not in ("rtsp", "rtsps") or not parts.hostname:
        raise ValueError("stream URL must be rtsp:// or rtsps://")
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("stream URL has an invalid port")
    if parts.fragment:
        raise ValueError("stream URL must not have a fragment")
    return url


def ffmpeg_push_cmd(ffmpeg_bin, source_url, stream_name, camera_audio=True, copy_audio=True,
                    wallclock=True):
    """Camera video copied (no re-encode) plus an audio track into FLV on stdout.
    Protect's ingest only marks a stream ready (live view, recording) with both
    tracks present -- the extendedFlv header always flags audio+video -- so a
    camera without audio gets a silent AAC track.

    The camera's audio is copied (FLV carries G.711 and AAC as they are), as
    unifi-cam-proxy runs here with -c:a copy. Re-encoding G.711 to AAC made
    Protect's live view stop and start every second (live 2026-10-28);
    `copy_audio=False` re-encodes, for audio FLV can't hold (e.g. Opus).
    `wallclock=False` keeps the camera's timestamps (see CAMERA_TIMESTAMPS)."""
    validate_stream_url(source_url)
    cmd = [
        ffmpeg_bin, "-nostdin", "-loglevel", "error",
        "-avoid_negative_ts", "make_zero", "-fflags", "+genpts+discardcorrupt",
    ] + (["-use_wallclock_as_timestamps", "1"] if wallclock else []) + [
        "-timeout", "15000000",
        "-protocol_whitelist", "rtsp,tcp,udp,rtp,tls",
        "-rtsp_transport", "tcp", "-i", source_url,
    ]
    aac = ["-c:a", "aac", "-ar", "32000", "-ac", "1", "-b:a", "32k"]
    if camera_audio:
        cmd += ["-map", "0:v:0", "-map", "0:a:0"] + (["-c:a", "copy"] if copy_audio else aac)
    else:
        cmd += ["-f", "lavfi", "-i", "anullsrc=r=32000:cl=mono",
                "-map", "0:v:0", "-map", "1:a:0", "-shortest"] + aac
    return cmd + [
        "-c:v", "copy",
        "-metadata", f"streamName={stream_name}", "-f", "flv", "-",
    ]


# Transcode tries each audio depayloader because one pipeline cannot copy every camera
# format; it re-encodes to AAC like ffmpeg's fallback.
GST_AUDIO = ("pcmu", "pcma", "aac", "encode")
_GST_AAC = ["audioconvert", "!", "audioresample", "!", "audio/x-raw,rate=32000,channels=1",
            "!", "avenc_aac", "bitrate=32000", "!", "aacparse", "!", "mux.audio"]
_GST_DEPAY = {"pcmu": ["rtppcmudepay", "!", "mux.audio"],
              "pcma": ["rtppcmadepay", "!", "mux.audio"],
              "aac": ["rtpmp4gdepay", "!", "aacparse", "!", "mux.audio"],
              "encode": ["decodebin", "!"] + _GST_AAC}


def transcode_cmd(gst_bin, source_url, outputs, camera_audio=True, audio="pcmu",
                  latency_ms=GST_LATENCY_MS):
    """TRANSCODE's pipeline: rtspsrc with the AI Port's settings, the video decoded once and teed to
    one encoder + flvmux per output, each written to its own fd. outputs is
    [(fd, width, height, fps, kbps, gop)]; a None size or fps keeps the camera's. The audio
    (copied, or silent AAC) is teed to every mux."""
    validate_stream_url(source_url)
    cmd = [gst_bin, "-q",
           "rtspsrc", f"location={source_url}", "protocols=tcp", f"latency={int(latency_ms)}",
           "tcp-timeout=5000000", "teardown-timeout=2000000000", "name=src",
           "src.", "!", "application/x-rtp,media=video", "!", "rtph264depay", "!",
           "h264parse", "!", "vtdec", "!", "tee", "name=vt"]
    if camera_audio:
        cmd += ["src.", "!", "application/x-rtp,media=audio", "!"] + _GST_DEPAY[audio][:-2]
    else:
        cmd += ["audiotestsrc", "wave=silence", "is-live=true", "!"] + _GST_AAC[:-2]
    cmd += ["!", "tee", "name=at"]
    for fd, width, height, fps, kbps, gop in outputs:
        caps = "video/x-raw,format=NV12"
        if width and height:
            caps += f",width={int(width)},height={int(height)}"
        if fps:
            caps += f",framerate={int(fps)}/1"
        # Small queues: a default one holds 200 raw frames (over 1 GB at 2560x1440).
        cmd += ["vt.", "!", "queue", "max-size-buffers=3", "!", "videoscale", "!",
                "videorate", "!", "videoconvert", "!", caps, "!",
                "vtenc_h264", "realtime=true", "allow-frame-reordering=false",
                f"bitrate={int(kbps)}", f"max-keyframe-interval={int(gop)}", "!",
                "h264parse", "!", "video/x-h264,stream-format=avc,alignment=au", "!",
                f"mux{fd}.video",
                "at.", "!", "queue", "!", f"mux{fd}.audio",
                "flvmux", f"name=mux{fd}", "streamable=true", "!",
                "fdsink", f"fd={fd}", "sync=true"]
    return cmd


# FLV sound formats (high nibble of an audio tag) Protect takes copied: MP3, G.711 A-law and
# mu-law, AAC. ffmpeg 8 copies e.g. Opus too, as Enhanced FLV (9), like the HEVC ms refuses.
_COPYABLE_AUDIO = (2, 7, 8, 10)


class _AudioNotCopyable(ValueError):
    """The camera's audio came out of a copy in a format Protect won't take."""


def _metadata_numbers(payload):
    """{key: number} of an onMetaData payload's AMF0 numbers (framerate, width, ...)."""
    out = {}
    for key in (b"width", b"height", b"framerate", b"videodatarate", b"videocodecid",
                b"audiocodecid", b"audiosamplerate", b"audiodatarate"):
        at = payload.find(struct.pack(">H", len(key)) + key + b"\x00")
        if at >= 0:
            start = at + 2 + len(key) + 1
            out[key.decode()] = round(struct.unpack(">d", payload[start:start + 8])[0], 2)
    return out


def _describe(url):
    """A source for logs: host and path, never the login."""
    parts = urlsplit(url)
    return f"{parts.hostname}{parts.path}"


def _is_config_tag(tag_header, payload):
    """AVC sequence header or AAC AudioSpecificConfig: needed before any frame."""
    if tag_header[0] == _TAG_VIDEO:
        return len(payload) > 1 and payload[1] == 0
    if tag_header[0] == _TAG_AUDIO:
        return len(payload) > 1 and payload[0] >> 4 == 10 and payload[1] == 0
    return False


def _is_keyframe(tag_header, payload):
    return tag_header[0] == _TAG_VIDEO and len(payload) > 1 and payload[0] >> 4 == 1


class _DeliveryStats:
    """How evenly ffmpeg hands over video frames, logged once a minute: Protect's
    player stutters and lags when frames arrive in clumps or late against their
    own timestamps (ffmpeg stamps them on arrival: -use_wallclock_as_timestamps)."""

    INTERVAL_S = 60.0

    def __init__(self, name, clock=time.monotonic):
        self.name = name
        self.clock = clock
        self.started = clock()
        self.first = None
        self._reset(self.started)

    def _reset(self, now):
        self.since = now
        self.delays = []      # wall - stream time, per frame (s)
        self.gaps = []        # wall time between frames (s)
        self.last = None

    def frame(self, timestamp_ms):
        now = self.clock()
        if self.first is None:
            self.first = now
            log.info("pull %s: first frame %.2f s after ffmpeg started", self.name,
                     now - self.started)
        self.delays.append(now - timestamp_ms / 1000.0)
        if self.last is not None:
            self.gaps.append(now - self.last)
        self.last = now
        if now - self.since >= self.INTERVAL_S and len(self.delays) > 2:
            base = min(self.delays)
            late = sorted(d - base for d in self.delays)
            clumped = sum(g < 0.005 for g in self.gaps)
            log.info("pull %s over %.0f s: %d frames (%.1f/s), late vs own timestamp "
                     "p50 %.0f ms p95 %.0f ms max %.0f ms, longest gap %.0f ms, %d%% clumped",
                     self.name, now - self.since, len(self.delays),
                     len(self.delays) / (now - self.since), late[len(late) // 2] * 1000,
                     late[int(len(late) * 0.95)] * 1000, late[-1] * 1000,
                     max(self.gaps) * 1000, 100 * clumped // max(1, len(self.gaps)))
            self._reset(now)


class SharedPull:
    """One ffmpeg reading a camera stream, fanned out to every push of it.

    A camera allows few RTSP sessions (a Hikvision 2560x1440 answered 500 past
    three: video1, video3 and detection), so channels carrying the same stream
    share one session. A push joins at the next key frame: it gets ffmpeg's
    onMetaData and the AVC/AAC config tags first, then frames.
    """

    def __init__(self, source_url, camera_audio=True, ffmpeg_bin="ffmpeg"):
        self.source_url = source_url
        self.camera_audio = camera_audio
        self.ffmpeg_bin = ffmpeg_bin
        # The cameras this pull serves, set by PullPool.acquire before start(): the first
        # ffmpeg command is built before any push has subscribed.
        self.device_ids = set()
        self._lock = threading.Lock()
        self._sinks = {}       # FlvPush -> waiting for a key frame to (re)join
        self._header = None
        self._config = {}      # "meta" / "video" / "audio" -> (tag_header, body)
        self._stop = threading.Event()
        self._proc = None
        self._codec_error_logged = None
        self._copy_audio = True  # until ffmpeg says FLV can't hold the camera's audio
        self._audio_frames = 0   # per attempt: a camera/substream with no audio falls back
        self._last_video = None  # monotonic s of the last video frame of this pull
        self.protect_clock = None  # TRANSCODE video1 with shared decode: a ProtectClock
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name=f"pull-{_describe(source_url)}")

    def start(self):
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        proc = self._proc
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()

    def subscribe(self, sink):
        with self._lock:
            self._sinks[sink] = True

    def unsubscribe(self, sink):
        with self._lock:
            self._sinks.pop(sink, None)

    def resync(self, sink):
        """Send `sink` a fresh start at the next key frame."""
        with self._lock:
            if sink in self._sinks:
                self._sinks[sink] = True

    def _run(self):
        attempt = failed = 0
        while not self._stop.is_set():
            attempt += 1
            failed = 0 if self._pull_once(attempt) else failed + 1
            if self._stop.wait(retry_delay(1.0, failed)):
                break
        log.info("pull %s stopped", _describe(self.source_url))

    def _command(self):
        """(ffmpeg command, whether it keeps the camera's timestamps) for this pull's cameras.
        The streamName is set per push (set_stream_name), not here."""
        with self._lock:
            devices = set(self.device_ids) | {s.device_id for s in self._sinks}
        camera_ts = use_camera_timestamps(devices)
        return ffmpeg_push_cmd(self.ffmpeg_bin, self.source_url, "diyport", self.camera_audio,
                               self._copy_audio, wallclock=not camera_ts), camera_ts

    def _pull_once(self, attempt):
        proc = None
        stderr_tail = lambda: ""
        self._frames = 0
        self._audio_frames = 0
        self._last_video = None
        codec_error = None
        try:
            cmd, camera_ts = self._command()
            proc = self._proc = subprocess.Popen(cmd,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
            stderr_tail = start_stderr_drain(proc)
            with self._lock:
                names = ", ".join(sorted(f"{s.device_id}/{s.channel}" for s in self._sinks))
            log.info("pull %s started for %s (attempt %d, pid %d%s)",
                     _describe(self.source_url), names or "no push", attempt, proc.pid,
                     ", camera timestamps" if camera_ts else "")
            self._consume(proc.stdout.read)
            reason = "ffmpeg output ended"
        except _AudioNotCopyable as exc:
            self._copy_audio = False
            reason = f"camera audio not copyable ({exc}): re-encoding it to AAC from now on"
        except UnsupportedCodec as exc:
            codec_error = exc.codec
            reason = str(exc)
        except (OSError, ValueError, struct.error) as exc:
            reason = repr(exc)
        finally:
            if proc and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
            if proc:
                proc.stdout.close()
            self._end_sinks()
        frames = self._frames
        if not self._stop.is_set() and self.camera_audio:
            stderr = stderr_tail()[-500:]
            if frames > 0 and not self._audio_frames:
                self.camera_audio = False
                log.info("pull %s has no audio track: using silent AAC", _describe(self.source_url))
                return False
            if frames == 0 and "matches no streams" in stderr:
                self.camera_audio = False
                log.info("pull %s has no audio track: using silent AAC", _describe(self.source_url))
                return False
        if not self._stop.is_set():
            stderr = stderr_tail()[-300:]
            msg = ("pull %s ended after %d frames: %s%s", _describe(self.source_url), frames,
                   reason, f" ffmpeg: {stderr.strip()}" if stderr.strip() else "")
            if codec_error is None:
                self._codec_error_logged = None
                log.warning(*msg)
            elif self._codec_error_logged != codec_error:
                # Retried until the camera changes codec: log once at error, not once a second.
                self._codec_error_logged = codec_error
                log.error(*msg)
            else:
                log.debug(*msg)
        return frames > 0 and codec_error is None

    def _consume(self, read, label=None):
        """Fan the FLV from `read` out to the sinks until it ends. Counts frames in
        self._frames; raises on a codec or audio the push can't carry."""
        stats = _DeliveryStats(label or _describe(self.source_url))
        self._header = read_flv_header(read)
        self._config = {}
        if self.protect_clock:
            self.protect_clock.reset()  # a new stream: its timestamps start again
        for tag_header, body in read_tags(read):
            if self._stop.is_set():
                break
            payload = body[:-4]
            if len(body) < int.from_bytes(tag_header[1:4], "big") + 4:
                break
            check_video_codec(tag_header, body)
            if (self._copy_audio and tag_header[0] == _TAG_AUDIO and payload
                    and payload[0] >> 4 not in _COPYABLE_AUDIO):
                raise _AudioNotCopyable(f"FLV sound format {payload[0] >> 4}")
            if tag_header[0] == _TAG_SCRIPT and b"onMetaData" in payload[:16]:
                # flvmux re-sends onMetaData every few frames: log the first only.
                first = "meta" not in self._config
                self._config["meta"] = (tag_header, body)
                if first:
                    log.info("pull %s onMetaData: %s", label or _describe(self.source_url),
                             _metadata_numbers(payload))
            elif _is_config_tag(tag_header, payload):
                self._config["video" if tag_header[0] == _TAG_VIDEO else "audio"] = \
                    (tag_header, body)
            else:
                self._frames += 1
                if tag_header[0] == _TAG_AUDIO:
                    self._audio_frames += 1
                if tag_header[0] == _TAG_VIDEO:
                    self._last_video = time.monotonic()
                    stats.frame(_tag_timestamp(tag_header))
                self._deliver(tag_header, body, _is_keyframe(tag_header, payload))

    def _end_sinks(self):
        with self._lock:
            for sink in self._sinks:
                self._sinks[sink] = True
                sink.put(("end", "camera stream ended"))

    def _deliver(self, tag_header, body, keyframe):
        with self._lock:
            for sink, waiting in list(self._sinks.items()):
                if waiting:
                    if not keyframe or "video" not in self._config:
                        continue
                    config = [self._config[k] for k in ("meta", "video", "audio")
                              if k in self._config]
                    if not sink.put(("start", self._header, config,
                                     _tag_timestamp(tag_header))):
                        continue
                    self._sinks[sink] = False
                if not sink.put(("tag", tag_header, body)):
                    self._sinks[sink] = True  # fell behind: it rejoins at a key frame


# Shared decode: detection decodes the pushed video1 and uses Protect's wall time, so boxes
# line up without a separate camera pull. Instance-wide; 0 restores the camera pull.
SHARED_DECODE = envutil.env_bool("AIPORT_ONVIF_SHARED_DECODE", True)
DETECT_RECORD = struct.Struct(">BdI")  # kind, wall ms, length; then that many FLV bytes
DETECT_BYTES, DETECT_FRAME = 0, 1      # kinds: FLV bytes; a video frame (one decoded picture)
DETECT_QUEUE = 300                     # tags a client may fall behind before it is dropped


def detect_socket(source_url):
    """The unix socket serving a camera's video1 to detection, named from its url so avclient
    and the restream process find the same one without passing it. Each uid gets a private
    directory (0700): another local user cannot read the video or take the name."""
    namespace = os.environ.get("AIPORT_DETECT_NAMESPACE", "")
    digest = hashlib.sha1(f"{namespace}\0{source_url}".encode()).hexdigest()[:12]
    directory = os.path.join(tempfile.gettempdir(),
                             f"diyport-detect-{getattr(os, 'getuid', lambda: 0)()}")
    os.makedirs(directory, mode=0o700, exist_ok=True)
    try:
        os.chmod(directory, 0o700)
    except OSError:
        pass
    return os.path.join(directory, f"{digest}.sock")


# How long detection waits for a video1 frame before using its own clock. After one timeout
# the clock latches to its own reading, so a missing push cannot throttle detection.
PROTECT_CLOCK_WAIT_S = 3.0
PROTECT_CLOCK_STALL_S = 1.0
PROTECT_CLOCK_RETRY_S = 10.0


class ProtectClock:
    """The onClockSyncs the video1 push actually sent Protect, in the pull's stream time, and
    the last video frame it wrote. Live 2026-11-07 on Driveway (shared decode, delay 0): boxes
    a little ahead of the vehicle. DetectClient read the clock itself when a tag left the pull,
    while the push reads it when the tag leaves its queue (QUEUE_TAGS, up to ~10 s), so any
    push backlog put Protect's wall time later than ours and the box early by that much. Now a
    frame's wall time is Protect's own: its sync's wallClock + (ts - streamClock)."""

    def __init__(self):
        self._cond = threading.Condition()
        self.reset()

    def reset(self):
        with self._cond:
            self._syncs = collections.deque(maxlen=16)  # (stream ms, wall ms), oldest first
            self._written = None  # stream ms of the last video frame sent to Protect
            self._last_write = None  # monotonic s of that write
            # A new stream starts on our own clock until its first push write proves otherwise.
            self._stalled_until = time.monotonic() + PROTECT_CLOCK_RETRY_S
            self._cond.notify_all()

    def synced(self, stream_ms, wall_ms):
        with self._cond:
            self._syncs.append((stream_ms, wall_ms))
            self._cond.notify_all()

    def wrote(self, stream_ms):
        with self._cond:
            self._written = stream_ms
            self._last_write = time.monotonic()
            self._stalled_until = 0.0
            self._cond.notify_all()

    def wall(self, stream_ms, timeout):
        """Protect's wall ms for the frame at stream_ms, once the push has sent it, by the
        sync in force when it went (the latest at or before it); None after `timeout` s or
        immediately while a stalled/absent push has latched the fallback."""
        now = time.monotonic()
        with self._cond:
            if now < self._stalled_until:
                return None
            if self._written is not None and self._written < stream_ms \
                    and self._last_write is not None \
                    and now - self._last_write >= PROTECT_CLOCK_STALL_S:
                self._stalled_until = now + PROTECT_CLOCK_RETRY_S
                return None
            deadline = now + timeout
            while self._written is None or self._written < stream_ms:
                if time.monotonic() < self._stalled_until:  # reset by another thread
                    return None
                left = deadline - time.monotonic()
                if left <= 0:
                    self._stalled_until = time.monotonic() + PROTECT_CLOCK_RETRY_S
                    return None
                self._cond.wait(left)
            sync = next((s for s in reversed(self._syncs) if s[0] <= stream_ms), None)
            return None if sync is None else sync[1] + stream_ms - sync[0]


class DetectClient:
    """One detection reader of a channel: a SharedPull sink that sends each tag as a record,
    a video frame's with the wall time Protect places it at. With the channel's ProtectClock
    (TRANSCODE video1) that is the push's own onClockSync mapping, read once the push has sent
    the frame; without it, or when no push sends it within PROTECT_CLOCK_WAIT_S, the same
    g3_sync mapping (sync at every key frame, 10 s fallback) on a clock read here."""

    LAG_LOG_S = 60.0

    def __init__(self, conn, pull, on_close=None):
        self.conn, self.pull = conn, pull
        self._on_close = on_close
        clock = getattr(pull, "protect_clock", None)
        self.clock = clock if isinstance(clock, ProtectClock) else None
        self._queue = queue.Queue(DETECT_QUEUE)
        self._started = False
        self._base = self._sync = None  # base ts; (sync wall ms, sync stream ms)
        self.dead = False
        self._lags = []        # Protect's wall - ours, ms, since the last lag line
        self._fallbacks = 0
        self._lag_logged = time.monotonic()
        pull.subscribe(self)
        threading.Thread(target=self._run, daemon=True, name="detect-client").start()

    def close(self):
        """Stop this reader even when it is waiting for a frame (server shutdown)."""
        self.dead = True
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        try:
            self.conn.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.conn.close()

    def _record(self, kind, data, wall=0.0):
        self._queue.put_nowait(DETECT_RECORD.pack(kind, wall, len(data)) + data)

    def put(self, item):
        if self.dead:
            return True
        try:
            if item[0] == "start":
                if self._started:  # it fell behind and rejoined: its decoder can't follow
                    raise queue.Full
                self._started = True
                _, header, config, self._base = item
                self._record(DETECT_BYTES, header)
                for tag_header, body in config:
                    self._record(DETECT_BYTES, _retime(tag_header, 0) + body)
            elif item[0] == "tag":
                tag_header, body = item[1], item[2]
                stream_ms = _tag_timestamp(tag_header)
                ts = stream_ms - self._base
                data = _retime(tag_header, ts) + body
                payload = body[:-4]
                if tag_header[0] != _TAG_VIDEO or _is_config_tag(tag_header, payload):
                    self._record(DETECT_BYTES, data)
                    return True
                now = time.time() * 1000
                if (self._sync is None or _is_keyframe(tag_header, payload)
                        or now - self._sync[0] >= G3_SYNC_FALLBACK_S * 1000):
                    self._sync = (now, ts)
                own = self._sync[0] + ts - self._sync[1]
                if self.clock:  # resolved in _run, once the push has sent it
                    self._queue.put_nowait((stream_ms, own, data))
                else:
                    self._record(DETECT_FRAME, data, own)
            else:  # the stream ended: send what is queued, then close
                self.dead = True
                self._queue.put_nowait(None)
        except queue.Full:
            self.dead = True
            with self._queue.mutex:
                self._queue.queue.clear()
            self._queue.put_nowait(None)
        return True

    def _protect_wall(self, stream_ms, own):
        wall = self.clock.wall(stream_ms, PROTECT_CLOCK_WAIT_S)
        if wall is None:
            self._fallbacks += 1
            wall = own
        else:
            self._lags.append(wall - own)
        now = time.monotonic()
        if now - self._lag_logged >= self.LAG_LOG_S:
            self._lag_logged = now
            lags, self._lags = self._lags, []
            if lags:
                log.info("detect %s: video1 push lag avg %.0f ms, max %.0f ms over %d frames%s",
                         _describe(self.pull.source_url), sum(lags) / len(lags), max(lags),
                         len(lags), f", {self._fallbacks} on our own clock (no push)"
                         if self._fallbacks else "")
            elif self._fallbacks:
                log.warning("detect %s: %d frames on our own clock, no video1 push sent them",
                            _describe(self.pull.source_url), self._fallbacks)
            self._fallbacks = 0
        return wall

    def _run(self):
        try:
            while not self.dead or not self._queue.empty():
                data = self._queue.get()
                if data is None:
                    break
                if isinstance(data, tuple):
                    stream_ms, own, flv = data
                    data = DETECT_RECORD.pack(DETECT_FRAME, self._protect_wall(stream_ms, own),
                                              len(flv)) + flv
                self.conn.sendall(data)
        except OSError:
            pass
        finally:
            self.dead = True
            self.pull.unsubscribe(self)
            self.conn.close()
            if self._on_close:
                self._on_close(self)


class DetectServer:
    """Serves one channel of a TranscodePull to detection readers on a unix socket."""

    def __init__(self, path, pull):
        self.path, self.pull = path, pull
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.bind(path)
        self._sock.listen(2)
        self._sock.settimeout(1.0)  # so close() can wake _accept
        self._stop = threading.Event()
        self._clients = set()
        self._clients_lock = threading.Lock()
        threading.Thread(target=self._accept, daemon=True, name="detect-accept").start()

    def _accept(self):
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            conn.settimeout(None)  # accept() inherited the listener's 1 s timeout
            client = DetectClient(conn, self.pull, on_close=self._forget)
            with self._clients_lock:
                self._clients.add(client)

    def _forget(self, client):
        with self._clients_lock:
            self._clients.discard(client)

    def close(self):
        self._stop.set()
        with self._clients_lock:
            clients, self._clients = list(self._clients), set()
        for client in clients:
            client.close()
        self._sock.close()
        try:
            os.unlink(self.path)
        except OSError:
            pass


def read_detect_records(read):
    """(kind, wall ms, data) records from a DetectServer until the stream ends."""
    while True:
        head = _read_exact(read, DETECT_RECORD.size)
        if len(head) < DETECT_RECORD.size:
            return
        kind, wall, length = DETECT_RECORD.unpack(head)
        data = _read_exact(read, length)
        if len(data) < length:
            return
        yield kind, wall, data


class TranscodePull:
    """TRANSCODE: one gst-launch decoding a camera's main stream once and encoding one
    stream per channel, each read from its own pipe into a SharedPull (never started: it
    only fans that channel's tags out to its push). All channels are encoded whether
    Protect plays them or not, as on the AI Port."""

    def __init__(self, source_url, camera_audio, renditions):
        """renditions: ((channel, width, height, fps, kbps, gop), ...)."""
        self.source_url = source_url
        self.camera_audio = camera_audio
        self.renditions = renditions
        self.outputs = {}
        for channel, *_ in renditions:
            out = self.outputs[channel] = SharedPull(source_url, camera_audio)
            out.transcode = self
        self._stop = threading.Event()
        self._proc = None
        self._gst_audio = 0
        self._restart = threading.Event()
        self._restarted = -RECONNECT_MIN_S  # monotonic: this pipeline's last start or restart
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name=f"transcode-{_describe(source_url)}")
        self._detect = None

    def start(self):
        if SHARED_DECODE and "video1" in self.outputs:
            try:
                self.outputs["video1"].protect_clock = ProtectClock()
                self._detect = DetectServer(detect_socket(self.source_url),
                                            self.outputs["video1"])
            except OSError as exc:
                log.warning("detection socket for %s failed: %r", _describe(self.source_url),
                            exc)
        self._thread.start()
        return self

    def stop(self):
        if self._detect:
            self._detect.close()
        self._stop.set()
        for out in self.outputs.values():
            out._stop.set()
        proc = self._proc
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()

    def silent_channels(self, quiet_s, now=None):
        """Channels with no video frame for quiet_s, counted from this pipeline's last start.
        Every channel is encoded whether Protect plays it or not, so all of them should flow."""
        now = time.monotonic() if now is None else now
        return [channel for channel, out in self.outputs.items()
                if now - max(out._last_video or self._restarted, self._restarted) >= quiet_s]

    def restart(self, reason):
        """Replace a running pipeline with a silent channel, at most once per RECONNECT_MIN_S
        (and not within that of its start): every channel's re-send lands here, and each
        restart costs a camera session. Live 2026-11-03 17:24 on Driveway: gst-launch stayed
        alive with its RTSP socket CLOSED and no output for 2.5 h, while Protect re-sent video1
        and video3 every ~13 s and reconnected pushes kept joining the dead pipeline."""
        proc = self._proc
        now = time.monotonic()
        if (not proc or proc.poll() is not None
                or now - self._restarted < RECONNECT_MIN_S
                or not self.silent_channels(RESEND_RESTART_QUIET_S, now)):
            return False
        self._restarted = now
        log.warning("transcode %s restarting: %s (pid %d)", _describe(self.source_url),
                    reason, proc.pid)
        self._restart.set()
        return True

    def _run(self):
        attempt = failed = 0
        while not self._stop.is_set():
            attempt += 1
            failed = 0 if self._pull_once(attempt) else failed + 1
            if self._stop.wait(retry_delay(1.0, failed)):
                break
        log.info("transcode %s stopped", _describe(self.source_url))

    def _read_output(self, channel, fd, ended):
        out = self.outputs[channel]
        try:
            with os.fdopen(fd, "rb", buffering=0) as f:
                out._consume(f.read, f"{_describe(self.source_url)} {channel}")
            out.reason = "output ended"
        except Exception as exc:  # any end of one output restarts the whole pipeline
            out.reason = repr(exc)
        ended.set()

    def _pull_once(self, attempt):
        proc = None
        stderr_tail = lambda: ""
        pipes = {channel: os.pipe() for channel, *_ in self.renditions}
        readers = []
        ended = threading.Event()
        for out in self.outputs.values():
            out._frames = 0
            out._audio_frames = 0
            out._last_video = None
            out.reason = "not started"
        try:
            outputs = [(pipes[channel][1], *rest) for channel, *rest in self.renditions]
            cmd = transcode_cmd(GST_BIN, self.source_url, outputs, self.camera_audio,
                                GST_AUDIO[self._gst_audio])
            self._restart.clear()
            self._restarted = time.monotonic()
            proc = self._proc = subprocess.Popen(
                cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE, pass_fds=[w for _, w in pipes.values()])
            stderr_tail = start_stderr_drain(proc)
            log.info("transcode %s started: %s (attempt %d, pid %d)",
                     _describe(self.source_url),
                     ", ".join(f"{c} {w or '?'}x{h or '?'}@{f or '?'} {k}k gop {g}"
                               for c, w, h, f, k, g in self.renditions), attempt, proc.pid)
        except OSError as exc:
            log.warning("transcode %s failed to start: %r", _describe(self.source_url), exc)
            for r, w in pipes.values():
                os.close(r)
                os.close(w)
            for out in self.outputs.values():
                out._end_sinks()  # no reader was started to do it
            return False
        finally:
            for r, w in pipes.values():
                try:
                    os.close(w)  # the child holds the write ends
                except OSError:
                    pass
        for channel, (r, _) in pipes.items():
            t = threading.Thread(target=self._read_output, args=(channel, r, ended),
                                 daemon=True, name=f"transcode-{channel}")
            t.start()
            readers.append(t)
        while (not ended.wait(1.0) and not self._stop.is_set()
               and not self._restart.is_set()):
            if proc.poll() is not None:
                break
            silent = self.silent_channels(TRANSCODE_STALL_S) if TRANSCODE_STALL_S else None
            if silent:
                log.warning("transcode %s stalled: no video on %s for %.0f s, restarting (pid %d)",
                            _describe(self.source_url), ", ".join(silent), TRANSCODE_STALL_S,
                            proc.pid)
                break
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        for t in readers:
            t.join(timeout=5)
        for out in self.outputs.values():
            out._end_sinks()
        frames = sum(out._frames for out in self.outputs.values())
        audio_frames = sum(out._audio_frames for out in self.outputs.values())
        if self.camera_audio and frames > 0 and not audio_frames and not self._stop.is_set():
            self.camera_audio = False
            log.warning("transcode %s has no audio track: using silent AAC",
                        _describe(self.source_url))
            return False
        if self.camera_audio and frames == 0 and not self._stop.is_set():
            self._gst_audio = (self._gst_audio + 1) % len(GST_AUDIO)
        if not self._stop.is_set():
            stderr = stderr_tail()[-300:].strip()
            log.warning("transcode %s ended: %s%s", _describe(self.source_url),
                        ", ".join(f"{c} {o._frames} frames ({o.reason})"
                                  for c, o in self.outputs.items()),
                        f" gst: {stderr}" if stderr else "")
        return frames > 0


class PullPool:
    """The SharedPull per (source url, audio), alive while a push uses it; with
    renditions, the TranscodePull per (source url, audio, renditions)."""

    def __init__(self, ffmpeg_bin="ffmpeg"):
        self.ffmpeg_bin = ffmpeg_bin
        self._lock = threading.Lock()
        self._pulls = {}  # key -> [SharedPull or TranscodePull, users]

    def acquire(self, url, camera_audio, channel=None, renditions=None, device_id=None):
        with self._lock:
            key = (url, camera_audio) if renditions is None else (url, camera_audio, renditions)
            entry = self._pulls.get(key)
            if entry is None:
                pull = (SharedPull(url, camera_audio, self.ffmpeg_bin) if renditions is None
                        else TranscodePull(url, camera_audio, renditions))
                if device_id and renditions is None:
                    pull.device_ids.add(device_id)  # before start(): its first command needs it
                entry = self._pulls[key] = [pull.start(), 0]
            elif device_id and renditions is None:
                entry[0].device_ids.add(device_id)
            entry[1] += 1
            return entry[0] if renditions is None else entry[0].outputs[channel]

    def release(self, pull):
        with self._lock:
            owner = getattr(pull, "transcode", None)
            owner = owner if isinstance(owner, TranscodePull) else pull
            key = (owner.source_url, owner.camera_audio)
            if isinstance(owner, TranscodePull):
                key += (owner.renditions,)
            entry = self._pulls.get(key)
            if entry and entry[0] is owner:
                entry[1] -= 1
                if entry[1] <= 0:
                    del self._pulls[key]
                    owner.stop()


class FlvPush:
    """Keeps one channel pushed: its SharedPull's tags -> extendedFlv -> TCP,
    reconnected on any failure every retry interval until stop()."""

    QUEUE_TAGS = 600       # ~10 s of 20 fps video plus audio
    STALL_S = 30.0         # no tag for this long ends the connection

    def __init__(self, device_id, channel, source_url, stream_name, destination,
                 ffmpeg_bin="ffmpeg", camera_audio=True, pool=None, renditions=None):
        self.device_id = device_id
        self.renditions = renditions  # TRANSCODE: this camera's TranscodePull outputs
        self.camera_audio = camera_audio
        self.channel = channel
        self.source_url = source_url
        self.stream_name = stream_name
        self.destination = destination
        self.host, self.port, self.retry_s, self.timeout_s = parse_destination(destination)
        self._pool = pool or PullPool(ffmpeg_bin)
        self._pull = None
        self._queue = queue.Queue(maxsize=self.QUEUE_TAGS)
        self._stop = threading.Event()
        self._reconnect = threading.Event()
        self._sock = None
        self.last_sent = time.monotonic()  # last video frame written to Protect
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name=f"push-{device_id}-{channel}")

    def matches(self, source_url, stream_name, destination, renditions=None, camera_audio=None):
        same = (self.source_url, self.stream_name, self.destination, self.renditions) == \
               (source_url, stream_name, destination, renditions)
        return same and (camera_audio is None or bool(camera_audio) == bool(self.camera_audio))

    def start(self):
        self._pull = self._pool.acquire(self.source_url, self.camera_audio, self.channel,
                                        self.renditions, device_id=self.device_id)
        self._pull.subscribe(self)
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        if self._pull:
            self._pull.unsubscribe(self)
            self._pool.release(self._pull)
        sock = self._sock
        if sock:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def _new_writer(self, write):
        g3 = self.renditions is not None
        return ExtendedFlvWriter(write, early_sync=not g3, g3_sync=g3)

    def restart_source(self, reason):
        """TRANSCODE: replace the pipeline behind this push. Other pulls are left alone."""
        owner = getattr(self._pull, "transcode", None)
        if isinstance(owner, TranscodePull):
            owner.restart(reason)

    def reconnect(self, reason):
        """Open a fresh Protect connection without releasing the shared pull."""
        log.info("push %s/%s reconnecting: %s", self.device_id, self.channel, reason)
        self._reconnect.set()
        self.put(("end", reason))
        sock = self._sock
        if sock:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def put(self, item):
        """From the pull: False (and the queue emptied) when this push fell behind."""
        try:
            self._queue.put_nowait(item)
            return True
        except queue.Full:
            with self._queue.mutex:
                self._queue.queue.clear()
                self._queue.queue.append(("end", "fell behind the camera stream"))
                self._queue.not_empty.notify()
            return False

    def _get(self, timeout):
        """Next item, or None on stop or after `timeout` seconds without one."""
        deadline = time.monotonic() + timeout
        while not self._stop.is_set():
            try:
                return self._queue.get(timeout=min(1.0, max(0.0, deadline - time.monotonic())))
            except queue.Empty:
                if time.monotonic() >= deadline:
                    return None
        return None

    def _run(self):
        attempt = failed = 0
        while not self._stop.is_set():
            if self._reconnect.is_set():
                self._reconnect.clear()
                with self._queue.mutex:
                    self._queue.queue.clear()
                self._pull.resync(self)
                continue
            item = self._get(1.0)
            if not item or item[0] != "start":
                continue  # waiting for the pull to hand over a key frame
            attempt += 1
            failed = 0 if self._push_once(item, attempt) else failed + 1
            if self._reconnect.is_set():
                self._reconnect.clear()
                with self._queue.mutex:
                    self._queue.queue.clear()
                self._pull.resync(self)
                continue
            if self._stop.wait(retry_delay(self.retry_s, failed)):
                break
            with self._queue.mutex:
                self._queue.queue.clear()
            self._pull.resync(self)
        log.info("push %s/%s stopped", self.device_id, self.channel)

    def _push_once(self, start, attempt):
        _, header, config, base_ts = start
        sent = 0
        reason = "stopped"
        try:
            sock = self._sock = socket.create_connection((self.host, self.port),
                                                         timeout=self.timeout_s)
            sock.settimeout(30)
            drain_socket(sock, f"{self.device_id}/{self.channel}")
            log.info("push %s/%s -> %s:%d streamName=%s started (attempt %d, %s)",
                     self.device_id, self.channel, self.host, self.port, self.stream_name,
                     attempt, _describe(self.source_url))

            def write(data):
                nonlocal sent
                sock.sendall(data)
                sent += len(data)

            writer = self._new_writer(write)
            clock = getattr(self._pull, "protect_clock", None)  # shared decode, video1
            clock = clock if isinstance(clock, ProtectClock) else None
            if clock:
                writer.on_sync = lambda ts, wall: clock.synced(ts + base_ts, wall)
            writer.header(header)
            for tag_header, body in config:
                tag_header, body = set_stream_name(tag_header, body, self.stream_name)
                writer.sync_if_due(0)  # sets the trailer time; never due here with g3_sync
                writer.tag(_retime(tag_header, 0), body)
                if writer.g3_sync and tag_header[0] == _TAG_SCRIPT:
                    writer.initial_sync()
            while not self._stop.is_set():
                item = self._get(self.STALL_S)
                if item is None:
                    if not self._stop.is_set():
                        reason = f"no frames for {self.STALL_S:.0f} s"
                    break
                if item[0] != "tag":
                    reason = item[1] if item[0] == "end" else "restarted"
                    break
                tag_header = _retime(item[1], _tag_timestamp(item[1]) - base_ts)
                writer.sync_if_due(_tag_timestamp(tag_header),
                                   _is_keyframe(tag_header, item[2][:-4]))
                writer.tag(tag_header, item[2])
                if tag_header[0] == _TAG_VIDEO:
                    self.last_sent = time.monotonic()
                    if clock:
                        clock.wrote(_tag_timestamp(item[1]))
        except (OSError, ValueError, struct.error) as exc:
            reason = repr(exc)
        finally:
            if self._sock:
                # The drain thread holds a reference to this socket: close() alone would
                # not close the fd or send FIN, leaving Protect a second live push.
                try:
                    self._sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                self._sock.close()
                self._sock = None
        if not self._stop.is_set() and not self._reconnect.is_set():
            log.warning("push %s/%s ended after %d bytes: %s", self.device_id, self.channel,
                        sent, reason)
        return sent > 0


PUSH_RETRY_MAX_S = 30.0


def retry_delay(retry_s, failed):
    """Seconds before the next push: Protect's retryInterval, doubling after the third push in
    a row that sent nothing, up to PUSH_RETRY_MAX_S. An unreachable source otherwise started an
    ffmpeg every second (live 2026-10-28)."""
    base = max(retry_s, 1.0)
    return min(PUSH_RETRY_MAX_S, base * 2 ** max(0, failed - 3)) if failed else base


# Protect re-sends a channel's settings when its media server lost the stream; reconnect
# that push, at most once per RECONNECT_MIN_S, instead of ignoring the repeat.
RECONNECT_MIN_S = 10

# Retry a failed ONVIF profile lookup quickly while the SUB channels wait; otherwise a
# transient error leaves them on the main stream until restart.
PROFILE_QUICK_RETRY_S = (2.0, 3.0)
# ...then, with the channels on the main stream, these (the last repeats). Success moves them
# to the substream at once, replacing the pushes.
PROFILE_RETRY_S = (30.0, 120.0, 600.0)
# ChangeVideoSettings waits this long at most for a lookup, to report the right sizes.
PROFILE_WAIT_S = 10.0


class PushManager:
    """Per-device ONVIF source URL plus the channel pushes Protect asked for."""

    def __init__(self, ffmpeg_bin="ffmpeg", fetch_profiles=None, background=True,
                 push_factory=None):
        self.ffmpeg_bin = ffmpeg_bin
        self._push_factory = push_factory  # default FlvPush; see onvif_restream.py
        warn_old_settings()
        self._fetch_profiles = fetch_profiles or onvif_media.fetch_profiles
        self._background = background  # False runs the profile lookup inline (tests)
        self._lock = threading.Lock()
        self._lookup_done = threading.Condition(self._lock)
        self._sources = {}   # device_id -> {"url", "audio", "width", "height", "fps"}
        self._wanted = {}    # device_id -> {channel: (stream_name, destination)}
        self._pushes = {}    # (device_id, channel) -> FlvPush
        self._last_sent = {}  # (device_id, channel) -> last_sent of a push a re-send replaced
        self._reconnected = {}  # (device_id, channel) -> monotonic time of the last reconnect
        self._pool = PullPool(ffmpeg_bin)  # channels on the same stream share one pull
        self._subs = {}      # main url -> substream profile dict, or None (none/failed)
        self._looking_up = set()  # main urls whose profiles are being fetched

    def set_source(self, device_id, url, audio=True, width=None, height=None, fps=None,
                   codec=None):
        note_codec(device_id, codec)
        with self._lock:
            self._sources[device_id] = {"url": url, "audio": bool(audio), "width": width,
                                        "height": height, "fps": fps, "codec": codec}
            lookup = url not in self._subs and url not in self._looking_up
            if lookup:
                self._looking_up.add(url)
            self._sync(device_id)
        if lookup:
            if self._background:
                threading.Thread(target=self._find_substream, args=(url,), daemon=True,
                                 name=f"onvif-profiles-{device_id}").start()
            else:
                self._find_substream(url)

    def _fetch_substream(self, url, late):
        """The camera's substream or None; raises if its profiles can't be had."""
        for delay in (PROFILE_QUICK_RETRY_S if self._background and not late else ()):
            try:
                return onvif_media.pick_substream(self._fetch_profiles(url))
            except Exception as e:
                log.info("ONVIF profiles of %s: %s, trying again in %.0f s",
                         urlsplit(url).hostname, e, delay)
                time.sleep(delay)
        return onvif_media.pick_substream(self._fetch_profiles(url))

    def _find_substream(self, url, late=0):
        """Look up the camera's profiles for a main url, then start the waiting SUB_CHANNELS
        pushes (on the substream, or the main stream if there is none). `late`: how many
        lookups have already failed, with the pushes running on the main stream."""
        host = urlsplit(url).hostname
        retry_s = None
        try:
            sub = self._fetch_substream(url, late)
        except Exception as e:
            retry_s = PROFILE_RETRY_S[min(late, len(PROFILE_RETRY_S) - 1)]
            (log.info if late else log.warning)(
                "ONVIF profiles of %s unavailable (%s): %s carry the main stream, "
                "trying again in %.0f s", host, e, "/".join(SUB_CHANNELS), retry_s)
            sub = None
        else:
            if sub:
                log.info("ONVIF camera %s: %s carry substream %s (%sx%s @%s fps)", host,
                         "/".join(SUB_CHANNELS), sub["token"], sub["width"], sub["height"], sub["fps"])
            else:
                log.info("ONVIF camera %s has no smaller H.264 profile: %s carry the main "
                         "stream", host, "/".join(SUB_CHANNELS))
        with self._lock:
            self._subs[url] = sub
            self._looking_up.discard(url)
            self._lookup_done.notify_all()
            used = [d for d, src in self._sources.items() if src["url"] == url]
            for device_id in used:
                self._sync(device_id)
            if retry_s is not None and not used:
                # Nothing uses this camera now: its next set_source looks it up afresh.
                del self._subs[url]
                return
        if retry_s is not None and self._background:
            timer = threading.Timer(retry_s, self._find_substream, args=(url, late + 1))
            timer.daemon = True
            timer.start()

    def wait_for_substream(self, device_id, timeout=PROFILE_WAIT_S):
        """Give a running profile lookup for this camera time to finish."""
        with self._lock:
            src = self._sources.get(device_id)
            if src:
                self._lookup_done.wait_for(lambda: src["url"] not in self._looking_up, timeout)

    def substream(self, device_id):
        """{width, height, fps} of the substream SUB_CHANNELS carry, or None if they
        carry the main stream."""
        with self._lock:
            src = self._sources.get(device_id)
            sub = self._subs.get(src["url"]) if src else None
            return {k: sub[k] for k in ("width", "height", "fps")} if sub else None

    def substream_pending(self, device_id):
        """True while this camera's profile lookup is still running."""
        with self._lock:
            src = self._sources.get(device_id)
            return bool(src and src["url"] in self._looking_up)

    def transcode_channels(self, device_id):
        """{channel: {width, height, fps, bitrate}} of what each channel is encoded at under
        TRANSCODE, leaving out what isn't known; None when this camera isn't transcoded. So
        Protect is told what its encoder really makes. Live 2026-11-04 09:20 on Driveway:
        video2 was encoded at 2000 kbps and reported at 1000000."""
        if not transcodes(device_id):
            return None
        with self._lock:
            src = self._sources.get(device_id)
            renditions = self._renditions(device_id, src) if src else None
        if not renditions:
            return None
        return {channel: {k: v for k, v in (("width", w), ("height", h), ("fps", fps),
                                            ("bitrate", kbps * 1000)) if v}
                for channel, w, h, fps, kbps, _ in renditions}

    def stalled_channels(self, stale_s, now=None):
        """{device_id: [channels]} of the running pushes that have written no video to Protect
        for stale_s."""
        now = time.monotonic() if now is None else now
        stalled = {}
        with self._lock:
            for (device_id, channel), push in self._pushes.items():
                if now - push.last_sent >= stale_s:
                    stalled.setdefault(device_id, []).append(channel)
        return {device_id: sorted(channels) for device_id, channels in stalled.items()}

    def _channel_url(self, device_id, src, channel):
        """The url `channel` pushes; None while SUB_CHANNELS wait for the profiles."""
        url = src["url"]
        if transcodes(device_id) and channel in TRANSCODE_CHANNELS:
            # One pull of the main stream, started once the substream size is known.
            return None if url in self._looking_up else url
        if channel not in SUB_CHANNELS:
            return url
        if url in self._looking_up:
            return None
        sub = self._subs.get(url)
        return sub["url"] if sub else url

    def _renditions(self, device_id, src):
        """TRANSCODE outputs for a camera: video1 at its main size with a 1 s GOP, video2 at
        the substream's and video3 at 360 lines (what Protect is told) with GOP 24, as the
        AI Port's S0/S1/S2."""
        if not transcodes(device_id):
            return None
        sub = self._subs.get(src["url"])
        main = (src["width"], src["height"], src["fps"])
        low = (sub["width"], sub["height"], sub["fps"] or src["fps"]) if sub else main
        size3 = transcode_video3_size(src["width"], src["height"])
        video3 = (*size3, low[2]) if size3 else low
        return tuple((ch, *{"video2": low, "video3": video3}.get(ch, main), TRANSCODE_KBPS[ch],
                      24 if ch in SUB_CHANNELS else (src["fps"] or 20))
                     for ch in TRANSCODE_CHANNELS)

    def source(self, device_id):
        with self._lock:
            src = self._sources.get(device_id)
            return dict(src) if src else None

    def stop_device(self, device_id):
        """Streaming stopped: stop the pushes but remember the destinations.

        OnvifStreamControl streaming:false is a stop, not an unassign. A real AI
        Port keeps its channel config, so a later streaming:true (e.g. after a
        console restart, which does not necessarily re-send ChangeVideoSettings)
        resumes the same streamNames instead of leaving the camera with no video.
        """
        with self._lock:
            self._sources.pop(device_id, None)
            self._sync(device_id)
            self._forget_last_sent(device_id)

    def _forget_last_sent(self, device_id):
        for key in [k for k in self._last_sent if k[0] == device_id]:
            del self._last_sent[key]

    def has_source(self, device_id):
        with self._lock:
            return device_id in self._sources

    def configure(self, device_id, video_settings):
        """Apply a ChangeVideoSettings `video` dict: channels with an extendedFlv
        avSerializer and a tcp destination are pushed; /dev/null stops one.
        A channel without an avSerializer is left as it is: Protect also sends
        encoder-only settings (fps, bitrate mode), which stopped the push."""
        with self._lock:
            wanted = self._wanted.setdefault(device_id, {})
            again = set()
            for channel, settings in (video_settings or {}).items():
                serializer = (settings or {}).get("avSerializer")
                if serializer is None:
                    continue
                serializer = serializer or {}
                dests = serializer.get("destinations") or []
                name = (serializer.get("parameters") or {}).get("streamName")
                if not dests or any("/dev/null" in d for d in dests):
                    wanted.pop(channel, None)
                elif name and dests[0].startswith("tcp://"):
                    if wanted.get(channel) == (name, dests[0]):
                        again.add(channel)
                    wanted[channel] = (name, dests[0])
            now = time.monotonic()
            for channel in again:
                key = (device_id, channel)
                if key in self._pushes and now - self._reconnected.get(key, -RECONNECT_MIN_S) \
                        >= RECONNECT_MIN_S:
                    self._reconnected[key] = now
                    push = self._pushes[key]
                    push.restart_source("Protect re-sent its settings")
                    push.reconnect("Protect re-sent its settings")
                    # Keep its last write, or a channel Protect keeps re-sending would
                    # look healthy however long it has delivered nothing.
                    self._last_sent[key] = push.last_sent
            self._sync(device_id)

    def stop_all(self):
        with self._lock:
            for push in self._pushes.values():
                push.stop()
            self._pushes.clear()
            self._sources.clear()
            self._wanted.clear()
            self._last_sent.clear()

    def _sync(self, device_id):
        src = self._sources.get(device_id)
        wanted = self._wanted.get(device_id, {}) if src else {}
        urls = {channel: self._channel_url(device_id, src, channel) for channel in wanted}
        renditions = self._renditions(device_id, src) if src else None
        rend = lambda channel: renditions if channel in TRANSCODE_CHANNELS else None
        for key in [k for k in self._pushes if k[0] == device_id]:
            want, url = wanted.get(key[1]), urls.get(key[1])
            if not want or not url or not self._pushes[key].matches(
                    url, *want, rend(key[1]), src.get("audio", True)):
                self._pushes.pop(key).stop()
        for channel, (name, dest) in wanted.items():
            url = urls[channel]
            if url and (device_id, channel) not in self._pushes:
                try:
                    push = self._pushes[(device_id, channel)] = (self._push_factory or FlvPush)(
                        device_id, channel, url, name, dest, self.ffmpeg_bin,
                        camera_audio=src["audio"], pool=self._pool,
                        renditions=rend(channel)).start()
                    if (device_id, channel) in self._last_sent:
                        push.last_sent = self._last_sent.pop((device_id, channel))
                except ValueError:
                    log.warning("push %s/%s: bad destination %r", device_id, channel, dest)
