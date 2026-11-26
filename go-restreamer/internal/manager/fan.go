// SPDX-License-Identifier: AGPL-3.0-or-later

package manager

import (
	"errors"
	"sync"

	"diyport/go-restreamer/internal/media"
	"diyport/go-restreamer/internal/transcode"
)

// errHEVC is the symbolic reason get_stats reports (and the Python logs) for an H.265 camera
// on a machine with no hardware H.264 encoder.
const errHEVC = "ERR_UNSUPPORTED_CODEC_HEVC"

// fan is one channel's downstream: the pusher, plus the detection server on video1. It
// remembers the stream's tracks so a subscriber added later (a pusher replaced by a changed
// configure_camera) starts at once instead of waiting for the camera to reconnect. Calls into
// subscribers hold mu: they never call back, and it keeps each one's calls ordered across the
// pull's goroutine and the RPC's.
type fan struct {
	mu   sync.Mutex
	subs []media.Subscriber
	last *media.Tracks
	err  string
}

var _ media.Subscriber = (*fan)(nil)

func (f *fan) add(s media.Subscriber) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.subs = append(f.subs, s)
	if f.last != nil {
		s.OnTracks(*f.last)
	}
}

func (f *fan) remove(s media.Subscriber) {
	f.mu.Lock()
	defer f.mu.Unlock()
	for i, x := range f.subs {
		if x == s {
			f.subs = append(f.subs[:i:i], f.subs[i+1:]...)
			return
		}
	}
}

func (f *fan) reason() string {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.err
}

func (f *fan) OnTracks(t media.Tracks) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.last, f.err = &t, ""
	for _, s := range f.subs {
		s.OnTracks(t)
	}
}

func (f *fan) OnVideo(au media.VideoAU) {
	f.mu.Lock()
	defer f.mu.Unlock()
	for _, s := range f.subs {
		s.OnVideo(au)
	}
}

func (f *fan) OnAudio(a media.AudioFrame) {
	f.mu.Lock()
	defer f.mu.Unlock()
	for _, s := range f.subs {
		s.OnAudio(a)
	}
}

func (f *fan) OnDisconnect(err error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.last = nil
	if errors.Is(err, transcode.ErrUnsupportedHEVC) {
		f.err = errHEVC
	}
	for _, s := range f.subs {
		s.OnDisconnect(err)
	}
}
