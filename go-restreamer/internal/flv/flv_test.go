// SPDX-License-Identifier: AGPL-3.0-or-later

package flv

import (
	"sync"
	"bytes"
	"encoding/binary"
	"encoding/hex"
	"io"
	"log/slog"
	"math"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"testing"
	"time"

	"diyport/go-restreamer/internal/media"
)

func TestAMF0(t *testing.T) {
	cases := []struct {
		name string
		v    any
		want string
	}{
		{"number", 1.0, "003ff0000000000000"},
		{"int", 7, "00401c000000000000"},
		{"true", true, "0101"},
		{"string", "ab", "02000261" + "62"},
		{"object", Object{{"a", 0}}, "03" + "000161" + "000000000000000000" + "000009"},
		{"ecma", ECMAArray{{"s", "x"}}, "08" + "00000001" + "000173" + "02000178" + "000009"},
	}
	for _, c := range cases {
		if got := hex.EncodeToString(AppendAMF(nil, c.v)); got != c.want {
			t.Errorf("%s: %s want %s", c.name, got, c.want)
		}
	}
	got := ScriptPayload("onMetaData", Object{})
	if !bytes.HasPrefix(got, append([]byte{0x02, 0, 10}, "onMetaData"...)) {
		t.Errorf("script payload prefix % x", got[:8])
	}
}

func TestAnnexBToAVCC(t *testing.T) {
	au := []byte{0, 0, 0, 1, 0x09, 0xF0, 0, 0, 1, 0x67, 1, 2, 3, 0, 0, 0, 1, 0x65, 9, 9, 0, 0, 1, 0x41, 7}
	nalus := SplitAnnexB(au)
	if len(nalus) != 4 || !bytes.Equal(nalus[1], []byte{0x67, 1, 2, 3}) || !bytes.Equal(nalus[2], []byte{0x65, 9, 9}) {
		t.Fatalf("split: %x", nalus)
	}
	want := []byte{0, 0, 0, 4, 0x67, 1, 2, 3, 0, 0, 0, 3, 0x65, 9, 9, 0, 0, 0, 2, 0x41, 7} // AUD dropped
	if got := AnnexBToAVCC(au); !bytes.Equal(got, want) {
		t.Errorf("avcc % x want % x", got, want)
	}
}

func TestAVCDecoderConfigurationRecord(t *testing.T) {
	sps := []byte{0x67, 0x4D, 0x40, 0x1F, 0xAA}
	pps := []byte{0x68, 0xEE, 0x3C}
	got, err := AVCDecoderConfigurationRecord(sps, pps)
	if err != nil {
		t.Fatal(err)
	}
	want := []byte{1, 0x4D, 0x40, 0x1F, 0xFF, 0xE1, 0, 5, 0x67, 0x4D, 0x40, 0x1F, 0xAA, 1, 0, 3, 0x68, 0xEE, 0x3C}
	if !bytes.Equal(got, want) {
		t.Errorf("main profile: % x", got)
	}
	sps[1] = 100
	got, _ = AVCDecoderConfigurationRecord(sps, pps)
	if !bytes.HasSuffix(got, []byte{0xFD, 0xF8, 0xF8, 0}) {
		t.Errorf("high profile lacks extension: % x", got)
	}
	if _, err := AVCDecoderConfigurationRecord(nil, pps); err == nil {
		t.Error("empty SPS accepted")
	}
}

func TestTrailerAndTag(t *testing.T) {
	b := appendTrailer(nil, timescaleVideo, 2*time.Second)
	if len(b) != 16 || b[1] != 0x01 || b[2] != 0x5F || b[3] != 0x90 ||
		binary.BigEndian.Uint64(b[8:]) != 180000 {
		t.Errorf("video trailer % x", b)
	}
	b = appendTrailer(nil, timescaleOther, time.Second)
	if b[1] != 0 || b[2] != 0x2B || b[3] != 0x11 || binary.BigEndian.Uint64(b[8:]) != 11025 {
		t.Errorf("other trailer % x", b)
	}
	tag := appendTag(nil, tagVideo, []byte{1, 2, 3}, 0x01020304)
	want := []byte{9, 0, 0, 3, 2, 3, 4, 1, 0, 0, 0, 1, 2, 3, 0, 0, 0, 14}
	if !bytes.Equal(tag, want) {
		t.Errorf("tag % x want % x", tag, want)
	}
}

// --- a parser for what the Pusher writes -------------------------------------------------

