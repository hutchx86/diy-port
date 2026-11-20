// SPDX-License-Identifier: AGPL-3.0-or-later

package flv

import (
	"bytes"
	"encoding/binary"
	"encoding/hex"
	"io"
	"math"
	"net"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"diyport/go-restreamer/internal/media"
)

func TestAMFMapSortedAndDoubles(t *testing.T) {
	got := hex.EncodeToString(AppendAMF(nil, Map{"b": 2, "a": Map{"z": true, "y": uint32(1)}, "B": "x", "c": float32(0.5)}))
	// Byte-wise order: "B" (0x42) before "a" (0x61), "b", "c".
	want := "03" +
		"000142" + "02000178" +
		"000161" + "03" + "000179" + "003ff0000000000000" + "00017a" + "0101" + "000009" +
		"000162" + "004000000000000000" +
		"000163" + "003fe0000000000000" +
		"000009"
	if got != want {
		t.Errorf("got  %s\nwant %s", got, want)
	}
}

func TestAIPortHeaderFlags(t *testing.T) {
	if got := aiportHeader(true); !bytes.Equal(got, []byte{'F', 'L', 'V', 1, 0x07, 0, 0, 0, 9, 0, 0, 0, 0}) {
		t.Errorf("with audio % x", got)
	}
	if got := aiportHeader(false); got[4] != 0x03 {
		t.Errorf("without audio flags %#x", got[4])
	}
}

func TestTrailerMath(t *testing.T) {
	// Uptime-sized ns would overflow ns*90000 in uint64 (1e16 * 9e4 = 9e20).
	ns := int64(10_000_000_000_000_000) // ~115 days
	if got := ticksOf(ns, 90000); got != 900_000_000_000 {
		t.Errorf("ticksOf %d", got)
	}
	if got := ticksOf(1_500_000_000, 90000); got != 135000 {
		t.Errorf("1.5 s = %d ticks", got)
	}
	if got := audioTicks(1_000_000_000, 24000); got != 24000 {
		t.Errorf("audio 24k ticks %d", got)
	}
	if got := msOf(135001, 90000); got != 1500 {
		t.Errorf("msOf %d", got)
	}
	// trailer ms - tag ts == base for any scale.
	const base = 5_000_000
	for _, scale := range []uint32{90000, 48000, 24000, 8000} {
		ticks := ticksOf(5_000_123_456_789/1000*1000, scale)
		b := aiportTag(tagVideo, []byte{1, 2}, msOf(ticks, scale)-base, scale, 7, ticks)
		tr := b[len(b)-16:]
		if binary.BigEndian.Uint32(tr) != scale || binary.BigEndian.Uint32(tr[4:]) != 7 ||
			binary.BigEndian.Uint64(tr[8:]) != ticks {
			t.Errorf("scale %d trailer % x", scale, tr)
		}
		ts := int64(b[7])<<24 | int64(b[4])<<16 | int64(b[5])<<8 | int64(b[6])
		if got := msOf(binary.BigEndian.Uint64(tr[8:]), scale) - ts; got != base {
			t.Errorf("scale %d trailer_ms - ts = %d", scale, got)
		}
	}
}

func TestAudioConfigBytes(t *testing.T) {
	// 19 bytes, spec 4.4: 08 00 00 04 | ts 0 | 00 00 00 | AF 00 ASC | 00 00 00 0F (then our trailer).
	b := aiportTag(tagAudio, aacConfigPayload(aacASC48Mono), 0, 48000, 0, 0)
	if want := "0800000400000000000000" + "af001188" + "0000000f"; hex.EncodeToString(b[:19]) != want {
		t.Errorf("aac config % x", b[:19])
	}
	// The firmware's ASC arithmetic on the encoder's ADTS header (AAC-LC, 48 kHz, mono).
	h := []byte{0xFF, 0xF1, 1<<6 | 3<<2 | 0, 1 << 6, 0, 0, 0}
	if asc := ascFromADTS(h); !bytes.Equal(asc[:], aacASC48Mono) {
		t.Errorf("asc % x", asc)
	}
	h = []byte{0xFF, 0xF1, 1<<6 | 8<<2 | 0, 1 << 6, 0, 0, 0} // 16 kHz mono: 14 08
	if asc := ascFromADTS(h); asc != [2]byte{0x14, 0x08} {
		t.Errorf("16k asc % x", asc)
	}
	for rate, tag := range map[int]byte{48000: 0, 24000: 1, 16000: 2, 12000: 3, 8000: 4, 44100: 0, 0: 0} {
		b := aiportTag(tagOpus, opusConfigPayload(rate), 0, 48000, 0, 0)
		if want := "0a000004000000000000" + "00" + "cf0003" + hex.EncodeToString([]byte{tag}) + "0000000f"; hex.EncodeToString(b[:19]) != want {
			t.Errorf("opus config rate %d: % x", rate, b[:19])
		}
	}
	if got := aacDataPayload([]byte{9, 8}); !bytes.Equal(got, []byte{0xAF, 1, 9, 8}) {
		t.Errorf("aac data % x", got)
	}
}

