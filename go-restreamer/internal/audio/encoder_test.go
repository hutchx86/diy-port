// SPDX-License-Identifier: AGPL-3.0-or-later

package audio

import (
	"bytes"
	"sync"
	"testing"
	"time"

	"diyport/go-restreamer/internal/media"
)

// feed pushes 20 ms of G.711 silence per tick into the shared encoder until stop closes.
func feed(e *Encoder, stop chan struct{}) {
	pkt := bytes.Repeat([]byte{0xff}, 160)
	for i := 0; ; i++ {
		select {
		case <-stop:
			return
		default:
		}
		e.OnAudio(media.AudioFrame{PTS: time.Duration(i) * 20 * time.Millisecond, Data: pkt})
		time.Sleep(5 * time.Millisecond)
	}
}

func waitN(t *testing.T, what string, f func() bool) {
	t.Helper()
	for end := time.Now().Add(15 * time.Second); time.Now().Before(end); time.Sleep(20 * time.Millisecond) {
		if f() {
			return
		}
	}
	t.Fatalf("timeout: %s", what)
}

// Two Normalizers on one Encoder (CPU: 3 encoders per camera, 2026-11-12): one ffmpeg, the
// same AAC and Opus frames to both, one leaving leaves the other flowing, both leaving stops
// ffmpeg.
func TestSharedEncoderOneFFmpegForTwoNormalizers(t *testing.T) {
	if !ffmpegAvailable("") || !opusEncoderAvailable(ffmpegPath("")) {
		t.Skip("no ffmpeg with libopus")
	}
	enc := NewEncoder("", nil)
	tr := media.Tracks{Audio: media.PCMU, AudioRate: 8000, AudioChannels: 1}
	var mu sync.Mutex
	var aac, opus [2][]media.AudioFrame
	mk := func(i int) *Normalizer {
		return New(Config{Enabled: true, TranscodeG711: true, Encoder: enc,
			OpusOut: func(f media.AudioFrame) { mu.Lock(); opus[i] = append(opus[i], f); mu.Unlock() }},
			func(f media.AudioFrame) { mu.Lock(); aac[i] = append(aac[i], f); mu.Unlock() })
	}
	count := func(s *[2][]media.AudioFrame, i int) int { mu.Lock(); defer mu.Unlock(); return len(s[i]) }
	n0, n1 := mk(0), mk(1)
	enc.Reset()
	n0.SetTracks(tr)
	n1.SetTracks(tr)
	n0.Start(0)
	n1.Start(0)
	if enc.Starts() != 1 || enc.Subscribers() != 2 || !enc.Running() {
		t.Fatalf("starts=%d subs=%d running=%v", enc.Starts(), enc.Subscribers(), enc.Running())
	}
	stop := make(chan struct{})
	done := make(chan struct{})
	go func() { feed(enc, stop); close(done) }()
	defer func() { close(stop); <-done }()

	waitN(t, "frames on both", func() bool {
		return count(&aac, 0) > 5 && count(&aac, 1) > 5 && count(&opus, 0) > 5 && count(&opus, 1) > 5
	})
	mu.Lock()
	for _, s := range [][2][]media.AudioFrame{aac, opus} {
		k := min(len(s[0]), len(s[1]))
		for j := 0; j < k; j++ {
			if s[0][j].PTS != s[1][j].PTS || !bytes.Equal(s[0][j].Data, s[1][j].Data) {
				mu.Unlock()
				t.Fatalf("frame %d differs between normalizers", j)
			}
		}
	}
	mu.Unlock()

	n0.Close() // one leaves: the other keeps flowing
	before := count(&aac, 1)
	waitN(t, "n1 still flowing", func() bool { return count(&aac, 1) > before+3 })
	if enc.Starts() != 1 || !enc.Running() || enc.Subscribers() != 1 {
		t.Fatalf("after one left: starts=%d subs=%d running=%v", enc.Starts(), enc.Subscribers(), enc.Running())
	}
	n1.Close()
	if enc.Running() || enc.Subscribers() != 0 {
		t.Fatalf("after both left: subs=%d running=%v", enc.Subscribers(), enc.Running())
	}
	// A pusher joining later gets the format at once and attaches to a fresh ffmpeg.
	n2 := mk(0)
	if f := n2.SetTracks(tr); f.Codec != media.AAC || f.Silent {
		t.Fatalf("late joiner format %+v", f)
	}
	n2.Close()
}

