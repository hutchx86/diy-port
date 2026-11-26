// SPDX-License-Identifier: AGPL-3.0-or-later

package audio

import (
	"bytes"
	"os"
	"os/exec"
	"path/filepath"
	"sync"
	"testing"
	"time"

	"diyport/go-restreamer/internal/media"
)

type sink struct {
	mu sync.Mutex
	fs []media.AudioFrame
}

func (s *sink) add(f media.AudioFrame) { s.mu.Lock(); s.fs = append(s.fs, f); s.mu.Unlock() }
func (s *sink) n() int                 { s.mu.Lock(); defer s.mu.Unlock(); return len(s.fs) }

func TestAACPassthrough(t *testing.T) {
	var s sink
	n := New(Config{Enabled: true}, s.add)
	f := n.SetTracks(media.Tracks{Audio: media.AAC, AudioRate: 16000, AudioChannels: 1, AudioConfig: []byte{0x14, 0x08}})
	if f.Codec != media.AAC || f.Rate != 16000 || f.Silent || !bytes.Equal(f.Config, []byte{0x14, 0x08}) {
		t.Fatalf("format %+v", f)
	}
	n.OnAudio(media.AudioFrame{PTS: 1, Data: []byte{1}}) // before Start: dropped
	n.Start(0)
	n.OnAudio(media.AudioFrame{PTS: 2, Data: []byte{2}})
	n.OnVideo(time.Second) // no silence over real audio
	if s.n() != 1 || s.fs[0].PTS != 2 {
		t.Errorf("frames %+v", s.fs)
	}
}

func TestG711CopiedByDefault(t *testing.T) {
	n := New(Config{Enabled: true}, func(media.AudioFrame) {})
	if f := n.SetTracks(media.Tracks{Audio: media.PCMU}); f.Codec != media.PCMU || f.Rate != 8000 {
		t.Errorf("format %+v", f)
	}
	n = New(Config{Enabled: true, TranscodeG711: true, FFmpegPath: "/nonexistent/ffmpeg"}, func(media.AudioFrame) {})
	if f := n.SetTracks(media.Tracks{Audio: media.PCMA}); !f.Silent {
		t.Errorf("missing ffmpeg should fall back to silence: %+v", f)
	}
}

func TestSilencePacedOnVideo(t *testing.T) {
	for name, tr := range map[string]media.Tracks{"no track": {}, "disabled": {Audio: media.AAC, AudioConfig: []byte{0x12, 0x88}}} {
		var s sink
		n := New(Config{Enabled: name == "no track"}, s.add)
		f := n.SetTracks(tr)
		if !f.Silent || f.Rate != 32000 || !bytes.Equal(f.Config, []byte{0x12, 0x88}) {
			t.Fatalf("%s: format %+v", name, f)
		}
		n.Start(10 * time.Second)
		for i := 0; i < 50; i++ { // 2 s of 25 fps
			n.OnVideo(10*time.Second + time.Duration(i)*40*time.Millisecond)
		}
		// 1960 ms of video -> frames at 0, 32, ..., 1952 ms: 62.
		if s.n() != 62 {
			t.Errorf("%s: %d frames", name, s.n())
		}
		for i, fr := range s.fs {
			if want := 10*time.Second + time.Duration(i)*32*time.Millisecond; fr.PTS != want {
				t.Fatalf("frame %d PTS %v want %v", i, fr.PTS, want)
			}
		}
	}
}

func TestADTSReader(t *testing.T) {
	// two frames: 7-byte headers, payload sizes 3 and 2 (no CRC)
	hdr := func(n int) []byte {
		l := n + 7
		return []byte{0xFF, 0xF1, 0x54, byte(0x40 | l>>11), byte(l >> 3), byte(l<<5) | 0x1F, 0xFC}
	}
	b := append(append(append(hdr(3), 1, 2, 3), hdr(2)...), 4, 5)
	r := &adtsReader{r: bytes.NewReader(b)}
	for _, want := range [][]byte{{1, 2, 3}, {4, 5}} {
		got, err := r.next()
		if err != nil || !bytes.Equal(got, want) {
			t.Fatalf("got %v %v want %v", got, err, want)
		}
	}
	if _, err := r.next(); err == nil {
		t.Error("no EOF")
	}
}