func TestVideoPayloadBytes(t *testing.T) {
	cases := []struct {
		codec       byte
		key, config bool
		cts         uint32
		want        string
	}{
		{codecIDAVC, true, true, 0, "1700000000"},
		{codecIDAVC, true, false, 0, "1701000000"},
		{codecIDAVC, false, false, 0x010203, "2701010203"},
		{codecIDHEVC, true, true, 0, "1800000000"},
		{codecIDHEVC, false, false, 0, "2801000000"},
	}
	for _, c := range cases {
		if got := hex.EncodeToString(aiportVideoPayload(c.codec, c.key, c.config, c.cts, nil)); got != c.want {
			t.Errorf("%+v: %s", c, got)
		}
	}
}

func TestMpmaAndClockSyncKeys(t *testing.T) {
	b := AppendAMF(nil, aiportMpma(uniformMpma(2_000_000)))
	keys := amfKeys(b[1:])
	if want := []string{"cs", "m", "r", "sp", "t"}; !equalStrings(keys, want) {
		t.Errorf("onMpma keys %v", keys)
	}
	// The nested cs object: cur, max, min, each 2 Mbps.
	if !bytes.Contains(b, AppendAMF(nil, Map{"cur": 2000000.0, "max": 2000000.0, "min": 2000000.0})) {
		t.Errorf("cs object missing: % x", b)
	}
	if got := amfKeys(AppendAMF(nil, aiportClockSync(1, 2, 3))[1:]); !equalStrings(got, []string{"streamClock", "streamClockBase", "wallClock"}) {
		t.Errorf("onClockSync keys %v", got)
	}
	if got := amfKeys(AppendAMF(nil, aiportRtt(0, "abcd", -1))[1:]); !equalStrings(got, []string{"encDelay", "id", "rttMillis"}) {
		t.Errorf("onRtt keys %v", got)
	}
	// onRtt's invalid rtt is -1.0 = BF F0 00 ...
	if !bytes.Contains(AppendAMF(nil, aiportRtt(0, "abcd", -1)), []byte{0x00, 0xBF, 0xF0, 0, 0, 0, 0, 0, 0}) {
		t.Error("rttMillis -1 not BF F0")
	}
	if rttMillis(1e9, 0, 5) != -1 || rttMillis(2_000_000_000, 1000, 500) != 1500 {
		t.Errorf("rttMillis %v %v", rttMillis(1e9, 0, 5), rttMillis(2_000_000_000, 1000, 500))
	}
}

func TestParseFeedback(t *testing.T) {
	f1 := []byte{0, 4, 0, 16, 'T', 'O', 'K', 'N', 0, 0, 3, 0xE8, 0, 0, 0, 0, 0, 0, 1, 0xF4}
	f2 := []byte{0, 5, 0, 2, 0, 7}
	f3 := []byte{0, 3, 0, 0}
	all := append(append(append([]byte(nil), f1...), f2...), f3...)
	fr, rest := parseFeedback(all[:len(f1)+3]) // one whole frame and a cut one
	if len(fr) != 1 || fr[0].typ != 4 || len(fr[0].payload) != 16 || len(rest) != 3 {
		t.Fatalf("partial: %+v rest %d", fr, len(rest))
	}
	fr, rest = parseFeedback(append(rest, all[len(f1)+3:]...))
	if len(fr) != 2 || fr[0].typ != 5 || fr[1].typ != 3 || len(rest) != 0 {
		t.Fatalf("rest: %+v rest %d", fr, len(rest))
	}
}

// amfKeys lists an AMF0 object's top-level keys from the byte after the 0x03 marker. It knows
// number, bool, string and nested object values, which is all the AI Port objects hold.
func amfKeys(b []byte) []string {
	var keys []string
	var skip func(b []byte) []byte
	skip = func(b []byte) []byte {
		switch b[0] {
		case 0:
			return b[9:]
		case 1:
			return b[2:]
		case 2:
			return b[3+int(binary.BigEndian.Uint16(b[1:])):]
		case 3:
			b = b[1:]
			for !(b[0] == 0 && b[1] == 0 && b[2] == 9) {
				n := int(binary.BigEndian.Uint16(b))
				b = skip(b[2+n:])
			}
			return b[3:]
		}
		panic("amf type")
	}
	for !(b[0] == 0 && b[1] == 0 && b[2] == 9) {
		n := int(binary.BigEndian.Uint16(b))
		keys = append(keys, string(b[2:2+n]))
		b = skip(b[2+n:])
	}
	return keys
}

func equalStrings(a, b []string) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}

// ---- pusher against a loopback console

