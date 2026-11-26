// SPDX-License-Identifier: AGPL-3.0-or-later

// Package audio decides what audio track Protect's extendedFlv ingest gets. Protect only
// marks a stream ready with both tracks present (onvif_push.py's header always flags audio
// and video), so every push has audio: the camera's own, or silence.
//
// What the Python (the ground truth, and where this differs from SPEC.md) does:
//   - AAC is copied as is.
//   - G.711 is copied as is (FLV sound format 7/8, 8 kHz mono). "Re-encoding G.711 to AAC
//     made Protect's live view stop and start every second (live 2026-10-28)". SPEC.md says
//     G.711 -> AAC, so that is here as Config.TranscodeG711 (off by default).
//   - Opus has no FLV id Protect takes, so ffmpeg re-encodes it to AAC 32 kHz mono 32k.
//   - No audio track (or audio disabled): silent AAC, 32 kHz mono, as onvif_push.py's
//     `anullsrc=r=32000:cl=mono` (SPEC.md says 16 kHz; the Python wins).
//
// Opus (Config.OpusOut): Protect's browser live view plays only an Opus track (opus.go), so
// besides the AAC above, the same camera audio is also encoded to 48 kHz mono Opus by ffmpeg
// (a second output of the AAC transcode, or an Opus-only ffmpeg when the AAC is passed
// through), and silent Opus packets are paced on video when the AAC is silence.
package audio

import (
	"log/slog"
	"sync"
	"time"

	"diyport/go-restreamer/internal/media"
)

// Output AAC parameters for everything this package encodes or fakes.
const (
	AACRate     = 32000
	aacFrameLen = 1024 // samples per AAC frame
)

// AACConfig is the AudioSpecificConfig of the encoded and silent tracks: AAC-LC (2),
// sampling index 5 (32 kHz), mono.
var AACConfig = []byte{0x12, 0x88}

// Format is the audio track that goes into the FLV: its codec decides the tag layout.
type Format struct {
	Codec    media.Codec // AAC, PCMU or PCMA
	Rate     int
	Channels int
	Config   []byte // AAC AudioSpecificConfig
	Silent   bool   // generated here, nothing from the camera
}

// Equal reports whether a connection's sequence headers stay valid across the two formats.
func (f Format) Equal(o Format) bool {
	return f.Codec == o.Codec && f.Rate == o.Rate && f.Channels == o.Channels &&
		f.Silent == o.Silent && string(f.Config) == string(o.Config)
}

// Config selects the behaviours above.
type Config struct {
	Enabled       bool   // false: always silence
	TranscodeG711 bool   // G.711 -> AAC instead of copying it
	FFmpegPath    string // "" looks up ffmpeg on PATH
	// OpusOut, when set, receives 20 ms Opus packets (media.AudioFrame, PTS on the camera's
	// clock) alongside the AAC/G.711 frames: called from any goroutine, like out.
	OpusOut func(media.AudioFrame)
	// OpusRate is the sample rate the Opus output is encoded at (0: 48000); a shared Encoder
	// keeps one ffmpeg per rate.
	OpusRate int
	// Encoder, when set, replaces the Normalizer's own ffmpeg with a shared one (encoder.go);
	// its owner feeds it the camera frames, so OnAudio only passes copied audio through.
	Encoder *Encoder
	Log     *slog.Logger
}

func (c Config) opusRate() int {
	if c.OpusRate > 0 {
		return c.OpusRate
	}
	return OpusRate
}

// Normalizer turns a camera's audio into Format frames. out is called from whichever
// goroutine produced the frame (the caller's for passthrough and silence, the ffmpeg
// reader's for transcodes), so out must be safe for that.
type Normalizer struct {
	cfg Config
	out func(media.AudioFrame)
	log *slog.Logger

	mu      sync.Mutex
	format  Format
	in      media.Codec // the camera's codec; None when silent
	tc      *transcoder
	enc     *Encoder // shared ffmpeg, replaces tc when set
	leave   func()   // detaches from enc
	tcAAC   bool     // tc produces the AAC/format frames (else it is Opus-only and the camera's pass through)
	gen     uint64   // bumped per transcoder; emit drops frames from a replaced one
	silence bool
	next    time.Duration // PTS of the next silent frame
	nextOp  time.Duration // PTS of the next silent Opus packet
	started bool
	base    time.Duration // Start's base
}