func TestOpusSamples(t *testing.T) {
	for _, c := range []struct {
		p    []byte
		want int
	}{{[]byte{0xF8}, 960}, {[]byte{0x00}, 480}, {[]byte{0x79}, 2 * 960}, {[]byte{0xFB, 3}, 3 * 960}} {
		if got := opusSamples(c.p); got != c.want {
			t.Errorf("% x: %d want %d", c.p, got, c.want)
		}
	}
}

func needFFmpeg(t *testing.T) string {
	p, err := exec.LookPath("ffmpeg")
	if err != nil {
		t.Skip("no ffmpeg")
	}
	return p
}

func collect(t *testing.T, s *sink, min int) {
	t.Helper()
	for end := time.Now().Add(10 * time.Second); s.n() < min && time.Now().Before(end); {
		time.Sleep(20 * time.Millisecond)
	}
	if s.n() < min {
		t.Fatalf("only %d frames", s.n())
	}
}

func TestG711Transcode(t *testing.T) {
	ff := needFFmpeg(t)
	var s sink
	n := New(Config{Enabled: true, TranscodeG711: true, FFmpegPath: ff}, s.add)
	defer n.Close()
	f := n.SetTracks(media.Tracks{Audio: media.PCMU})
	if f.Codec != media.AAC || f.Silent || f.Rate != 32000 {
		t.Fatalf("format %+v", f)
	}
	n.Start(5 * time.Second)
	for i := 0; i < 100; i++ { // 2 s of 160-byte (20 ms) mu-law silence, 0xFF
		n.OnAudio(media.AudioFrame{PTS: 5*time.Second + time.Duration(i)*20*time.Millisecond, Data: bytes.Repeat([]byte{0xFF}, 160)})
	}
	collect(t, &s, 30)
	if s.fs[0].PTS != 5*time.Second || s.fs[1].PTS-s.fs[0].PTS != 32*time.Millisecond {
		t.Errorf("PTS %v %v", s.fs[0].PTS, s.fs[1].PTS)
	}
}

func TestOpusTranscode(t *testing.T) {
	ff := needFFmpeg(t)
	var s sink
	n := New(Config{Enabled: true, FFmpegPath: ff}, s.add)
	defer n.Close()
	n.SetTracks(media.Tracks{Audio: media.Opus, AudioRate: 48000, AudioChannels: 2})
	n.Start(0)
	for i := 0; i < 100; i++ { // 2 s of 20 ms CELT DTX silence packets
		n.OnAudio(media.AudioFrame{PTS: time.Duration(i) * 20 * time.Millisecond, Data: []byte{0xFC, 0xFF, 0xFE}})
	}
	collect(t, &s, 30)
}

// The embedded silent frame must decode as AAC in the ASC we declare.
func TestSilentFrameDecodes(t *testing.T) {
	ff := needFFmpeg(t)
	probe, err := exec.LookPath("ffprobe")
	if err != nil {
		t.Skip("no ffprobe")
	}
	var adts bytes.Buffer
	for i := 0; i < 20; i++ {
		l := len(silentFrame) + 7
		adts.Write([]byte{0xFF, 0xF1, 0x54, byte(0x40 | l>>11), byte(l >> 3), byte(l<<5) | 0x1F, 0xFC})
		adts.Write(silentFrame)
	}
	file := filepath.Join(t.TempDir(), "s.aac")
	os.WriteFile(file, adts.Bytes(), 0o644)
	out, err := exec.Command(ff, "-v", "error", "-i", file, "-f", "null", "-").CombinedOutput()
	if err != nil || len(bytes.TrimSpace(out)) != 0 {
		t.Errorf("decode: %v %s", err, out)
	}
	out, _ = exec.Command(probe, "-v", "error", "-show_entries", "stream=sample_rate,channels", "-of", "csv=p=0", file).Output()
	if string(bytes.TrimSpace(out)) != "32000,1" {
		t.Errorf("probe %q", out)
	}
}

// opusSink collects Opus packets.
func (s *sink) opus() func(media.AudioFrame) { return s.add }