type aiTag struct {
	typ     byte
	ts      int64
	payload []byte
	scale   uint32
	cts     uint32
	ticks   uint64
}

func (g aiTag) name() string {
	if g.typ != tagScript {
		return ""
	}
	return scriptName(g.payload)
}

// parseAI splits an extendedFlv stream: the 13-byte header, then tags each followed by
// PreviousTagSize and the 16-byte trailer. A cut-off last tag is ignored.
func parseAI(t *testing.T, b []byte) (flags byte, tags []aiTag) {
	t.Helper()
	if len(b) < 13 || string(b[:3]) != "FLV" {
		t.Fatalf("bad header % x", b[:min(13, len(b))])
	}
	flags, b = b[4], b[13:]
	for len(b) >= 11 {
		n := int(b[1])<<16 | int(b[2])<<8 | int(b[3])
		if len(b) < 11+n+4+16 {
			break
		}
		if got := binary.BigEndian.Uint32(b[11+n:]); int(got) != n+11 {
			t.Fatalf("PreviousTagSize %d want %d", got, n+11)
		}
		tr := b[11+n+4:]
		tags = append(tags, aiTag{b[0], int64(b[7])<<24 | int64(b[4])<<16 | int64(b[5])<<8 | int64(b[6]), b[11 : 11+n],
			binary.BigEndian.Uint32(tr), binary.BigEndian.Uint32(tr[4:]), binary.BigEndian.Uint64(tr[8:])})
		b = b[11+n+20:]
	}
	return flags, tags
}

type console struct {
	ln    net.Listener
	mu    sync.Mutex
	conns []net.Conn
	data  []*bytes.Buffer
}

func newConsole(t *testing.T) *console {
	t.Helper()
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	c := &console{ln: ln}
	t.Cleanup(func() { ln.Close() })
	go func() {
		for {
			conn, err := ln.Accept()
			if err != nil {
				return
			}
			c.mu.Lock()
			buf := &bytes.Buffer{}
			c.conns, c.data = append(c.conns, conn), append(c.data, buf)
			c.mu.Unlock()
			go func() {
				b := make([]byte, 8192)
				for {
					n, err := conn.Read(b)
					c.mu.Lock()
					buf.Write(b[:n])
					c.mu.Unlock()
					if err != nil {
						return
					}
				}
			}()
		}
	}()
	return c
}

func (c *console) addr() string { return c.ln.Addr().String() }

// wait polls the i-th connection's parsed stream until ok.
func (c *console) wait(t *testing.T, i int, ok func(flags byte, tags []aiTag) bool) (byte, []aiTag) {
	t.Helper()
	var flags byte
	var tags []aiTag
	for end := time.Now().Add(5 * time.Second); time.Now().Before(end); time.Sleep(5 * time.Millisecond) {
		c.mu.Lock()
		var b []byte
		if i < len(c.data) {
			b = append(b, c.data[i].Bytes()...)
		}
		c.mu.Unlock()
		if len(b) >= 13 {
			if flags, tags = parseAI(t, b); ok(flags, tags) {
				return flags, tags
			}
		}
	}
	t.Fatalf("conn %d: condition not met; tags %v", i, names(tags))
	return 0, nil
}

func (c *console) send(i int, b []byte) {
	c.mu.Lock()
	conn := c.conns[i]
	c.mu.Unlock()
	conn.Write(b)
}

func names(tags []aiTag) []string {
	var out []string
	for _, g := range tags {
		switch {
		case g.typ == tagScript:
			out = append(out, g.name())
		case g.typ == tagVideo && g.payload[1] == 0:
			out = append(out, "vcfg")
		case g.typ == tagVideo && g.payload[0]>>4 == 1:
			out = append(out, "key")
		case g.typ == tagVideo:
			out = append(out, "inter")
		case g.typ == tagAudio && g.payload[1] == 0:
			out = append(out, "aaccfg")
		case g.typ == tagAudio:
			out = append(out, "aac")
		case g.typ == tagOpus && len(g.payload) == 4 && g.payload[0] == 0xCF:
			out = append(out, "opuscfg")
		default:
			out = append(out, "opus")
		}
	}
	return out
}

func eqNames(t *testing.T, got []aiTag, want ...string) {
	t.Helper()
	n := names(got)
	if len(n) < len(want) || !equalStrings(n[:len(want)], want) {
		t.Fatalf("tags %v, want prefix %v", n, want)
	}
}

type testClock struct{ mono atomic.Int64 } // Mono() in ns

func (c *testClock) Mono() time.Duration     { return time.Duration(c.mono.Load()) }
func (c *testClock) advance(d time.Duration) { c.mono.Add(int64(d)) }
func (c *testClock) Now() time.Time {
	return time.Unix(1_700_000_000, 0).Add(c.Mono() - 5000*time.Second)
}

func aiTestTracks() media.Tracks { return testTracks() }