// Teardown with a slow subscriber while ffmpeg is emitting must not hang (same race as
// TestNormalizerTeardownWhileEmitting, through the shared path).
func TestSharedEncoderTeardownWhileEmitting(t *testing.T) {
	if !ffmpegAvailable("") {
		t.Skip("no ffmpeg")
	}
	tr := media.Tracks{Audio: media.PCMU, AudioRate: 8000, AudioChannels: 1}
	for round := 0; round < 3; round++ {
		enc := NewEncoder("", nil)
		n := New(Config{Enabled: true, TranscodeG711: true, Encoder: enc}, func(media.AudioFrame) { time.Sleep(time.Millisecond) })
		n.SetTracks(tr)
		n.Start(0)
		stop, fed := make(chan struct{}), make(chan struct{})
		go func() { feed(enc, stop); close(fed) }()
		time.Sleep(300 * time.Millisecond)
		done := make(chan struct{})
		go func() {
			enc.Reset()
			n.SetTracks(tr)
			time.Sleep(100 * time.Millisecond)
			n.Close()
			enc.Close()
			close(done)
		}()
		select {
		case <-done:
		case <-time.After(10 * time.Second):
			t.Fatalf("round %d: teardown hung", round)
		}
		close(stop)
		<-fed
	}
}

// audio_enabled=false keeps its silent track and never joins the encoder.
func TestSharedEncoderSkippedWhenDisabled(t *testing.T) {
	enc := NewEncoder("", nil)
	n := New(Config{Enabled: false, TranscodeG711: true, Encoder: enc}, func(media.AudioFrame) {})
	if f := n.SetTracks(media.Tracks{Audio: media.PCMU}); !f.Silent {
		t.Fatalf("format %+v", f)
	}
	if enc.Subscribers() != 0 || enc.Running() {
		t.Fatal("disabled normalizer joined the encoder")
	}
}

// A shared encoder has run since before a pusher's Start: frames stamped before its base
// are dropped for that pusher, later ones pass.
func TestSharedFramesBeforeBaseDropped(t *testing.T) {
	if !ffmpegAvailable("") || !opusEncoderAvailable(ffmpegPath("")) {
		t.Skip("no ffmpeg with libopus")
	}
	enc := NewEncoder("", nil)
	var mu sync.Mutex
	var got []media.AudioFrame
	n := New(Config{Enabled: true, TranscodeG711: true, Encoder: enc,
		OpusOut: func(f media.AudioFrame) { mu.Lock(); got = append(got, f); mu.Unlock() }}, func(media.AudioFrame) {})
	n.SetTracks(media.Tracks{Audio: media.PCMU, AudioRate: 8000, AudioChannels: 1})
	n.Start(time.Second) // frames fed below start at PTS 0
	stop, done := make(chan struct{}), make(chan struct{})
	go func() { feed(enc, stop); close(done) }()
	defer func() { close(stop); <-done; n.Close() }()
	waitN(t, "opus past base", func() bool { mu.Lock(); defer mu.Unlock(); return len(got) > 5 })
	mu.Lock()
	defer mu.Unlock()
	for _, f := range got {
		if f.PTS < time.Second {
			t.Fatalf("frame at %v is before base 1s", f.PTS)
		}
	}
}

// Protect asks for one Opus rate per channel (opusSampleRate 24000 live 2026-11-12): the rate
// validation, ffmpeg's -ar, and one ffmpeg per distinct rate on a shared Encoder.
func TestOpusRateFor(t *testing.T) {
	for _, c := range []struct {
		in, want int
		ok       bool
	}{{0, 48000, true}, {24000, 24000, true}, {16000, 16000, true}, {8000, 8000, true}, {12000, 12000, true},
		{48000, 48000, true}, {44100, 48000, false}, {-1, 48000, false}} {
		if got, ok := OpusRateFor(c.in); got != c.want || ok != c.ok {
			t.Errorf("OpusRateFor(%d) = %d,%v want %d,%v", c.in, got, ok, c.want, c.ok)
		}
	}
}