type tagRec struct {
	typ     byte
	ts      int64
	payload []byte
	trailer []byte
}

func parseStream(t *testing.T, b []byte) []tagRec {
	t.Helper()
	if len(b) < 13 || string(b[:3]) != "FLV" || b[4] != 7 {
		t.Fatalf("bad header % x", b[:min(13, len(b))])
	}
	b = b[13:]
	var out []tagRec
	for len(b) >= 11 {
		n := int(b[1])<<16 | int(b[2])<<8 | int(b[3])
		if len(b) < 11+n+4+16 {
			break // cut-off last tag
		}
		ts := int64(b[7])<<24 | int64(b[4])<<16 | int64(b[5])<<8 | int64(b[6])
		if got := binary.BigEndian.Uint32(b[11+n:]); int(got) != n+11 {
			t.Fatalf("PreviousTagSize %d want %d", got, n+11)
		}
		out = append(out, tagRec{b[0], ts, b[11 : 11+n], b[11+n+4 : 11+n+20]})
		b = b[11+n+20:]
	}
	return out
}

func scriptName(p []byte) string { return string(p[3 : 3+int(binary.BigEndian.Uint16(p[1:]))]) }

func testTracks() media.Tracks {
	return media.Tracks{Video: media.H264, SPS: []byte{0x67, 0x4D, 0x40, 0x1F, 0xAA}, PPS: []byte{0x68, 0xEE, 0x3C},
		Width: 640, Height: 360, FPS: 25}
}

// capture runs a Pusher against a local listener, feeds feed's AUs and returns the bytes
// the listener got.
func capture(t *testing.T, cfg Config, tr media.Tracks, feed func(p *Pusher)) []byte {
	t.Helper()
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer ln.Close()
	got := make(chan []byte, 1)
	go func() {
		c, err := ln.Accept()
		if err != nil {
			got <- nil
			return
		}
		b, _ := io.ReadAll(c)
		got <- b
	}()
	cfg.Addr = ln.Addr().String()
	p := NewPusher(cfg)
	p.OnTracks(tr)
	feed(p)
	deadline := time.Now().Add(3 * time.Second)
	for p.Stats().LastFrameTime.IsZero() && time.Now().Before(deadline) {
		time.Sleep(10 * time.Millisecond)
	}
	time.Sleep(100 * time.Millisecond)
	st := p.Stats()
	p.Close()
	b := <-got
	if st.BytesSent == 0 || st.Reconnects != 0 {
		t.Errorf("stats %+v", st)
	}
	return b
}

func TestPusherTagOrderAndTimestamps(t *testing.T) {
	base := 100 * time.Second
	b := capture(t, Config{StreamName: "video1", AudioEnabled: true}, testTracks(), func(p *Pusher) {
		// P frames before the first IDR are dropped; PTS wobbles backwards once to check the clamp.
		p.OnVideo(media.VideoAU{PTS: base, NALUs: [][]byte{{0x41, 1}}})
		for i := 0; i < 60; i++ {
			pts := base + time.Duration(i)*40*time.Millisecond
			if i == 30 {
				pts -= 100 * time.Millisecond
			}
			p.OnVideo(media.VideoAU{PTS: pts + 7*time.Second, NALUs: [][]byte{{0x65, byte(i)}}, IDR: i%25 == 0})
		}
	})
	tags := parseStream(t, b)
	if len(tags) < 8 {
		t.Fatalf("only %d tags", len(tags))
	}
	var names []string
	for _, tg := range tags[:5] {
		if tg.typ == tagScript {
			names = append(names, scriptName(tg.payload))
		} else {
			names = append(names, map[byte]string{tagVideo: "video", tagAudio: "audio"}[tg.typ])
		}
	}
	want := []string{"onMetaData", "onMpma", "onClockSync", "video", "audio"}
	for i := range want {
		if names[i] != want[i] {
			t.Fatalf("order %v want %v", names, want)
		}
	}
	if !bytes.Contains(tags[0].payload, []byte("streamName")) || !bytes.Contains(tags[0].payload, []byte("video1")) {
		t.Error("metadata lacks streamName")
	}
	if !bytes.Equal(tags[3].payload[:5], []byte{0x17, 0, 0, 0, 0}) || !bytes.Equal(tags[4].payload, []byte{0xAF, 0, 0x12, 0x88}) {
		t.Errorf("sequence headers: % x / % x", tags[3].payload, tags[4].payload)
	}
	// Every tag has a trailer with a known timescale; per-track timestamps never decrease and
	// start at 0 from the first IDR; video has one frame per AU after it, silence is paced.
	var lastV, lastA int64 = -1, -1
	var video, aacFrames int
	for _, tg := range tags[3:] {
		wantScale := []byte{0x00, 0x2B, 0x11} // script tags keep 11025
		switch tg.typ {
		case tagVideo:
			wantScale = []byte{0x01, 0x5F, 0x90}
		case tagAudio:
			wantScale = []byte{0x00, 0x7D, 0x00} // AAC: its own 32 kHz rate
		}
		if tg.trailer[0] != 0 || !bytes.Equal(tg.trailer[1:4], wantScale) {
			t.Errorf("trailer % x for type %d", tg.trailer, tg.typ)
		}
		switch tg.typ {
		case tagVideo:
			if tg.ts < lastV {
				t.Errorf("video ts went back: %d after %d", tg.ts, lastV)
			}
			if lastV < 0 && tg.ts != 0 {
				t.Errorf("first frame ts %d", tg.ts)
			}
			lastV = tg.ts
			if tg.payload[1] == 1 {
				video++
			}
		case tagAudio:
			if tg.ts < lastA {
				t.Errorf("audio ts went back: %d after %d", tg.ts, lastA)
			}
			lastA = tg.ts
			aacFrames++
		}
	}
	if video != 60 || aacFrames < 60 {
		t.Errorf("video %d audio %d", video, aacFrames)
	}
	if tags[3].ts != 0 || math.Abs(float64(lastV)-2360) > 1 {
		t.Errorf("video span: last %d", lastV)
	}
}