func newAI(t *testing.T, addr string, clk *testClock, mod func(*AIPortConfig)) *AIPortPusher {
	t.Helper()
	cfg := AIPortConfig{Addr: addr, StreamName: "video1", Channel: "video1", BitrateBps: 2_000_000, FPS: 25,
		RetryInterval: 20 * time.Millisecond, Now: clk.Now, Mono: clk.Mono, AudioHold: 20 * time.Millisecond}
	if mod != nil {
		mod(&cfg)
	}
	p := NewAIPortPusher(cfg)
	t.Cleanup(p.Close)
	p.OnTracks(aiTestTracks())
	return p
}

func idr(pts time.Duration) media.VideoAU {
	return media.VideoAU{PTS: pts, IDR: true, NALUs: [][]byte{{0x65, 1, 2}}}
}
func pframe(pts time.Duration) media.VideoAU {
	return media.VideoAU{PTS: pts, NALUs: [][]byte{{0x41, 3}}}
}

// frameAt feeds an AU whose PTS is pts and advances the fake clock to mono = 5000 s + pts - 10 s,
// so the first frame at pts 10 s sits at monotonic 5000 s and base 5,000,000 ms.
func feed(clk *testClock, p *AIPortPusher, au media.VideoAU) {
	clk.mono.Store(int64(5000*time.Second + au.PTS - 10*time.Second))
	p.OnVideo(au)
}

func checkTrailers(t *testing.T, tags []aiTag, base int64) {
	t.Helper()
	for i, g := range tags {
		if g.typ == tagScript && g.name() == "onRtt" {
			continue
		}
		if got := msOf(g.ticks, g.scale) - g.ts; got != base {
			t.Errorf("tag %d (%v): trailer ms - ts = %d, want base %d", i, names(tags[i:i+1]), got, base)
		}
	}
}

func TestAIPortFirstConnectSequenceAndThrottle(t *testing.T) {
	con := newConsole(t)
	clk := &testClock{}
	p := newAI(t, con.addr(), clk, nil)
	// Non-key frames before the first key frame are skipped.
	feed(clk, p, pframe(9*time.Second))
	feed(clk, p, idr(10*time.Second))
	_, tags := con.wait(t, 0, func(_ byte, tg []aiTag) bool { return len(tg) >= 5 })
	// Header + onMetaData, onMpma, onClockSync, the AVC config, the key frame (spec 4.6).
	eqNames(t, tags, "onMetaData", "onMpma", "onClockSync", "vcfg", "key")
	flags, _ := con.wait(t, 0, func(byte, []aiTag) bool { return true })
	if flags != 0x03 {
		t.Errorf("no audio: flags %#x, want 0x03", flags)
	}
	const base = 5_000_000
	if tags[0].ts != 0 || tags[4].ts != 0 || tags[4].scale != 90000 || tags[4].ticks != 450_000_000 || tags[4].cts != 0 {
		t.Errorf("first frame ts/trailer: %+v", tags[4])
	}
	if tags[3].payload[0] != 0x17 || tags[3].payload[1] != 0 || !bytes.HasSuffix(tags[3].payload, []byte{0x68, 0xEE, 0x3C}) {
		t.Errorf("config tag % x", tags[3].payload)
	}
	// onClockSync {streamClock 0, streamClockBase base, wallClock}.
	sync := tags[2].payload
	if !bytes.Contains(sync, append([]byte("\x00\x0fstreamClockBase\x00"), f64(base)...)) ||
		!bytes.Contains(sync, append([]byte("\x00\x0bstreamClock\x00"), f64(0)...)) ||
		!bytes.Contains(sync, append([]byte("\x00\x09wallClock\x00"), f64(float64(clk.Now().UnixMilli()))...)) {
		t.Errorf("onClockSync % x", sync)
	}
	// Non-key frames: no onMpma within 500 ms / 10 s; one after 10 s idle.
	feed(clk, p, pframe(10*time.Second+40*time.Millisecond))
	feed(clk, p, pframe(10*time.Second+80*time.Millisecond))
	feed(clk, p, pframe(10*time.Second+11*time.Second))
	_, tags = con.wait(t, 0, func(_ byte, tg []aiTag) bool { return len(tg) >= 9 })
	eqNames(t, tags, "onMetaData", "onMpma", "onClockSync", "vcfg", "key", "inter", "inter", "onMpma", "inter")
	if tags[5].ts != 40 || tags[6].ts != 80 {
		t.Errorf("inter ts %d %d", tags[5].ts, tags[6].ts)
	}
	// A changed bitrate sends within 500 ms of the last one only after 500 ms.
	p.SetBitrate(3_000_000)
	feed(clk, p, pframe(21*time.Second+100*time.Millisecond))
	feed(clk, p, pframe(21*time.Second+700*time.Millisecond))
	_, tags = con.wait(t, 0, func(_ byte, tg []aiTag) bool { return len(tg) >= 12 })
	eqNames(t, tags[8:], "inter", "inter", "onMpma", "inter")
	// Every later key frame: onMpma, onClockSync, key (no config: SPS/PPS unchanged).
	feed(clk, p, idr(22*time.Second))
	_, tags = con.wait(t, 0, func(_ byte, tg []aiTag) bool { return len(tg) >= 15 })
	eqNames(t, tags[11:], "inter", "onMpma", "onClockSync", "key")
	checkTrailers(t, tags, base)
	for _, g := range tags {
		if g.typ == tagScript && g.scale != 90000 {
			t.Errorf("script tag timescale %d after video", g.scale)
		}
	}
}

