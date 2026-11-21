// SPDX-License-Identifier: AGPL-3.0-or-later

// Package rtsp pulls a camera's RTSP stream once per URL and fans it out to every subscriber
// of that URL. Channels that carry the same camera stream (video1 and the detection feed,
// say) share one session, as onvif_push.py's PullPool shares one ffmpeg: a camera has only
// a few RTSP sessions to give, and Frigate's go2rtc may already hold some of them (Live on
// Driveway: .42 refused a second session while go2rtc held the first).
package rtsp

import (
	"context"
	"fmt"
	"log/slog"
	"net/url"
	"sort"
	"strings"
	"sync"
	"time"

	"diyport/go-restreamer/internal/media"
)

// Reconnect backoff: 1, 2, 4, 8, 15 s. A session that delivered tracks resets it.
const (
	backoffMin = time.Second
	backoffMax = 15 * time.Second
)

// Sink is what a Dialer feeds. Tracks comes first, again if parameters change mid-session;
// PTS start at 0 for each session (the pool puts them on one clock across sessions).
type Sink interface {
	Tracks(media.Tracks)
	Video(media.VideoAU)
	Audio(media.AudioFrame)
}

// Dialer runs one RTSP session to completion: it connects, feeds the sink and returns when
// the session ends (nil only when ctx was cancelled). Injectable so tests need no server.
type Dialer func(ctx context.Context, rawURL string, sink Sink) error

// Stats is a per-URL snapshot for get_stats. URL is credential-redacted.
type Stats struct {
	URL         string
	Subscribers int
	Sessions    int  // sessions started, including the first
	Reconnects  int  // sessions started after a drop
	Connected   bool // tracks seen on the current session
	LastFrame   time.Time
}

// Pool refcounts one source per exact URL (a different query string is a different
// camera session).
type Pool struct {
	dial Dialer
	log  *slog.Logger

	mu      sync.Mutex
	entries map[string]*entry
}

// NewPool returns a pool using the gortsplib TCP dialer when dial is nil.
func NewPool(dial Dialer, log *slog.Logger) *Pool {
	if dial == nil {
		dial = GortsplibDialer
	}
	if log == nil {
		log = slog.Default()
	}
	return &Pool{dial: dial, log: log, entries: map[string]*entry{}}
}

type entry struct {
	pool   *Pool
	url    string
	redact string
	cancel context.CancelFunc
	done   chan struct{}

	mu        sync.Mutex // guards everything below; held while calling subscribers
	subs      []media.Subscriber
	tracks    *media.Tracks
	sessions  int
	reconnect int
	connected bool
	lastFrame time.Time
	lastPTS   time.Duration // highest PTS handed out, across sessions
	offset    time.Duration // added to this session's PTS
}

// Acquire subscribes sub to url, starting the pull if it is the first. A subscriber joining
// a live stream gets OnTracks at once; video joins wherever the camera is (subscribers wait
// for their own IDR).
func (p *Pool) Acquire(rawURL string, sub media.Subscriber) {
	// p.mu stays held while subscribing so a concurrent last Release can't close the entry
	// between the lookup and the append.
	p.mu.Lock()
	defer p.mu.Unlock()
	e := p.entries[rawURL]
	if e == nil {
		ctx, cancel := context.WithCancel(context.Background())
		e = &entry{pool: p, url: rawURL, redact: Redact(rawURL), cancel: cancel, done: make(chan struct{})}
		p.entries[rawURL] = e
		go e.run(ctx)
	}
	e.mu.Lock()
	defer e.mu.Unlock()
	e.subs = append(e.subs, sub)
	if e.tracks != nil {
		sub.OnTracks(*e.tracks)
	}
}

// Release unsubscribes sub; the last one out closes the session.
func (p *Pool) Release(rawURL string, sub media.Subscriber) {
	p.mu.Lock()
	e := p.entries[rawURL]
	if e == nil {
		p.mu.Unlock()
		return
	}
	e.mu.Lock()
	for i, s := range e.subs {
		if s == sub {
			e.subs = append(e.subs[:i:i], e.subs[i+1:]...)
			break
		}
	}
	empty := len(e.subs) == 0
	e.mu.Unlock()
	if empty {
		delete(p.entries, rawURL)
	}
	p.mu.Unlock()
	if empty {
		e.cancel()
		<-e.done
		p.log.Info("rtsp: closed", "url", e.redact)
	}
}