func TestPusherReconnects(t *testing.T) {
	ln, _ := net.Listen("tcp", "127.0.0.1:0")
	defer ln.Close()
	conns := make(chan int, 4)
	go func() {
		for n := 1; ; n++ {
			c, err := ln.Accept()
			if err != nil {
				return
			}
			conns <- n
			if n == 1 {
				c.Close() // Protect drops the first connection
				continue
			}
			go io.Copy(io.Discard, c)
		}
	}()
	p := NewPusher(Config{Addr: ln.Addr().String(), StreamName: "v", RetryInterval: 20 * time.Millisecond})
	defer p.Close()
	p.OnTracks(testTracks())
	deadline := time.Now().Add(5 * time.Second)
	for i := 0; time.Now().Before(deadline) && p.Stats().Reconnects == 0; i++ {
		p.OnVideo(media.VideoAU{PTS: time.Duration(i) * 40 * time.Millisecond, NALUs: [][]byte{{0x65, 1}}, IDR: true})
		time.Sleep(40 * time.Millisecond)
	}
	if p.Stats().Reconnects == 0 {
		t.Fatal("never reconnected")
	}
}

func TestFFprobe(t *testing.T) {
	ffmpeg, err1 := exec.LookPath("ffmpeg")
	ffprobe, err2 := exec.LookPath("ffprobe")
	if err1 != nil || err2 != nil {
		t.Skip("ffmpeg/ffprobe not installed")
	}
	dir := t.TempDir()
	h264 := filepath.Join(dir, "in.h264")
	if out, err := exec.Command(ffmpeg, "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=s=320x240:r=25",
		"-t", "2", "-c:v", "libx264", "-bf", "0", "-g", "25", "-bsf:v", "h264_mp4toannexb", "-f", "h264", h264).CombinedOutput(); err != nil {
		t.Skipf("no libx264: %v %s", err, out)
	}
	raw, _ := os.ReadFile(h264)
	// Group NAL units into access units: a new AU starts at each slice with first_mb_in_slice 0
	// (top bit of the first slice byte after the NAL header set) or SPS.
	var aus [][][]byte
	var cur [][]byte
	var sps, pps []byte
	for _, n := range SplitAnnexB(raw) {
		switch n[0] & 0x1F {
		case 7:
			sps = n
		case 8:
			pps = n
		}
		if (n[0]&0x1F == 1 || n[0]&0x1F == 5) && n[1]&0x80 != 0 && len(cur) > 0 && hasSlice(cur) {
			aus = append(aus, cur)
			cur = nil
		}
		cur = append(cur, n)
	}
	aus = append(aus, cur)
	tr := media.Tracks{Video: media.H264, SPS: sps, PPS: pps, Width: 320, Height: 240, FPS: 25}
	b := capture(t, Config{StreamName: "video1", AudioEnabled: false}, tr, func(p *Pusher) {
		for i, au := range aus {
			idr := false
			for _, n := range au {
				idr = idr || n[0]&0x1F == 5
			}
			p.OnVideo(media.VideoAU{PTS: time.Duration(i) * 40 * time.Millisecond, NALUs: au, IDR: idr})
		}
	})
	// ffprobe's FLV demuxer doesn't know the 16-byte trailers; strip them for the check.
	var plain bytes.Buffer
	plain.Write(b[:13])
	rest := b[13:]
	for len(rest) >= 11 {
		n := int(rest[1])<<16 | int(rest[2])<<8 | int(rest[3])
		if len(rest) < 11+n+20 {
			break
		}
		plain.Write(rest[:11+n+4])
		rest = rest[11+n+20:]
	}
	plain.Bytes()[4] = 5
	file := filepath.Join(dir, "out.flv")
	os.WriteFile(file, plain.Bytes(), 0o644)
	out, err := exec.Command(ffprobe, "-v", "error", "-f", "flv", "-count_frames", "-show_entries",
		"stream=codec_name,width,height,nb_read_frames", "-of", "csv=p=0", file).CombinedOutput()
	if err != nil {
		t.Fatalf("ffprobe: %v\n%s", err, out)
	}
	t.Logf("ffprobe: %s", bytes.ReplaceAll(out, []byte("\n"), []byte(" | ")))
	if !bytes.Contains(out, []byte("h264,320,240")) || !bytes.Contains(out, []byte("aac")) {
		t.Errorf("ffprobe output: %s", out)
	}
}

