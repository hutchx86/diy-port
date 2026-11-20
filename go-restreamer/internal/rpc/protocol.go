// SPDX-License-Identifier: AGPL-3.0-or-later

// Package rpc is the restreamer's control channel: newline-delimited JSON-RPC 2.0 on a unix
// socket. The payloads are what diyport/onvif_push.py's RestreamerClient and GoPush send;
// change them together (SPEC.md and this file are the contract).
package rpc

import (
	"encoding/json"
	"fmt"
)

// Method names.
const (
	MethodConfigure = "configure_camera"
	MethodRemove    = "remove_camera"
	MethodStats     = "get_stats"
)

// JSON-RPC 2.0 error codes, plus ours.
const (
	CodeParse          = -32700
	CodeInvalidRequest = -32600
	CodeMethodNotFound = -32601
	CodeInvalidParams  = -32602
	CodeServer         = -32000 // the handler refused; Error.Data is a symbolic reason
)

// ConfigureParams is configure_camera: the whole desired state of one camera. Re-sending an
// identical one is a no-op; a changed one restarts only the channels that differ.
type ConfigureParams struct {
	DeviceID     string        `json:"device_id"`
	Mode         string        `json:"mode"` // "copy" (default), "transcode", or "aiport" (AI Port exact: one pull, three encodes, ubnt_streamer's FLV)
	SharedDecode *SharedDecode `json:"shared_decode,omitempty"`
	Channels     []Channel     `json:"channels"`
}

// SharedDecode asks for video1 to be served to detection on Socket (the DetectServer protocol).
type SharedDecode struct {
	Enabled   bool   `json:"enabled"`
	DetectFPS int    `json:"detect_fps"`
	Socket    string `json:"socket"`
}

// Channel is one push to Protect. Width/Height/BitrateKbps (0 = the source's) shape the
// transcode profile only.
type Channel struct {
	Channel      string `json:"channel"`
	StreamName   string `json:"stream_name"`
	RTSPURL      string `json:"rtsp_url"`
	DestHost     string `json:"dest_host"`
	DestPort     int    `json:"dest_port"`
	AudioEnabled bool   `json:"audio_enabled"`
	Width        int    `json:"width"`
	Height       int    `json:"height"`
	BitrateKbps  int    `json:"bitrate_kbps"`
	// Opus / OpusSampleRate come from Protect's ChangeVideoSettings avSerializer.parameters
	// (withOpus, opusSampleRate; live 2026-11-12: true / 24000). Opus false: no type-10 track on
	// this channel. OpusSampleRate 0 (not given) means 48000; libopus takes 8/12/16/24/48 kHz,
	// anything else is sent at 48000 with a warning.
	Opus           bool `json:"opus"`
	OpusSampleRate int  `json:"opus_sample_rate"`
	// Request counts Protect's ChangeVideoSettings for this channel (Python bumps it each
	// time). A change un-parks a push Protect had rejected; nothing else restarts.
	Request int64 `json:"request"`
	// aiport mode only. FPS is the console's fps for the channel (0: the camera's, or the
	// channel's fixed 24 for video2/3); RetryIntervalS / ConnectTimeoutS are the destination
	// URI's retryInterval / connectTimeout in seconds (0: 1 s each, the streamer's defaults).
	FPS             float64 `json:"fps,omitempty"`
	RetryIntervalS  float64 `json:"retry_interval_s,omitempty"`
	ConnectTimeoutS float64 `json:"connect_timeout_s,omitempty"`
}

// Validate rejects what no camera could run.
func (p *ConfigureParams) Validate() error {
	if p.DeviceID == "" {
		return fmt.Errorf("device_id is required")
	}
	switch p.Mode {
	case "", "copy", "transcode", "aiport":
	default:
		return fmt.Errorf("mode %q: want copy, transcode or aiport", p.Mode)
	}
	seen := map[string]bool{}
	for i, c := range p.Channels {
		switch {
		case c.Channel == "":
			return fmt.Errorf("channels[%d]: channel is required", i)
		case seen[c.Channel]:
			return fmt.Errorf("channel %s given twice", c.Channel)
		case c.RTSPURL == "":
			return fmt.Errorf("channel %s: rtsp_url is required", c.Channel)
		case c.DestHost == "" || c.DestPort <= 0 || c.DestPort > 65535:
			return fmt.Errorf("channel %s: bad destination %q:%d", c.Channel, c.DestHost, c.DestPort)
		}
		seen[c.Channel] = true
	}
	return nil
}