func New(cfg Config, out func(media.AudioFrame)) *Normalizer {
	lg := cfg.Log
	if lg == nil {
		lg = slog.Default()
	}
	return &Normalizer{cfg: cfg, out: out, log: lg, silence: true, enc: cfg.Encoder,
		format: Format{Codec: media.AAC, Rate: AACRate, Channels: 1, Config: AACConfig, Silent: true}}
}

// SetTracks picks the output format for a stream and returns it. It stops a previous
// transcoder; call Start for the new connection afterwards.
func (n *Normalizer) SetTracks(t media.Tracks) Format {
	n.mu.Lock()
	old := n.detachLocked()
	defer func() {
		n.mu.Unlock()
		if old != nil {
			old() // unlocked: see detachLocked
		}
	}()
	n.in = t.Audio
	n.silence = false
	n.tcAAC = false
	// opusFor starts an Opus-only ffmpeg for audio that passes through as is.
	opusFor := func() {
		if n.cfg.OpusOut == nil {
			return
		}
		if !ffmpegAvailable(n.cfg.FFmpegPath) || !opusEncoderAvailable(ffmpegPath(n.cfg.FFmpegPath)) {
			n.log.Warn("audio: ffmpeg with libopus not found, no Opus track", "codec", t.Audio)
			return
		}
		n.gen++
		gen := n.gen
		shared := n.enc != nil
		opus := func(f media.AudioFrame) { n.emitOpus(gen, f, shared) }
		if shared {
			n.leave = n.enc.Join(t, false, n.cfg.opusRate(), nil, opus)
			return
		}
		n.tc = newTranscoder(n.cfg, t, nil, opus, n.log)
	}
	switch {
	case !n.cfg.Enabled || t.Audio == media.None:
		n.silence = true
		n.in = media.None
		n.format = Format{Codec: media.AAC, Rate: AACRate, Channels: 1, Config: AACConfig, Silent: true}
	case t.Audio == media.AAC && len(t.AudioConfig) >= 2:
		n.format = Format{Codec: media.AAC, Rate: t.AudioRate, Channels: max(t.AudioChannels, 1),
			Config: append([]byte(nil), t.AudioConfig...)}
		opusFor()
	case (t.Audio == media.PCMU || t.Audio == media.PCMA) && !n.cfg.TranscodeG711:
		n.format = Format{Codec: t.Audio, Rate: 8000, Channels: 1}
		opusFor()
	case t.Audio == media.PCMU || t.Audio == media.PCMA || t.Audio == media.Opus:
		if !ffmpegAvailable(n.cfg.FFmpegPath) {
			n.log.Warn("audio: ffmpeg not found, pushing silence instead", "codec", t.Audio)
			n.silence, n.in = true, media.None
			n.format = Format{Codec: media.AAC, Rate: AACRate, Channels: 1, Config: AACConfig, Silent: true}
			break
		}
		n.format = Format{Codec: media.AAC, Rate: AACRate, Channels: 1, Config: AACConfig}
		n.gen++
		gen := n.gen
		shared := n.enc != nil
		var opus func(media.AudioFrame)
		if n.cfg.OpusOut != nil && opusEncoderAvailable(ffmpegPath(n.cfg.FFmpegPath)) {
			opus = func(f media.AudioFrame) { n.emitOpus(gen, f, shared) }
		}
		n.tcAAC = true
		aac := func(f media.AudioFrame) { n.emit(gen, f, shared) }
		if shared {
			n.leave = n.enc.Join(t, true, n.cfg.opusRate(), aac, opus)
			break
		}
		n.tc = newTranscoder(n.cfg, t, aac, opus, n.log)
	default: // AAC without a config, or a codec FLV can't hold
		n.silence, n.in = true, media.None
		n.format = Format{Codec: media.AAC, Rate: AACRate, Channels: 1, Config: AACConfig, Silent: true}
	}
	return n.format
}

// Format is the output format chosen by the last SetTracks.
func (n *Normalizer) Format() Format {
	n.mu.Lock()
	defer n.mu.Unlock()
	return n.format
}

// Start begins a connection's audio at base (the first IDR's PTS): silence is paced from it.
func (n *Normalizer) Start(base time.Duration) {
	n.mu.Lock()
	defer n.mu.Unlock()
	n.next, n.nextOp, n.started, n.base = base, base, true, base
}

// Stop ends the connection's audio (before the next Start).
func (n *Normalizer) Stop() {
	n.mu.Lock()
	defer n.mu.Unlock()
	n.started = false
}