func f64(v float64) []byte { return binary.BigEndian.AppendUint64(nil, math.Float64bits(v)) }

func TestAIPortMetadataKeys(t *testing.T) {
	con := newConsole(t)
	clk := &testClock{}
	p := newAI(t, con.addr(), clk, func(c *AIPortConfig) { c.Channel, c.StreamName = "video3", "cam_2" })
	feed(clk, p, idr(10*time.Second))
	_, tags := con.wait(t, 0, func(_ byte, tg []aiTag) bool { return len(tg) >= 1 })
	pl := tags[0].payload
	obj := pl[3+len("onMetaData"):]
	want := []string{"audioBandwidth", "audioChannels", "audioFrequency", "channelId", "extendedFormat", "hasAudio",
		"hasVideo", "streamId", "streamName", "videoBandwidth", "videoFps", "videoHeight", "videoWidth"}
	if got := amfKeys(obj[1:]); !equalStrings(got, want) {
		t.Fatalf("keys %v", got)
	}
	for _, want := range [][]byte{
		append([]byte("\x00\x09channelId\x00"), f64(2)...), append([]byte("\x00\x08streamId\x00"), f64(4)...),
		[]byte("\x00\x08hasVideo\x01\x01"), []byte("\x00\x08hasAudio\x01\x00"),
		append([]byte("\x00\x0eaudioBandwidth\x00"), f64(0)...),
		append([]byte("\x00\x0evideoBandwidth\x00"), f64(2000000)...), append([]byte("\x00\x08videoFps\x00"), f64(25)...),
		append([]byte("\x00\x0avideoWidth\x00"), f64(640)...), append([]byte("\x00\x0bvideoHeight\x00"), f64(360)...),
		[]byte("\x00\x0avideoWidth"), []byte("\x00\x0astreamName\x02\x00\x05cam_2"),
	} {
		if !bytes.Contains(obj, want) {
			t.Errorf("metadata lacks % x", want)
		}
	}
}

// withAudio turns on the audio and Opus tracks without an ffmpeg, so tests feed frames to onAAC/onOpus.
func withAudio(p *AIPortPusher) {
	p.mu.Lock()
	p.audioOn, p.opusOn = true, true
	p.mu.Unlock()
}

func TestAIPortAudioOrderAndTimescales(t *testing.T) {
	con := newConsole(t)
	clk := &testClock{}
	p := newAI(t, con.addr(), clk, func(c *AIPortConfig) { c.Opus, c.OpusRate = true, 24000 })
	withAudio(p)
	// Audio before the video base exists is dropped.
	p.onAAC(media.AudioFrame{PTS: 9 * time.Second, Data: []byte{1}})
	feed(clk, p, idr(10*time.Second))
	flags, tags := con.wait(t, 0, func(_ byte, tg []aiTag) bool { return len(tg) >= 7 })
	if flags != 0x07 {
		t.Errorf("flags %#x", flags)
	}
	// First connect: key frame first, then the AAC config, then the Opus config (4.6).
	eqNames(t, tags, "onMetaData", "onMpma", "onClockSync", "vcfg", "key", "aaccfg", "opuscfg")
	if !bytes.Equal(tags[5].payload, []byte{0xAF, 0, 0x11, 0x88}) || !bytes.Equal(tags[6].payload, []byte{0xCF, 0, 3, 1}) {
		t.Errorf("configs % x | % x", tags[5].payload, tags[6].payload)
	}
	if tags[5].scale != 48000 || tags[6].scale != 24000 || tags[5].ts != 0 {
		t.Errorf("config trailers %+v %+v", tags[5], tags[6])
	}
	// AAC and Opus frames come out in ts order whatever order they arrive in.
	p.onAAC(media.AudioFrame{PTS: 10*time.Second + 10*time.Millisecond, Data: []byte{6}})
	p.onAAC(media.AudioFrame{PTS: 10*time.Second + 21*time.Millisecond, Data: []byte{7, 7}})
	p.onOpus(media.AudioFrame{PTS: 10*time.Second + 20*time.Millisecond, Data: []byte{0xF8, 1}})
	p.onOpus(media.AudioFrame{PTS: 10*time.Second + 40*time.Millisecond, Data: []byte{0xF8, 2}})
	p.onAAC(media.AudioFrame{PTS: 10*time.Second + 43*time.Millisecond, Data: []byte{8}})
	_, tags = con.wait(t, 0, func(_ byte, tg []aiTag) bool { return len(tg) >= 11 })
	var ts []int64
	for _, g := range tags[7:] {
		ts = append(ts, g.ts)
	}
	if len(ts) < 4 || ts[0] != 10 || ts[1] != 20 || ts[2] != 21 || ts[3] != 40 {
		t.Errorf("audio ts %v", ts)
	}
	if g := tags[8]; g.typ != tagOpus || g.scale != 24000 || !bytes.Equal(g.payload, []byte{0xF8, 1}) {
		t.Errorf("opus tag %+v", g)
	}
	if g := tags[7]; g.typ != tagAudio || g.scale != 48000 || !bytes.Equal(g.payload, []byte{0xAF, 1, 6}) {
		t.Errorf("aac tag %+v", g)
	}
	// Script tags after audio carry the audio timescale when audio holds the latest pts.
	clk.advance(0)
	feed(clk, p, pframe(10*time.Second+1))
	_, tags = con.wait(t, 0, func(_ byte, tg []aiTag) bool { return len(names(tg)) >= 12 })
	checkTrailers(t, tags, 5_000_000)
	if p.audioDrops.Load() != 0 {
		t.Errorf("audio drops %d", p.audioDrops.Load())
	}
}