func TestOpusEncodeRateInFFmpegArgs(t *testing.T) {
	for rate, want := range map[int]string{0: "48000", 24000: "24000", 16000: "16000"} {
		tc := &transcoder{cfg: Config{OpusRate: rate}, codec: media.PCMU, rate: 8000, chans: 1,
			opus: func(media.AudioFrame) {}}
		args := tc.args()
		var got string
		for i, a := range args {
			if a == "libopus" {
				for j := i; j < len(args)-1; j++ {
					if args[j] == "-ar" {
						got = args[j+1]
						break
					}
				}
			}
		}
		if got != want {
			t.Errorf("OpusRate %d: libopus -ar %q want %s (%v)", rate, got, want, args)
		}
	}
}

func TestSharedEncoderOneFFmpegPerOpusRate(t *testing.T) {
	if !ffmpegAvailable("") || !opusEncoderAvailable(ffmpegPath("")) {
		t.Skip("no ffmpeg with libopus")
	}
	enc := NewEncoder("", nil)
	tr := media.Tracks{Audio: media.PCMU, AudioRate: 8000, AudioChannels: 1}
	var mu sync.Mutex
	var got [3][]media.AudioFrame
	mk := func(i, rate int) *Normalizer {
		return New(Config{Enabled: true, TranscodeG711: true, Encoder: enc, OpusRate: rate,
			OpusOut: func(f media.AudioFrame) { mu.Lock(); got[i] = append(got[i], f); mu.Unlock() }},
			func(media.AudioFrame) {})
	}
	n := [3]*Normalizer{mk(0, 24000), mk(1, 24000), mk(2, 48000)}
	enc.Reset()
	for _, x := range n {
		x.SetTracks(tr)
		x.Start(0)
	}
	if enc.Starts() != 2 || enc.Subscribers() != 3 {
		t.Fatalf("starts=%d subs=%d: want one ffmpeg per rate (24000 shared by two, 48000)", enc.Starts(), enc.Subscribers())
	}
	stop, done := make(chan struct{}), make(chan struct{})
	go func() { feed(enc, stop); close(done) }()
	defer func() {
		close(stop)
		<-done
		for _, x := range n {
			x.Close()
		}
		enc.Close()
	}()
	waitN(t, "opus on all three", func() bool {
		mu.Lock()
		defer mu.Unlock()
		return len(got[0]) > 5 && len(got[1]) > 5 && len(got[2]) > 5
	})
	// The 24000 ffmpeg's frames reach only the two 24000 subscribers: the first two see identical packets.
	mu.Lock()
	defer mu.Unlock()
	k := min(len(got[0]), len(got[1]), 5)
	for i := 0; i < k; i++ {
		if got[0][i].PTS != got[1][i].PTS || !bytes.Equal(got[0][i].Data, got[1][i].Data) {
			t.Fatalf("shared-rate subscribers differ at %d", i)
		}
	}
	// One leaving the 24000 pair keeps it; the last 24000 leaving stops only that ffmpeg.
	n[0].Close()
	if !enc.Running() {
		t.Fatal("encoder stopped with subscribers left")
	}
}

// The AI Port exact push asks the shared encoder for AAC at 48 kHz and 128 kbps (spec 7); the
// default stays 32 kHz / 32 kbps.
func TestAACRateAndBitrateArgs(t *testing.T) {
	has := func(a []string, k, v string) bool {
		for i := 0; i+1 < len(a); i++ {
			if a[i] == k && a[i+1] == v {
				return true
			}
		}
		return false
	}
	enc := NewEncoderAAC("ffmpeg", 48000, 128000, nil)
	a := (&transcoder{cfg: enc.cfg, codec: media.PCMA, out: func(media.AudioFrame) {}}).args()
	if !has(a, "-ar", "48000") || !has(a, "-b:a", "128k") {
		t.Errorf("exact args %v", a)
	}
	a = (&transcoder{cfg: Config{}, codec: media.PCMA, out: func(media.AudioFrame) {}}).args()
	if !has(a, "-ar", "32000") || !has(a, "-b:a", "32k") {
		t.Errorf("default args %v", a)
	}
}
