// SPDX-License-Identifier: AGPL-3.0-or-later

// Package media is the contract between the restreamer's packages: rtsp produces these,
// transcode and flv consume (and transcode re-produces) them. Keep it small and stable.
package media

import "time"

type Codec int

const (
	None Codec = iota
	H264
	H265
	AAC
	PCMU
	PCMA
	Opus
)

func (c Codec) String() string {
	return [...]string{"none", "h264", "h265", "aac", "pcmu", "pcma", "opus"}[c]
}

// Tracks describes a stream. Sent before any packet and again whenever parameters change
// (new SPS/PPS, reconnect to a camera that changed resolution).
type Tracks struct {
	Video         Codec
	VPS, SPS, PPS []byte // raw NAL units, no start code
	Width, Height int
	FPS           float64 // 0 when unknown

	Audio         Codec // None when the stream has no audio
	AudioRate     int
	AudioChannels int
	AudioConfig   []byte // AAC AudioSpecificConfig, AAC only
}

// VideoAU is one access unit. NALUs carry no start codes. PTS is on the producer's
// monotonic clock, shared with the same producer's AudioFrame PTS.
type VideoAU struct {
	PTS   time.Duration
	NALUs [][]byte
	IDR   bool
}

// AudioFrame is one encoded audio frame (raw AAC AU without ADTS, or G.711/Opus payload).
type AudioFrame struct {
	PTS  time.Duration
	Data []byte
}

// Subscriber receives a stream. Calls for one subscriber come from one goroutine, in order.
// Implementations must not block for long: copy data they keep, drop rather than stall.
type Subscriber interface {
	OnTracks(Tracks)
	OnVideo(VideoAU)
	OnAudio(AudioFrame)
	// OnDisconnect is called when the source drops; OnTracks follows on reconnect.
	OnDisconnect(err error)
}