// G.711 in: Opus packets out, on a 20 ms grid anchored to the input's PTS even though the
// input arrives in one clump.
func TestG711ToOpus(t *testing.T) {
	ff := needFFmpeg(t)
	if !opusEncoderAvailable(ff) {
		t.Skip("ffmpeg has no libopus")
	}
	var aac, op sink
	n := New(Config{Enabled: true, TranscodeG711: true, FFmpegPath: ff, OpusOut: op.opus()}, aac.add)
	defer n.Close()
	n.SetTracks(media.Tracks{Audio: media.PCMU})
	n.Start(5 * time.Second)
	for i := 0; i < 100; i++ { // 2 s, all fed at once
		n.OnAudio(media.AudioFrame{PTS: 5*time.Second + time.Duration(i)*20*time.Millisecond, Data: bytes.Repeat([]byte{0xFF}, 160)})
	}
	collect(t, &op, 50)
	collect(t, &aac, 30)
	op.mu.Lock()
	defer op.mu.Unlock()
	if op.fs[0].PTS != 5*time.Second {
		t.Errorf("first opus PTS %v", op.fs[0].PTS)
	}
	for i := 1; i < len(op.fs); i++ {
		if d := op.fs[i].PTS - op.fs[i-1].PTS; d != 20*time.Millisecond {
			t.Fatalf("opus %d step %v", i, d)
		}
	}
	if opusSamples(op.fs[0].Data) != 960 {
		t.Errorf("packet % x is not 20 ms", op.fs[0].Data)
	}
}

// Camera AAC passes through untouched and also feeds an Opus-only ffmpeg (ADTS headers added).
func TestAACPassthroughAlsoOpus(t *testing.T) {
	ff := needFFmpeg(t)
	if !opusEncoderAvailable(ff) {
		t.Skip("ffmpeg has no libopus")
	}
	var aac, op sink
	n := New(Config{Enabled: true, FFmpegPath: ff, OpusOut: op.opus()}, aac.add)
	defer n.Close()
	n.SetTracks(media.Tracks{Audio: media.AAC, AudioRate: 32000, AudioChannels: 1, AudioConfig: AACConfig})
	n.Start(0)
	for i := 0; i < 100; i++ {
		n.OnAudio(media.AudioFrame{PTS: time.Duration(i) * 32 * time.Millisecond, Data: silentFrame})
	}
	if aac.n() != 100 {
		t.Errorf("passthrough %d frames", aac.n())
	}
	collect(t, &op, 50)
	op.mu.Lock()
	defer op.mu.Unlock()
	for i := 1; i < len(op.fs); i++ {
		if d := op.fs[i].PTS - op.fs[i-1].PTS; d != 20*time.Millisecond {
			t.Fatalf("opus %d step %v", i, d)
		}
	}
}

func TestSilentOpusPacedOnVideo(t *testing.T) {
	var aac, op sink
	n := New(Config{Enabled: false, OpusOut: op.opus()}, aac.add)
	n.SetTracks(media.Tracks{})
	n.Start(time.Second)
	for i := 0; i < 50; i++ {
		n.OnVideo(time.Second + time.Duration(i)*40*time.Millisecond)
	}
	if op.n() != 99 || opusSamples(op.fs[0].Data) != 960 { // 0..1960 ms, every 20 ms
		t.Errorf("%d silent opus packets", op.n())
	}
}

func TestOggPacketsAndADTS(t *testing.T) {
	o := &oggOpus{chans: 1, rate: 48000, serial: 1}
	big := bytes.Repeat([]byte{0xF8}, 600) // spans several 255-byte lacing values
	stream := append(o.headers(), o.packet([]byte{0xF8, 1})...)
	stream = append(stream, o.packet(big)...)
	r := &oggPackets{r: bytes.NewReader(stream)}
	for _, want := range [][]byte{{0xF8, 1}, big} {
		got, err := r.next()
		if err != nil || !bytes.Equal(got, want) {
			t.Fatalf("got %d bytes, %v", len(got), err)
		}
	}
	if _, err := r.next(); err == nil {
		t.Error("no EOF")
	}
	if h, ok := adtsHeader(AACConfig, 4); !ok || h != [7]byte{0xFF, 0xF1, 0x54, 0x40, 0x01, 0x7F, 0xFC} {
		t.Errorf("adts % x", h)
	}
	if ASCRate(AACConfig) != 32000 {
		t.Error("ASCRate")
	}
}
