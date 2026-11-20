// SPDX-License-Identifier: AGPL-3.0-or-later

package manager

import (
	"log/slog"
	"sync"

	"diyport/go-restreamer/internal/audio"
	"diyport/go-restreamer/internal/media"
	"diyport/go-restreamer/internal/transcode"
)

// member is one channel fed by a router, with the transcode profile it would get.
type member struct {
	fan     *fan
	profile transcode.Profile
}

// router is the pool subscriber for one camera RTSP URL. Python always asks for mode "copy",
// so what a stream needs is only known from its Tracks: H.264 goes straight to the channels
// (the camera's own bits, as the ffmpeg -c copy does); H.265, or mode "transcode", goes
// through one Transcoder that decodes once and encodes each member's profile. A Transcoder
// is made on first need, and rebuilt when members or their profiles change (its profiles are
// fixed at construction).
type router struct {
	name string // device id, for the transcoder's logs
	url  string // credentials and all: only for pool keys; never logged
	caps transcode.Caps
	log  *slog.Logger

	mu      sync.Mutex
	mode    string
	members map[string]member
	order   []string
	last    *media.Tracks
	tc      *transcode.Transcoder // non-nil while the stream goes through transcode
	// enc is this URL's shared audio ffmpeg: fed the camera audio once here (not once per
	// channel) and reset whenever the stream restarts, as its PTS clock restarts with it.
	enc *audio.Encoder
}

var _ media.Subscriber = (*router)(nil)

func newRouter(name, url, mode string, caps transcode.Caps, log *slog.Logger) *router {
	return &router{name: name, url: url, mode: mode, caps: caps, log: log, members: map[string]member{}}
}

func (r *router) wantTC(t media.Tracks) bool {
	return r.mode == "transcode" || r.mode == modeAIPort || t.Video == media.H265
}

// set adds or updates a channel. A transcoder in use is rebuilt if the profiles it has to
// make changed; a new member of a direct stream starts right away from the last tracks.
func (r *router) set(channel string, m member) {
	r.mu.Lock()
	defer r.mu.Unlock()
	old, had := r.members[channel]
	r.members[channel] = m
	if !had {
		r.order = append(r.order, channel)
	}
	if r.tc != nil && (!had || old.profile != m.profile || old.fan != m.fan) {
		r.reapplyLocked()
	}
	if r.tc == nil && r.last != nil && (!had || old.fan != m.fan) && !r.wantTC(*r.last) {
		m.fan.OnTracks(*r.last)
	}
}

// del removes a channel and reports how many remain.
func (r *router) del(channel string) int {
	r.mu.Lock()
	defer r.mu.Unlock()
	if _, ok := r.members[channel]; ok {
		delete(r.members, channel)
		for i, c := range r.order {
			if c == channel {
				r.order = append(r.order[:i:i], r.order[i+1:]...)
				break
			}
		}
		if r.tc != nil {
			r.reapplyLocked()
		}
	}
	return len(r.members)
}

func (r *router) setMode(mode string) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.mode == mode {
		return
	}
	r.mode = mode
	r.reapplyLocked()
}

// reapplyLocked re-decides the route for the stream now flowing: used when the transcoder's
// shape or the mode changed.
func (r *router) reapplyLocked() {
	if r.tc != nil {
		r.tc.Close()
		r.tc = nil
	}
	if r.last != nil {
		r.applyLocked(*r.last)
	}
}

func (r *router) applyLocked(t media.Tracks) {
	if t.Video != media.H264 && t.Video != media.H265 {
		r.log.Warn("camera video codec is not supported", "device", r.name, "codec", t.Video)
		return
	}
	if !r.wantTC(t) {
		if r.tc != nil { // was transcoding (mode changed): stop ffmpeg, channels see a new stream
			r.tc.OnDisconnect(nil)
			r.tc.Close()
			r.tc = nil
		}
		for _, c := range r.order {
			r.members[c].fan.OnTracks(t)
		}
		return
	}
	if r.tc == nil {
		profiles := make([]transcode.Profile, 0, len(r.order))
		for _, c := range r.order {
			profiles = append(profiles, r.members[c].profile)
		}
		r.tc = transcode.New(transcode.Config{Name: r.name, Caps: r.caps, Profiles: profiles, Log: r.log})
		for _, c := range r.order {
			r.tc.Output(c, r.members[c].fan)
		}
		if t.Video == media.H265 {
			if err := r.tc.Check(t.Video); err != nil {
				r.log.Error("cannot serve this camera", "device", r.name, "err", err)
			}
		}
	}
	r.tc.OnTracks(t)
}

func (r *router) OnTracks(t media.Tracks) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.last = &t
	r.enc.Reset() // before the fan gives each pusher its tracks, so their Join starts a fresh ffmpeg
	r.applyLocked(t)
}

func (r *router) OnVideo(au media.VideoAU) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.tc != nil {
		r.tc.OnVideo(au)
		return
	}
	for _, c := range r.order {
		r.members[c].fan.OnVideo(au)
	}
}

func (r *router) OnAudio(f media.AudioFrame) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.enc.OnAudio(f)
	if r.tc != nil {
		r.tc.OnAudio(f)
		return
	}
	for _, c := range r.order {
		r.members[c].fan.OnAudio(f)
	}
}

func (r *router) OnDisconnect(err error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.last = nil
	r.enc.Reset()
	if r.tc != nil {
		r.tc.OnDisconnect(err)
	}
	for _, c := range r.order {
		if r.tc == nil {
			r.members[c].fan.OnDisconnect(err)
		}
	}
}

// close stops the transcoder (the pool subscription is the manager's to release).
func (r *router) close() {
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.tc != nil {
		r.tc.Close()
		r.tc = nil
	}
}

func (r *router) tcStats() (transcode.Stats, bool) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.tc == nil {
		return transcode.Stats{}, false
	}
	return r.tc.Stats(), true
}