func TestAIPortReconnectSequence(t *testing.T) {
	con := newConsole(t)
	clk := &testClock{}
	p := newAI(t, con.addr(), clk, func(c *AIPortConfig) { c.Opus = true })
	withAudio(p)
	feed(clk, p, idr(10*time.Second))
	feed(clk, p, pframe(10*time.Second+40*time.Millisecond))
	con.wait(t, 0, func(_ byte, tg []aiTag) bool { return len(tg) >= 8 })
	// Drop the connection from the console side.
	con.mu.Lock()
	con.conns[0].Close()
	con.mu.Unlock()
	time.Sleep(100 * time.Millisecond) // the pusher notices and redials
	// Frames while down; the first connection's key frame must not be repeated.
	feed(clk, p, pframe(10*time.Second+80*time.Millisecond))
	feed(clk, p, pframe(10*time.Second+120*time.Millisecond))
	flags, tags := con.wait(t, 1, func(_ byte, tg []aiTag) bool { return len(tg) >= 4 })
	if flags != 0x07 {
		t.Errorf("flags %#x", flags)
	}
	// Header + onMetaData, video config, AAC config, Opus config; non-key frames stay out.
	eqNames(t, tags, "onMetaData", "vcfg", "aaccfg", "opuscfg")
	feed(clk, p, pframe(10*time.Second+160*time.Millisecond))
	feed(clk, p, idr(10*time.Second+200*time.Millisecond))
	_, tags = con.wait(t, 1, func(_ byte, tg []aiTag) bool { return len(tg) >= 7 })
	eqNames(t, tags, "onMetaData", "vcfg", "aaccfg", "opuscfg", "onMpma", "onClockSync", "key")
	// The base is kept: the key frame is at ts 200, not 0, and the sync carries the old base.
	if tags[6].ts != 200 {
		t.Errorf("key frame ts after reconnect %d, want 200 (base kept)", tags[6].ts)
	}
	if !bytes.Contains(tags[5].payload, append([]byte("\x00\x0fstreamClockBase\x00"), f64(5_000_000)...)) {
		t.Errorf("sync base % x", tags[5].payload)
	}
	checkTrailers(t, tags, 5_000_000)
	if st := p.Stats(); st.Reconnects != 1 || !st.Connected {
		t.Errorf("stats %+v", st)
	}
}

func TestAIPortConfigTagOnlyWhenSPSChanges(t *testing.T) {
	con := newConsole(t)
	clk := &testClock{}
	p := newAI(t, con.addr(), clk, nil)
	feed(clk, p, idr(10*time.Second))
	feed(clk, p, idr(11*time.Second))
	tr := aiTestTracks()
	tr.SPS = []byte{0x67, 0x4D, 0x40, 0x1F, 0xBB}
	p.OnTracks(tr)
	feed(clk, p, idr(12*time.Second))
	feed(clk, p, idr(13*time.Second))
	_, tags := con.wait(t, 0, func(_ byte, tg []aiTag) bool { return len(tg) >= 15 })
	eqNames(t, tags, "onMetaData", "onMpma", "onClockSync", "vcfg", "key",
		"onMpma", "onClockSync", "key", // unchanged: no config
		"onMpma", "onClockSync", "vcfg", "key", // new SPS: config before the key frame
		"onMpma", "onClockSync", "key")
}