func hasSlice(nalus [][]byte) bool {
	for _, n := range nalus {
		if t := n[0] & 0x1F; t == 1 || t == 5 {
			return true
		}
	}
	return false
}

func TestRetryDelay(t *testing.T) {
	s := time.Second
	for failed, want := range map[int]time.Duration{0: s, 1: s, 3: s, 4: 2 * s, 5: 4 * s, 20: 30 * s} {
		if got := retryDelay(s, 30*s, failed); got != want {
			t.Errorf("failed=%d: %v want %v", failed, got, want)
		}
	}
}

// Pace: frames fed in one clump still go out stamped on their own clock, trailer elapsed =
// Pace + ts, which is what un-stuttered Protect's audio over Wi-Fi (live 2026-11-12).
func TestPusherPacesClumpedFrames(t *testing.T) {
	const pace = 150 * time.Millisecond
	b := capture(t, Config{StreamName: "video1", Pace: pace}, testTracks(), func(p *Pusher) {
		for i := 0; i < 10; i++ { // all at once, 40 ms apart in PTS
			p.OnVideo(media.VideoAU{PTS: time.Duration(i) * 40 * time.Millisecond,
				NALUs: [][]byte{{0x65, byte(i)}}, IDR: i == 0})
		}
		time.Sleep(pace + 500*time.Millisecond)
	})
	var n int
	for _, tg := range parseStream(t, b) {
		if tg.typ != tagVideo || tg.payload[1] == 0 { // skip the AVC sequence header
			continue
		}
		scale := uint64(tg.trailer[1])<<16 | uint64(tg.trailer[2])<<8 | uint64(tg.trailer[3])
		elapsed := time.Duration(binary.BigEndian.Uint64(tg.trailer[8:]) * uint64(time.Second) / scale)
		want := pace + time.Duration(tg.ts)*time.Millisecond
		if d := elapsed - want; d < -2*time.Millisecond || d > 2*time.Millisecond {
			t.Errorf("tag ts %d: trailer elapsed %v want %v", tg.ts, elapsed, want)
		}
		n++
	}
	if n < 5 {
		t.Fatalf("only %d paced video tags", n)
	}
}