// RemoveParams is remove_camera.
type RemoveParams struct {
	DeviceID string `json:"device_id"`
}

// Request is one inbound line. ID is absent for a notification (no reply is sent).
type Request struct {
	JSONRPC string          `json:"jsonrpc"`
	ID      json.RawMessage `json:"id,omitempty"`
	Method  string          `json:"method"`
	Params  json.RawMessage `json:"params,omitempty"`
}

// Response is one outbound line.
type Response struct {
	JSONRPC string          `json:"jsonrpc"`
	ID      json.RawMessage `json:"id"`
	Result  any             `json:"result,omitempty"`
	Error   *Error          `json:"error,omitempty"`
}

// Error is a JSON-RPC error object. Data carries a symbolic reason such as
// ERR_UNSUPPORTED_CODEC_HEVC.
type Error struct {
	Code    int    `json:"code"`
	Message string `json:"message"`
	Data    any    `json:"data,omitempty"`
}

func (e *Error) Error() string { return fmt.Sprintf("rpc error %d: %s", e.Code, e.Message) }

// Stats is the get_stats result. Everything that could carry a credential is redacted.
type Stats struct {
	Cameras   map[string]CameraStats `json:"cameras"`
	RTSP      []RTSPStats            `json:"rtsp"`
	Hwaccel   HwStats                `json:"hwaccel"`
	UptimeMs  int64                  `json:"uptime_ms"`
	Transcode []TCStats              `json:"transcode,omitempty"`
}

// CameraStats is one camera: its channels in configure order.
type CameraStats struct {
	Mode     string         `json:"mode"`
	Channels []ChannelStats `json:"channels"`
}

// ChannelStats is one push. LastFrameAgeMs is how long ago a video frame last went to Protect,
// null before the first on this channel; Python's GoPush turns it into last_sent for stall
// detection (last_sent = monotonic() - age/1000). UptimeMs is the channel's age so a
// channel that has never sent can be judged against a grace period.
type ChannelStats struct {
	Channel        string `json:"channel"`
	StreamName     string `json:"stream_name"`
	URL            string `json:"rtsp_url"` // redacted
	BytesSent      uint64 `json:"bytes_sent"`
	LastFrameAgeMs *int64 `json:"last_frame_age_ms"`
	LastSentUnixMs *int64 `json:"last_sent_unix_ms"`
	Reconnects     uint64 `json:"reconnects"`
	Connected      bool   `json:"connected"`
	UptimeMs       int64  `json:"uptime_ms"`
	Error          string `json:"error,omitempty"` // e.g. ERR_UNSUPPORTED_CODEC_HEVC
}

// RTSPStats is one pulled URL (redacted).
type RTSPStats struct {
	URL            string `json:"url"`
	Subscribers    int    `json:"subscribers"`
	Sessions       int    `json:"sessions"`
	Reconnects     int    `json:"reconnects"`
	Connected      bool   `json:"connected"`
	LastFrameAgeMs *int64 `json:"last_frame_age_ms"`
}

// HwStats is what the hardware probe found.
type HwStats struct {
	Requested string `json:"requested"`
	Backend   string `json:"backend"` // "" when none
	Encoder   string `json:"encoder,omitempty"`
	FFmpeg    string `json:"ffmpeg,omitempty"`
	Reason    string `json:"reason,omitempty"`
}

// TCStats is one transcoder (one per camera stream that needs one).
type TCStats struct {
	DeviceID string           `json:"device_id"`
	URL      string           `json:"rtsp_url"` // redacted
	Mode     string           `json:"mode"`
	Restarts int              `json:"restarts"`
	Frames   map[string]int64 `json:"frames"`
}