// Close stops any ffmpeg.
func (n *Normalizer) Close() {
	n.mu.Lock()
	n.started = false
	old := n.detachLocked()
	n.mu.Unlock()
	if old != nil {
		old()
	}
}

// SetEncoder switches to another shared Encoder (the channel moved to a different camera
// URL), leaving the old one. SetTracks joins the new one when the new stream's tracks arrive.
func (n *Normalizer) SetEncoder(e *Encoder) {
	n.mu.Lock()
	if n.enc == e {
		n.mu.Unlock()
		return
	}
	old := n.detachLocked()
	n.enc = e
	n.mu.Unlock()
	if old != nil {
		old()
	}
}

// detachLocked takes the transcoder (or the shared-encoder subscription) away; the caller
// runs the returned func after unlocking. Closing it
// under n.mu deadlocked: close waits for ffmpeg's reader, whose last frame waits in emit for
// n.mu (live 2026-11-12 12:02 after a Wi-Fi change: a channel's teardown hung there and held
// the manager's lock, so get_stats and every configure_camera hung with it).
func (n *Normalizer) detachLocked() func() {
	tc, leave := n.tc, n.leave
	n.tc, n.leave = nil, nil
	n.gen++ // frames still coming from tc are dropped in emit
	if tc == nil && leave == nil {
		return nil
	}
	return func() {
		if tc != nil {
			tc.close()
		}
		if leave != nil {
			leave()
		}
	}
}

// emit/emitOpus gate a transcoder's frames: only while started and from the current
// transcoder. shared marks frames from a shared Encoder, whose ffmpeg has run since before
// this connection's first IDR: its frames older than base are dropped here. Kept, they made
// a late-joining pusher's Opus grid (lastO+20 per frame) race ahead of its video, and Pace
// then held each video tag behind the future Opus tag (video2 last_frame_age 800 ms against
// 30 ms for video3 on the same URL, live 2026-11-12; local repro 1.2 s for a channel added
// 1.3 s after the first). A private ffmpeg started after Start, so it had none.
func (n *Normalizer) emit(gen uint64, f media.AudioFrame, shared bool) {
	n.mu.Lock()
	ok := n.started && gen == n.gen && !(shared && f.PTS < n.base)
	n.mu.Unlock()
	if ok {
		n.out(f)
	}
}

func (n *Normalizer) emitOpus(gen uint64, f media.AudioFrame, shared bool) {
	n.mu.Lock()
	ok := n.started && gen == n.gen && !(shared && f.PTS < n.base)
	n.mu.Unlock()
	if ok && n.cfg.OpusOut != nil {
		n.cfg.OpusOut(f)
	}
}

// OnAudio takes one camera frame. Ignored for silent formats.
func (n *Normalizer) OnAudio(f media.AudioFrame) {
	n.mu.Lock()
	if !n.started || n.silence {
		n.mu.Unlock()
		return
	}
	tc, tcAAC := n.tc, n.tcAAC
	n.mu.Unlock()
	if tc != nil {
		tc.write(f)
	}
	if !tcAAC {
		n.out(f) // passthrough (an Opus-only ffmpeg, if any, got a copy above)
	}
}

// OnVideo paces the silent track on video PTS: one AAC frame (32 ms at 32 kHz) per
// 1024 samples up to this access unit's PTS.
func (n *Normalizer) OnVideo(pts time.Duration) {
	n.mu.Lock()
	if !n.started || !n.silence {
		n.mu.Unlock()
		return
	}
	var frames, opus []media.AudioFrame
	step := time.Second * aacFrameLen / AACRate
	for n.next <= pts {
		frames = append(frames, media.AudioFrame{PTS: n.next, Data: silentFrame})
		n.next += step
	}
	// Silent Opus on its own 20 ms grid: the browser live view has only the Opus track.
	for n.cfg.OpusOut != nil && n.nextOp <= pts {
		opus = append(opus, media.AudioFrame{PTS: n.nextOp, Data: silentOpus})
		n.nextOp += OpusFrameDur * time.Millisecond
	}
	n.mu.Unlock()
	for _, f := range frames {
		n.out(f)
	}
	for _, f := range opus {
		n.cfg.OpusOut(f)
	}
}

// silentFrame is a raw AAC-LC access unit of silence at 32 kHz mono, as ffmpeg's aac
// encoder writes for anullsrc (steady-state frames are all these 4 bytes; checked by
// decoding it with ffprobe).
var silentFrame = []byte{0x01, 0x18, 0x20, 0x07}