// A frame later than Pace re-bases with the whole Pace restored ahead of it, so the frames
// after it are on time again rather than at zero buffer.
func TestPusherRebaseRestoresPace(t *testing.T) {
	const pace = 100 * time.Millisecond
	b := capture(t, Config{StreamName: "video1", Pace: pace}, testTracks(), func(p *Pusher) {
		p.OnVideo(media.VideoAU{PTS: 0, NALUs: [][]byte{{0x65, 0}}, IDR: true})
		time.Sleep(pace + 300*time.Millisecond) // the next frame is 260 ms past its slot
		for i := 1; i <= 3; i++ {
			p.OnVideo(media.VideoAU{PTS: time.Duration(i) * 40 * time.Millisecond, NALUs: [][]byte{{0x65, byte(i)}}})
		}
		time.Sleep(3 * pace)
	})
	var offs []time.Duration
	for _, tg := range parseStream(t, b) {
		if tg.typ == tagVideo && tg.payload[1] != 0 {
			scale := uint64(tg.trailer[1])<<16 | uint64(tg.trailer[2])<<8 | uint64(tg.trailer[3])
			el := time.Duration(binary.BigEndian.Uint64(tg.trailer[8:]) * uint64(time.Second) / scale)
			offs = append(offs, el-time.Duration(tg.ts)*time.Millisecond)
		}
	}
	if len(offs) != 4 {
		t.Fatalf("got %d frames", len(offs))
	}
	// Frames 1..3 share one offset (smooth after the jump), about pace + 360 ms.
	if offs[1] != offs[2] || offs[2] != offs[3] || offs[1] < pace+300*time.Millisecond {
		t.Errorf("offsets %v", offs)
	}
}

// Opus (type 10): config tag cf 00 03 02 after the AAC config, raw packets as data, trailers
// at 48000 for Opus and at the AAC rate (32000) for AAC, and one audio ms sequence across both
// tracks, with Opus on an exact 20 ms grid even when its PTS arrive in a clump.
func TestPusherOpusTags(t *testing.T) {
	var opusIn func(media.AudioFrame)
	b := capture(t, Config{StreamName: "video1", AudioEnabled: true, Opus: true}, testTracks(), func(p *Pusher) {
		opusIn = p.onOpusOut
		p.OnVideo(media.VideoAU{PTS: 0, NALUs: [][]byte{{0x65, 0}}, IDR: true})
		for i := 1; i < 40; i++ {
			// 6 packets of a clump share one PTS (an AAC window's worth); the rest are late.
			pts := time.Duration(i/6*120) * time.Millisecond
			opusIn(media.AudioFrame{PTS: pts, Data: []byte{0xF8, 0xFF, byte(i)}})
			p.OnVideo(media.VideoAU{PTS: time.Duration(i) * 40 * time.Millisecond, NALUs: [][]byte{{0x41, byte(i)}}})
		}
	})
	tags := parseStream(t, b)
	var cfgAt = -1
	var aacCfgAt = -1
	for i, tg := range tags {
		if tg.typ == tagOpus && cfgAt < 0 {
			cfgAt = i
			if !bytes.Equal(tg.payload, []byte{0xCF, 0x00, 0x03, 0x02}) || tg.ts != 0 {
				t.Errorf("opus config % x ts %d", tg.payload, tg.ts)
			}
		}
		if tg.typ == tagAudio && aacCfgAt < 0 {
			aacCfgAt = i
		}
	}
	if aacCfgAt < 0 || cfgAt != aacCfgAt+1 {
		t.Fatalf("opus config at %d, aac config at %d", cfgAt, aacCfgAt)
	}
	var lastAudio int64 = -1
	var lastOpus int64 = -1
	var n int
	for _, tg := range tags[cfgAt+1:] {
		switch tg.typ {
		case tagOpus:
			if !bytes.Equal(tg.trailer[1:4], []byte{0x00, 0xBB, 0x80}) {
				t.Errorf("opus trailer % x", tg.trailer)
			}
			if n > 0 {
				if tg.payload[0] != 0xF8 || tg.ts-lastOpus < 20 {
					t.Fatalf("opus ts %d after %d", tg.ts, lastOpus)
				}
				if tg.ts-lastOpus != 20 && tg.ts < lastOpus+60 {
					t.Errorf("opus step %d", tg.ts-lastOpus)
				}
			}
			lastOpus = tg.ts
			n++
			fallthrough
		case tagAudio:
			if tg.typ == tagAudio && !bytes.Equal(tg.trailer[1:4], []byte{0x00, 0x7D, 0x00}) {
				t.Errorf("aac trailer % x", tg.trailer)
			}
			if tg.ts < lastAudio {
				t.Fatalf("audio ms went back across tracks: %d after %d", tg.ts, lastAudio)
			}
			lastAudio = tg.ts
		}
	}
	if n < 30 {
		t.Fatalf("only %d opus tags", n)
	}
}

