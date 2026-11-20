// SPDX-License-Identifier: AGPL-3.0-or-later

package rtsp

import (
	"context"
	"errors"
	"io"
	"log/slog"
	"strings"
	"sync"
	"testing"
	"time"

	"diyport/go-restreamer/internal/media"
)

type rec struct {
	mu     sync.Mutex
	tracks int
	video  []time.Duration
	disc   int
}

func (r *rec) OnTracks(media.Tracks) { r.mu.Lock(); r.tracks++; r.mu.Unlock() }
func (r *rec) OnVideo(a media.VideoAU) {
	r.mu.Lock()
	r.video = append(r.video, a.PTS)
	r.mu.Unlock()
}
func (r *rec) OnAudio(media.AudioFrame) {}
func (r *rec) OnDisconnect(error)       { r.mu.Lock(); r.disc++; r.mu.Unlock() }
func (r *rec) snap() (int, []time.Duration, int) {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.tracks, append([]time.Duration(nil), r.video...), r.disc
}

func waitFor(t *testing.T, what string, f func() bool) {
	t.Helper()
	for i := 0; i < 400; i++ {
		if f() {
			return
		}
		time.Sleep(10 * time.Millisecond)
	}
	t.Fatalf("timed out: %s", what)
}

func quiet() *slog.Logger { return slog.New(slog.NewTextHandler(io.Discard, nil)) }

func TestPoolDedupsAndReconnects(t *testing.T) {
	var mu sync.Mutex
	dials := map[string]int{}
	fail := make(chan struct{}, 1)
	failCh := func(u string) chan struct{} { // only /x ever fails
		if strings.HasSuffix(u, "/x") {
			return fail
		}
		return nil
	}
	dial := func(ctx context.Context, u string, sk Sink) error {
		mu.Lock()
		dials[u]++
		mu.Unlock()
		sk.Tracks(media.Tracks{Video: media.H264, Width: 640, Height: 360})
		sk.Video(media.VideoAU{PTS: 0, IDR: true})
		sk.Video(media.VideoAU{PTS: 50 * time.Millisecond})
		select {
		case <-ctx.Done():
			return nil
		case <-failCh(u):
			return errors.New("camera dropped rtsp://admin:hunter2@cam/x")
		}
	}
	p := NewPool(dial, quiet())
	const u = "rtsp://admin:hunter2@cam/x"
	a, b, c := &rec{}, &rec{}, &rec{}
	p.Acquire(u, a)
	p.Acquire(u, b)
	p.Acquire("rtsp://admin:hunter2@cam/y", c) // exact URL: a different session
	waitFor(t, "tracks", func() bool { n, _, _ := b.snap(); return n == 1 })

	mu.Lock()
	if dials[u] != 1 || dials["rtsp://admin:hunter2@cam/y"] != 1 {
		t.Fatalf("dials = %v, want one per URL", dials)
	}
	mu.Unlock()
	st := p.Stats()
	if len(st) != 2 || st[0].Subscribers != 2 || st[0].Sessions != 1 || st[0].Reconnects != 0 {
		t.Fatalf("stats = %+v", st)
	}
	for _, s := range st {
		if strings.Contains(s.URL, "hunter2") {
			t.Fatalf("password in stats: %s", s.URL)
		}
	}

	// A late joiner gets the live tracks straight away.
	d := &rec{}
	p.Acquire(u, d)
	if n, _, _ := d.snap(); n != 1 {
		t.Fatalf("late joiner tracks = %d", n)
	}

	// Drop: every subscriber hears it, the pull comes back (after the 1 s backoff) and PTS
	// carry on past the old ones.
	fail <- struct{}{}
	waitFor(t, "disconnect", func() bool { _, _, n := a.snap(); return n == 1 })
	waitFor(t, "reconnect", func() bool { n, _, _ := a.snap(); return n == 2 })
	_, v, _ := a.snap()
	if len(v) != 4 || v[2] <= v[1] {
		t.Fatalf("pts across reconnect = %v", v)
	}
	if st = p.Stats(); st[0].Sessions != 2 || st[0].Reconnects != 1 {
		t.Fatalf("stats after reconnect = %+v", st[0])
	}
	mu.Lock()
	if dials[u] != 2 {
		t.Fatalf("dials = %v", dials)
	}
	mu.Unlock()

	// Refcount: the session stays until the last subscriber leaves.
	p.Release(u, a)
	p.Release(u, b)
	if st = p.Stats(); len(st) != 2 || st[0].Subscribers != 1 {
		t.Fatalf("stats after partial release = %+v", st)
	}
	p.Release(u, d)
	p.Release("rtsp://admin:hunter2@cam/y", c)
	if st = p.Stats(); len(st) != 0 {
		t.Fatalf("pool not empty: %+v", st)
	}
}

func TestRedact(t *testing.T) {
	for in, want := range map[string]string{
		"rtsp://u:p%40ss@h:554/a?x=1": "rtsp://***@h:554/a?x=1",
		"rtsp://h/a":                  "rtsp://h/a",
	} {
		if got := Redact(in); got != want {
			t.Errorf("Redact(%q) = %q, want %q", in, got, want)
		}
	}
	if got := redactErr(errors.New("dial rtsp://u:p%40ss@h failed: u:p@ss"), "rtsp://u:p%40ss@h/a"); strings.Contains(got, "p%40ss") {
		t.Errorf("redactErr leaked: %s", got)
	}
}
