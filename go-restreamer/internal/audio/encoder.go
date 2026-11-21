// SPDX-License-Identifier: AGPL-3.0-or-later

package audio

import (
	"log/slog"
	"sync"

	"diyport/go-restreamer/internal/media"
)

// Encoder is one ffmpeg shared by every Normalizer that pulls the same camera RTSP URL
// (CPU: three encoders per camera, one per channel, 2026-11-12). Channels on one URL (video2
// and video3 on Channels/102) get identical camera audio on one session's PTS clock, so the
// AAC/Opus frames one ffmpeg makes are valid for all of them; each pusher still applies its
// own base PTS, pacing and reorder buffer to the shared frames. Never share across URLs:
// different sessions have different PTS bases.
//
// The owner (the manager's router) feeds it camera frames once with OnAudio and calls Reset
// when the camera's stream restarts (new tracks, or a disconnect), because the encoder
// stamps frames by sample count from its first input PTS. Normalizers Join it from
// SetTracks: the first one spawns ffmpeg, later ones (a second channel, a pusher replaced
// or reconnecting) attach to the running one, and the last one leaving stops it. Frames go
// to subscribers without e.mu held and closing a transcoder is always done unlocked, for
// the reason in Normalizer.detachLocked (close waits for the reader, which needs e.mu).
type Encoder struct {
	cfg Config
	log *slog.Logger

	mu     sync.Mutex
	gen    uint64         // bumped per transcoder; its late frames are dropped in fan
	tcs    map[int]*encTC // by Opus rate (0: Opus-less subscribers); see Join
	subs   map[uint64]encSub
	next   uint64
	closed bool
	starts uint64 // transcoders (ffmpeg supervisors) ever spawned, for stats and tests
}

type encSub struct {
	aac, opus func(media.AudioFrame)
	key       int // the tcs key its frames come from
}

type encTC struct {
	tc  *transcoder
	gen uint64
}

// NewEncoder makes an idle Encoder; no ffmpeg runs until the first Join.
func NewEncoder(ffmpegPath string, log *slog.Logger) *Encoder {
	if log == nil {
		log = slog.Default()
	}
	return &Encoder{cfg: Config{FFmpegPath: ffmpegPath, Log: log}, log: log, subs: map[uint64]encSub{}, tcs: map[int]*encTC{}}
}

// Starts is how many ffmpeg transcoders this Encoder has spawned.
func (e *Encoder) Starts() uint64 {
	e.mu.Lock()
	defer e.mu.Unlock()
	return e.starts
}

// Subscribers is how many Normalizers are attached.
func (e *Encoder) Subscribers() int {
	e.mu.Lock()
	defer e.mu.Unlock()
	return len(e.subs)
}

// Running reports whether an ffmpeg is up.
func (e *Encoder) Running() bool {
	e.mu.Lock()
	defer e.mu.Unlock()
	return len(e.tcs) > 0
}

// Join attaches a subscriber and returns its leave func (idempotent; call it with no lock
// held that aac or opus take). wantAAC is whether ffmpeg must also produce AAC (G.711 or Opus
// camera audio) or only Opus (camera AAC / copied G.711, which the subscriber passes through
// itself). All subscribers of one Encoder have the same tracks and AAC option. opusRate is the
// sample rate its Opus is encoded at (ignored when opus is nil): Protect asks for one rate per
// channel (ChangeVideoSettings opusSampleRate, 24000 live 2026-11-12) and a clock Protect reads
// at another rate hangs the browser live view, so each distinct rate gets its own ffmpeg, whose
// frames go only to the subscribers of that rate (including its AAC copy: a second rate costs a
// second AAC encode, which is rare and cheap next to a wrong clock). Subscribers without Opus
// share a key of their own.
func (e *Encoder) Join(t media.Tracks, wantAAC bool, opusRate int, aac, opus func(media.AudioFrame)) (leave func()) {
	e.mu.Lock()
	defer e.mu.Unlock()
	if e.closed {
		return func() {}
	}
	id := e.next
	e.next++
	key := 0
	if opus != nil {
		key = opusRate
	}
	e.subs[id] = encSub{aac, opus, key}
	if e.tcs[key] == nil {
		e.gen++
		gen := e.gen
		var o, p func(media.AudioFrame)
		if wantAAC {
			o = func(f media.AudioFrame) { e.fan(gen, key, f, false) }
		}
		if opus != nil {
			p = func(f media.AudioFrame) { e.fan(gen, key, f, true) }
		}
		cfg := e.cfg
		cfg.OpusRate = key
		e.tcs[key] = &encTC{newTranscoder(cfg, t, o, p, e.log), gen}
		e.starts++
	}
	return func() { e.leave(id) }
}

func (e *Encoder) leave(id uint64) {
	e.mu.Lock()
	var old []*transcoder
	if sub, ok := e.subs[id]; ok {
		delete(e.subs, id)
		if !e.keyUsedLocked(sub.key) {
			if c := e.tcs[sub.key]; c != nil {
				old = append(old, c.tc)
				delete(e.tcs, sub.key)
			}
		}
	}
	e.mu.Unlock()
	for _, tc := range old {
		tc.close() // unlocked: see the type comment
	}
}

func (e *Encoder) keyUsedLocked(key int) bool {
	for _, s := range e.subs {
		if s.key == key {
			return true
		}
	}
	return false
}

func (e *Encoder) detachLocked() []*transcoder {
	var old []*transcoder
	for k, c := range e.tcs {
		old = append(old, c.tc)
		delete(e.tcs, k)
	}
	return old
}

// Reset stops the running ffmpeg but keeps the subscribers: the camera's stream restarted,
// and the next Join (each Normalizer re-Joins from SetTracks) starts one on the new clock.
func (e *Encoder) Reset() {
	if e == nil {
		return
	}
	e.mu.Lock()
	old := e.detachLocked()
	e.mu.Unlock()
	for _, tc := range old {
		tc.close()
	}
}

// Close stops ffmpeg and drops every subscriber (the URL has no channels left).
func (e *Encoder) Close() {
	if e == nil {
		return
	}
	e.mu.Lock()
	e.closed = true
	e.subs = map[uint64]encSub{}
	old := e.detachLocked()
	e.mu.Unlock()
	for _, tc := range old {
		tc.close()
	}
}

// OnAudio feeds one camera frame to ffmpeg (a no-op while none runs).
func (e *Encoder) OnAudio(f media.AudioFrame) {
	if e == nil {
		return
	}
	e.mu.Lock()
	tcs := make([]*transcoder, 0, len(e.tcs))
	for _, c := range e.tcs {
		tcs = append(tcs, c.tc)
	}
	e.mu.Unlock()
	for _, tc := range tcs {
		tc.write(f)
	}
}

// fan hands a frame from the reader goroutine to every subscriber, unlocked. The Data slice
// is shared: subscribers only read it.
func (e *Encoder) fan(gen uint64, key int, f media.AudioFrame, opus bool) {
	e.mu.Lock()
	if c := e.tcs[key]; c == nil || c.gen != gen {
		e.mu.Unlock()
		return
	}
	subs := make([]encSub, 0, len(e.subs))
	for _, s := range e.subs {
		if s.key == key {
			subs = append(subs, s)
		}
	}
	e.mu.Unlock()
	for _, s := range subs {
		if opus && s.opus != nil {
			s.opus(f)
		} else if !opus && s.aac != nil {
			s.aac(f)
		}
	}
}