// Directly: Opus clumped PTS come out exactly 20 ms apart; AAC (32 ms) and Opus frames
// delivered out of order keep exact spacing per track and merge non-decreasing.
func TestOpusGridAndAudioReorder(t *testing.T) {
	// No run loop: the queue is read here.
	p := &Pusher{log: slog.Default(), q: make(chan item, 1000), base: time.Second, lastO: -opusStepMS,
		hiA: -1, hiO: -1, cfg: Config{Opus: true}}
	ms := func(n int) time.Duration { return time.Second + time.Duration(n)*time.Millisecond }
	// AAC runs ahead in bursts of 4, Opus lags and clumps its PTS.
	for k := 0; k < 8; k++ {
		for j := 0; j < 4; j++ {
			p.onAudioOut(media.AudioFrame{PTS: ms((k*4 + j) * 32), Data: []byte{1}})
		}
		for j := 0; j < 6; j++ {
			n := k*6 + j
			p.onOpusOut(media.AudioFrame{PTS: ms(n / 6 * 120), Data: []byte{0xF8}})
		}
	}
	p.mu.Lock()
	p.hiA, p.hiO = 1<<40, 1<<40 // flush the rest
	p.flushAudioLocked()
	p.mu.Unlock()
	var last, lastA, lastO int64 = -1, -1, -1
	var na, no int
	for len(p.q) > 0 {
		it := <-p.q
		if it.ts < last {
			t.Fatalf("merged audio went back: %d after %d", it.ts, last)
		}
		last = it.ts
		if it.typ == tagAudio {
			if na > 0 && it.ts-lastA != 32 {
				t.Fatalf("aac step %d", it.ts-lastA)
			}
			lastA, na = it.ts, na+1
		} else {
			if no > 0 && it.ts-lastO != 20 {
				t.Fatalf("opus step %d", it.ts-lastO)
			}
			lastO, no = it.ts, no+1
		}
	}
	if na != 32 || no != 48 || p.audioDrops.Load() != 0 {
		t.Errorf("aac %d opus %d drops %d", na, no, p.audioDrops.Load())
	}
}

// The Opus trailer timescale is the rate Protect asked for (24000 live 2026-11-12), 48000 when
// none was given; the ms stamps stay a 20 ms grid at any rate (TestPusherOpusTags).
func TestOpusTrailerTimescaleFollowsRate(t *testing.T) {
	for rate, want := range map[uint32]uint32{0: 48000, 24000: 24000, 16000: 16000, 48000: 48000} {
		w := &writer{opusRate: rate}
		if got := w.scale(tagOpus); got != want {
			t.Errorf("opusRate %d: scale %d want %d", rate, got, want)
		}
	}
	w := &writer{opusRate: 24000}
	b := w.tag(w.start.Add(time.Second), tagOpus, []byte{0xF8, 0xFF, 0xFE}, 0)
	if tr := b[len(b)-16:]; !bytes.Equal(tr[1:4], []byte{0x00, 0x5D, 0xC0}) {
		t.Errorf("trailer % x, want timescale 0x005DC0", tr)
	}
}

// A pace re-base must not move the detection clock: OnSync reports the wall without the
// stall shift. Live 2026-11-15 11:24 on Driveway: a re-base (shift_ms=2013) moved the
// detection walls 2 s (push lag 0.1 s -> 2.1 s) and boxes no longer lined up with the car.
func TestPusherRebaseKeepsSyncWall(t *testing.T) {
	const pace = 100 * time.Millisecond
	var mu sync.Mutex
	var offs []time.Duration
	cfg := Config{StreamName: "video1", Pace: pace, OnSync: func(pts time.Duration, wall time.Time) {
		mu.Lock()
		offs = append(offs, time.Duration(wall.UnixNano())-pts)
		mu.Unlock()
	}}
	capture(t, cfg, testTracks(), func(p *Pusher) {
		p.OnVideo(media.VideoAU{PTS: 0, NALUs: [][]byte{{0x65, 0}}, IDR: true})
		time.Sleep(pace + 300*time.Millisecond) // the next frame is past its slot: re-base
		for i := 1; i <= 3; i++ {
			p.OnVideo(media.VideoAU{PTS: time.Duration(i) * 40 * time.Millisecond, NALUs: [][]byte{{0x65, byte(i)}}, IDR: true})
		}
		time.Sleep(3 * pace)
	})
	mu.Lock()
	defer mu.Unlock()
	if len(offs) < 2 {
		t.Fatalf("got %d syncs", len(offs))
	}
	for _, o := range offs[1:] {
		if d := o - offs[0]; d > 20*time.Millisecond || d < -20*time.Millisecond {
			t.Errorf("sync wall moved %v after the re-base (offsets %v)", d, offs)
		}
	}
}