func TestAIPortBackwardDtsAndTimelineReset(t *testing.T) {
	con := newConsole(t)
	clk := &testClock{}
	p := newAI(t, con.addr(), clk, nil)
	feed(clk, p, idr(10*time.Second))
	feed(clk, p, pframe(10*time.Second+80*time.Millisecond))
	// A frame whose clock steps back is skipped (feed() sets mono from PTS: use the pusher directly).
	clk.mono.Store(int64(5000*time.Second + 50*time.Millisecond))
	p.OnVideo(pframe(10*time.Second + 40*time.Millisecond)) // anchor fixed: ns = 5000.04 s, behind 5000.08
	p.OnVideo(pframe(10*time.Second + 120*time.Millisecond))
	// A key frame far behind resets the timeline: a new base.
	p.OnVideo(idr(7 * time.Second))
	_, tags := con.wait(t, 0, func(_ byte, tg []aiTag) bool { return len(names(tg)) >= 10 })
	eqNames(t, tags, "onMetaData", "onMpma", "onClockSync", "vcfg", "key", "inter", "inter", "onMpma", "onClockSync", "key")
	if tags[6].ts != 120 || tags[9].ts != 0 {
		t.Errorf("ts %d %d (want 120 and a reset to 0)", tags[6].ts, tags[9].ts)
	}
	if !bytes.Contains(tags[8].payload, append([]byte("\x00\x0fstreamClockBase\x00"), f64(4_997_000)...)) {
		t.Errorf("reset base % x", tags[8].payload)
	}
}

func TestAIPortOnRttReplyAndFeedbackTolerance(t *testing.T) {
	con := newConsole(t)
	clk := &testClock{}
	p := newAI(t, con.addr(), clk, nil)
	feed(clk, p, idr(10*time.Second))
	con.wait(t, 0, func(_ byte, tg []aiTag) bool { return len(tg) >= 5 })
	clk.mono.Store(int64(5002 * time.Second))
	var fb []byte
	frame := func(typ uint16, payload ...byte) {
		fb = binary.BigEndian.AppendUint16(fb, typ)
		fb = binary.BigEndian.AppendUint16(fb, uint16(len(payload)))
		fb = append(fb, payload...)
	}
	frame(9, 1, 2, 3)       // invalid: ignored
	frame(0, 0, 0, 0, 1, 1) // bandwidth hint: ignored
	frame(5, 0, 7)          // event notification: logged
	frame(3)                // stop flag: logged
	ts := binary.BigEndian.AppendUint64(nil, 5001*1000)
	frame(4, append(append([]byte("AB\x00\x09"), 0, 0, 3, 0xE8), ts...)...) // token, 1000 Hz, ts 5001 s
	frame(4, append([]byte("XYZW"), make([]byte, 12)...)...)                // timescale 0: rtt -1
	// Split across two writes: the partial frame stays buffered.
	con.send(0, fb[:len(fb)-5])
	time.Sleep(20 * time.Millisecond)
	con.send(0, fb[len(fb)-5:])
	_, tags := con.wait(t, 0, func(_ byte, tg []aiTag) bool {
		n := 0
		for _, g := range tg {
			if g.name() == "onRtt" {
				n++
			}
		}
		return n >= 2
	})
	var rtts []aiTag
	for _, g := range tags {
		if g.name() == "onRtt" {
			rtts = append(rtts, g)
		}
	}
	if !bytes.Contains(rtts[0].payload, append([]byte("\x00\x02id\x02\x00\x04"), "AB\x00\x09"...)) ||
		!bytes.Contains(rtts[0].payload, append([]byte("\x00\x09rttMillis\x00"), f64(1000)...)) { // 5002 s - 5001 s
		t.Errorf("onRtt % x", rtts[0].payload)
	}
	if !bytes.Contains(rtts[1].payload, append([]byte("\x00\x09rttMillis\x00"), 0xBF, 0xF0, 0, 0, 0, 0, 0, 0)) {
		t.Errorf("invalid onRtt % x", rtts[1].payload)
	}
	if names(rtts[:1])[0] != "onRtt" || rtts[0].typ != tagScript {
		t.Error("not a script tag")
	}
	// The connection survived all of it.
	if !p.Stats().Connected {
		t.Error("connection dropped")
	}
}