// Stats returns a snapshot for every live URL, sorted by redacted URL.
func (p *Pool) Stats() []Stats {
	p.mu.Lock()
	es := make([]*entry, 0, len(p.entries))
	for _, e := range p.entries {
		es = append(es, e)
	}
	p.mu.Unlock()
	out := make([]Stats, 0, len(es))
	for _, e := range es {
		e.mu.Lock()
		out = append(out, Stats{URL: e.redact, Subscribers: len(e.subs), Sessions: e.sessions,
			Reconnects: e.reconnect, Connected: e.connected, LastFrame: e.lastFrame})
		e.mu.Unlock()
	}
	sort.Slice(out, func(i, j int) bool { return out[i].URL < out[j].URL })
	return out
}

// run is the reconnect loop: dial, serve, tell subscribers, back off, repeat.
func (e *entry) run(ctx context.Context) {
	defer close(e.done)
	backoff := backoffMin
	for ctx.Err() == nil {
		e.mu.Lock()
		e.sessions++
		if e.sessions > 1 {
			e.reconnect++
		}
		e.offset = 0
		if e.lastPTS > 0 {
			e.offset = e.lastPTS + 40*time.Millisecond // one frame on, so the clock never steps back
		}
		e.mu.Unlock()

		sk := &sessionSink{e: e}
		err := e.pool.dial(ctx, e.url, sk)
		if ctx.Err() != nil {
			return
		}
		if err == nil {
			err = fmt.Errorf("session ended")
		}
		e.mu.Lock()
		got := sk.gotTracks
		e.connected = false
		e.tracks = nil
		for _, s := range e.subs {
			s.OnDisconnect(err)
		}
		e.mu.Unlock()
		if got {
			backoff = backoffMin
		}
		e.pool.log.Warn("rtsp: dropped, reconnecting", "url", e.redact, "err", redactErr(err, e.url),
			"in", backoff)
		select {
		case <-ctx.Done():
			return
		case <-time.After(backoff):
		}
		if backoff *= 2; backoff > backoffMax {
			backoff = backoffMax
		}
	}
}

// sessionSink is one session's view of the entry; it applies the PTS offset and fans out.
type sessionSink struct {
	e         *entry
	gotTracks bool
}

func (s *sessionSink) Tracks(t media.Tracks) {
	e := s.e
	e.mu.Lock()
	defer e.mu.Unlock()
	s.gotTracks = true
	e.connected = true
	e.tracks = &t
	e.pool.log.Info("rtsp: tracks", "url", e.redact, "video", t.Video, "w", t.Width, "h", t.Height,
		"audio", t.Audio)
	for _, sub := range e.subs {
		sub.OnTracks(t)
	}
}

func (s *sessionSink) Video(au media.VideoAU) {
	e := s.e
	e.mu.Lock()
	defer e.mu.Unlock()
	au.PTS += e.offset
	e.stamp(au.PTS)
	for _, sub := range e.subs {
		sub.OnVideo(au)
	}
}

func (s *sessionSink) Audio(f media.AudioFrame) {
	e := s.e
	e.mu.Lock()
	defer e.mu.Unlock()
	f.PTS += e.offset
	e.stamp(f.PTS)
	for _, sub := range e.subs {
		sub.OnAudio(f)
	}
}

func (e *entry) stamp(pts time.Duration) {
	e.lastFrame = time.Now()
	if pts > e.lastPTS {
		e.lastPTS = pts
	}
}

// Redact hides the password (and user) of rawURL for logs and stats; nothing that logs or
// returns an RTSP URL may skip it.
func Redact(rawURL string) string {
	u, err := url.Parse(rawURL)
	if err != nil || u.User == nil {
		return rawURL
	}
	u.User = nil
	return strings.Replace(u.String(), "//", "//***@", 1)
}

// redactErr scrubs the credentials out of an error that may quote the URL (gortsplib's
// do), including their percent-encoded forms.
func redactErr(err error, rawURL string) string {
	msg := err.Error()
	u, perr := url.Parse(rawURL)
	if perr != nil || u.User == nil {
		return msg
	}
	if pw, ok := u.User.Password(); ok && pw != "" {
		msg = strings.ReplaceAll(msg, pw, "***")
		msg = strings.ReplaceAll(msg, url.QueryEscape(pw), "***")
	}
	if name := u.User.Username(); name != "" {
		msg = strings.ReplaceAll(msg, name+":", "***:")
	}
	return msg
}