func TestAIPortQueueDropsFramesAndKeepsGoing(t *testing.T) {
	clk := &testClock{}
	p := NewAIPortPusher(AIPortConfig{Addr: "127.0.0.1:1", StreamName: "video1", Channel: "video1", QueueBytes: 1000,
		Now: clk.Now, Mono: clk.Mono, RetryInterval: time.Hour})
	p.Close() // no writer: the queue only fills
	p.OnTracks(aiTestTracks())
	big := media.VideoAU{PTS: 10 * time.Second, IDR: true, NALUs: [][]byte{append([]byte{0x65}, make([]byte, 300)...)}}
	feed(clk, p, big)
	n0 := len(p.q)
	for i := 1; i <= 6; i++ {
		au := big
		au.IDR, au.NALUs[0][0] = false, 0x41
		au.PTS += time.Duration(i) * 40 * time.Millisecond
		feed(clk, p, au)
	}
	if p.Drops() == 0 || p.qBytes > 1000 {
		t.Errorf("drops %d queued %d", p.Drops(), p.qBytes)
	}
	// A small frame still fits afterwards: dropping is per frame, nothing is torn down.
	before := len(p.q)
	p.mu.Lock()
	p.qBytes = 0
	p.q = nil
	p.mu.Unlock()
	feed(clk, p, pframe(11*time.Second))
	if n0 != 1 || before == 0 || len(p.q) != 1 {
		t.Errorf("queue lengths %d %d %d", n0, before, len(p.q))
	}
}

func TestAIPortConfigDefaults(t *testing.T) {
	p := NewAIPortPusher(AIPortConfig{Addr: "127.0.0.1:1", Channel: "video2", RetryInterval: time.Hour, DialTimeout: 0})
	defer p.Close()
	if p.cfg.QueueBytes != 5<<20 || p.cfg.TOS != 184 || p.cfg.DialTimeout != time.Second {
		t.Errorf("defaults %+v", p.cfg)
	}
	q := NewAIPortPusher(AIPortConfig{Addr: "127.0.0.1:1", RetryInterval: time.Hour, DialTimeout: 5 * time.Minute})
	defer q.Close()
	if q.cfg.DialTimeout != time.Minute {
		t.Errorf("connectTimeout not clamped to 60 s: %v", q.cfg.DialTimeout)
	}
}

func TestAIPortKeepsDrainingConsole(t *testing.T) {
	// A console that writes a lot and nothing we understand must not stall the push.
	con := newConsole(t)
	clk := &testClock{}
	p := newAI(t, con.addr(), clk, nil)
	feed(clk, p, idr(10*time.Second))
	con.wait(t, 0, func(_ byte, tg []aiTag) bool { return len(tg) >= 5 })
	con.send(0, bytes.Repeat([]byte{0, 1, 0, 0}, 1000))
	feed(clk, p, pframe(10*time.Second+40*time.Millisecond))
	con.wait(t, 0, func(_ byte, tg []aiTag) bool { return len(tg) >= 6 })
	_ = io.Discard
}

// Feedback type 3 (stop flag) ends the stream: after the console drops the connection the pusher
// must not redial. Live 2026-11-12 21:51: stale streamNames after a re-pair got a stop on every
// reconnect and we redialled once a second.
func TestAIPortStopFeedbackEndsReconnects(t *testing.T) {
	con := newConsole(t)
	clk := &testClock{}
	p := newAI(t, con.addr(), clk, nil)
	feed(clk, p, idr(10*time.Second))
	con.wait(t, 0, func(_ byte, tg []aiTag) bool { return len(tg) >= 5 })
	con.send(0, []byte{0, 3, 0, 0}) // type 3, no payload
	time.Sleep(50 * time.Millisecond)
	con.mu.Lock()
	con.conns[0].Close()
	con.mu.Unlock()
	time.Sleep(200 * time.Millisecond) // ten retry intervals
	feed(clk, p, idr(11*time.Second))
	time.Sleep(100 * time.Millisecond)
	con.mu.Lock()
	n := len(con.conns)
	con.mu.Unlock()
	if n != 1 {
		t.Errorf("redialled after a stop feedback: %d connections", n)
	}
}

// ascForRate matches the encoder's ADTS-derived ASC at 48 kHz and Protect's native 8 kHz track.
func TestAIPortASCForRate(t *testing.T) {
	for rate, want := range map[int][]byte{48000: {0x11, 0x88}, 16000: {0x14, 0x08}, 8000: {0x15, 0x88}} {
		if got := ascForRate(rate); !bytes.Equal(got, want) {
			t.Errorf("%d: % x, want % x", rate, got, want)
		}
	}
}

// AudioCounter smooths per-frame pts jitter onto n*1024-sample steps and re-anchors on a jump.
func TestAIPortAudioCounter(t *testing.T) {
	p := &AIPortPusher{cfg: AIPortConfig{AACRate: 8000, AudioCounter: true}}
	dur := 128 * time.Millisecond
	base := 10 * time.Second
	for i, jitter := range []time.Duration{0, 300 * time.Microsecond, -400 * time.Microsecond, 0} {
		if got := p.countedPTS(false, base+time.Duration(i)*dur+jitter); got != base+time.Duration(i)*dur {
			t.Errorf("frame %d: %v", i, got)
		}
	}
	if got := p.countedPTS(false, 20*time.Second); got != 20*time.Second {
		t.Errorf("no re-anchor after a jump: %v", got)
	}
}
