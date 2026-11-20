// SPDX-License-Identifier: AGPL-3.0-or-later

package manager

import (
	"diyport/go-restreamer/internal/audio"
	"net"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"
	"time"

	"diyport/go-restreamer/internal/detect"
	"diyport/go-restreamer/internal/flv"
	"diyport/go-restreamer/internal/rpc"
)

func fastTune(c *flv.Config) {
	c.RetryInterval = 100 * time.Millisecond
	c.MaxRetry = 200 * time.Millisecond
}

// pushedFLV waits until the sink has seen enough video and audio, then returns the plain FLV.
func pushedFLV(t *testing.T, s *sink, video, audio int) []byte {
	var plain []byte
	waitFor(t, "FLV with video and audio", 20*time.Second, func() bool {
		d, _ := s.snapshot()
		p, counts, ok := flvTags(d)
		if !ok || counts[9] < video || counts[8] < audio {
			return false
		}
		plain = p
		return true
	})
	return plain
}

// The whole path: a real RTSP server (H.264 + AAC) -> pool -> router (copy) -> Pusher -> a TCP
// listener standing in for Protect. What arrives, trailers stripped, is an FLV ffprobe reads as
// h264 + aac, and no log line carries the camera password.
func TestEndToEndH264AAC(t *testing.T) {
	vs := h264Source(t)
	afmt, aframes := aacFrames(t)
	url := serveRTSP(t, vs, afmt, aframes)
	dst := newSink(t)
	logs := &syncBuf{}
	m := New(Options{Hwaccel: "none", Log: testLog(logs), Tune: fastTune})
	defer m.Close()

	if _, err := m.Configure(rpc.ConfigureParams{DeviceID: "AABBCCDDEEFF", Mode: "copy", Channels: []rpc.Channel{{
		Channel: "video1", StreamName: "video1", RTSPURL: url, DestHost: dst.host(), DestPort: dst.port(), AudioEnabled: true}}}); err != nil {
		t.Fatal(err)
	}
	plain := pushedFLV(t, dst, 30, 20)
	if got := probeCodecs(t, plain); !slices.Contains(got, "h264") || !slices.Contains(got, "aac") {
		t.Fatalf("ffprobe codecs = %v", got)
	}
	if !strings.Contains(string(plain), "streamName") {
		t.Error("onMetaData has no streamName")
	}

	st := m.Stats().(rpc.Stats)
	ch := st.Cameras["AABBCCDDEEFF"].Channels[0]
	if ch.BytesSent == 0 || !ch.Connected || ch.LastFrameAgeMs == nil || *ch.LastFrameAgeMs > 5000 || ch.LastSentUnixMs == nil {
		t.Errorf("channel stats = %+v", ch)
	}
	if len(st.RTSP) != 1 || strings.Contains(st.RTSP[0].URL, "secret") || !st.RTSP[0].Connected {
		t.Errorf("rtsp stats = %+v", st.RTSP)
	}
	if strings.Contains(logs.String(), "secret") {
		t.Errorf("a log line leaked the password:\n%s", logs.String())
	}
	if _, err := m.Remove("AABBCCDDEEFF"); err != nil {
		t.Fatal(err)
	}
	if st := m.Stats().(rpc.Stats); len(st.Cameras) != 0 || len(st.RTSP) != 0 {
		t.Errorf("after remove: %+v", st)
	}
}

// video1 and video3 on the same camera URL share one RTSP session, and re-sending the same
// configuration changes nothing; a changed destination restarts only that channel's push.
func TestConfigureIsIdempotent(t *testing.T) {
	vs := h264Source(t)
	url := serveRTSP(t, vs, nil, nil)
	d1, d2 := newSink(t), newSink(t)
	m := New(Options{Hwaccel: "none", Log: testLog(&syncBuf{}), Tune: fastTune})
	defer m.Close()
	params := func(d3 *sink) rpc.ConfigureParams {
		return rpc.ConfigureParams{DeviceID: "CAM1", Mode: "copy", Channels: []rpc.Channel{
			{Channel: "video1", StreamName: "video1", RTSPURL: url, DestHost: d1.host(), DestPort: d1.port()},
			{Channel: "video3", StreamName: "video3", RTSPURL: url, DestHost: d3.host(), DestPort: d3.port()}}}
	}
	changed := func(res any) int { return res.(map[string]any)["changed"].(int) }
	res, _ := m.Configure(params(d1))
	if changed(res) != 2 {
		t.Fatalf("first configure changed %d", changed(res))
	}
	waitFor(t, "both pushes", 15*time.Second, func() bool {
		st := m.Stats().(rpc.Stats).Cameras["CAM1"].Channels
		return st[0].LastFrameAgeMs != nil && st[1].LastFrameAgeMs != nil
	})
	if st := m.Stats().(rpc.Stats); len(st.RTSP) != 1 || st.RTSP[0].Subscribers != 1 || st.RTSP[0].Sessions != 1 {
		t.Fatalf("rtsp = %+v (want one session, one router subscriber)", st.RTSP)
	}
	cam := m.cams["CAM1"]
	p1, p3 := cam.chans["video1"].pusher, cam.chans["video3"].pusher

	if res, _ = m.Configure(params(d1)); changed(res) != 0 {
		t.Fatalf("identical configure changed %d", changed(res))
	}
	if res, _ = m.Configure(params(d2)); changed(res) != 1 {
		t.Fatalf("destination change: changed %d", changed(res))
	}
	if cam.chans["video1"].pusher != p1 || cam.chans["video3"].pusher == p3 {
		t.Fatal("only video3's push should have been replaced")
	}
	waitFor(t, "video3 on its new destination", 15*time.Second, func() bool {
		_, n := d2.snapshot()
		d, _ := d2.snapshot()
		_, c, _ := flvTags(d)
		return n > 0 && c[9] > 5
	})
	if st := m.Stats().(rpc.Stats); st.RTSP[0].Sessions != 1 {
		t.Errorf("the camera session restarted: %+v", st.RTSP)
	}
	// Dropping a channel keeps the other's session.
	p := params(d1)
	p.Channels = p.Channels[:1]
	if res, _ = m.Configure(p); changed(res) != 1 {
		t.Fatalf("channel removal: changed %d", changed(res))
	}
	if st := m.Stats().(rpc.Stats); len(st.Cameras["CAM1"].Channels) != 1 || st.RTSP[0].Sessions != 1 {
		t.Errorf("after removal: %+v", st)
	}
}

// Shared decode: video1 is served on shared_decode.socket in the Python DetectServer's
// records, which shared_decode.read_detect_records reads back: bytes records (an FLV header,
// metadata, the AVC config) first, then one frame record per video tag with a wall time near
// now. The bytes, concatenated, are a plain FLV ffprobe reads.
func TestDetectServerProtocol(t *testing.T) {
	vs := h264Source(t)
	url := serveRTSP(t, vs, nil, nil)
	dst := newSink(t)
	sock := filepath.Join(os.TempDir(), "dtest-"+strings.ReplaceAll(t.Name(), "/", "_")+".sock")
	m := New(Options{Hwaccel: "none", Log: testLog(&syncBuf{}), Tune: fastTune})
	defer m.Close()
	if _, err := m.Configure(rpc.ConfigureParams{DeviceID: "CAM1", Channels: []rpc.Channel{{
		Channel: "video1", StreamName: "video1", RTSPURL: url, DestHost: dst.host(), DestPort: dst.port()}},
		SharedDecode: &rpc.SharedDecode{Enabled: true, DetectFPS: 10, Socket: sock}}); err != nil {
		t.Fatal(err)
	}
	defer os.Remove(sock)

	var c net.Conn
	waitFor(t, "detect socket", 5*time.Second, func() bool {
		var err error
		c, err = net.Dial("unix", sock)
		return err == nil
	})
	defer c.Close()
	c.SetReadDeadline(time.Now().Add(20 * time.Second))
	var flvBytes []byte
	frames := 0
	var lastWall float64
	for frames < 25 {
		head := make([]byte, 13)
		if _, err := readFull(c, head); err != nil {
			t.Fatalf("after %d frames: %v", frames, err)
		}
		kind, wall, n := detect.Unpack(head)
		body := make([]byte, n)
		if _, err := readFull(c, body); err != nil {
			t.Fatal(err)
		}
		switch kind {
		case detect.KindBytes:
			if frames > 0 {
				t.Fatal("bytes record after frames began")
			}
			flvBytes = append(flvBytes, body...)
		case detect.KindFrame:
			if body[0] != 9 {
				t.Fatalf("frame record is tag type %d", body[0])
			}
			if d := wall - float64(time.Now().UnixMilli()); d > 1000 || d < -10000 {
				t.Fatalf("wall time %v ms from now", d)
			}
			if wall < lastWall {
				t.Fatalf("wall time went back: %v then %v", lastWall, wall)
			}
			lastWall = wall
			flvBytes = append(flvBytes, body...)
			frames++
		}
	}
	if string(flvBytes[:3]) != "FLV" {
		t.Fatalf("stream starts %q", flvBytes[:3])
	}
	if got := probeCodecs(t, flvBytes); !slices.Equal(got, []string{"h264"}) {
		t.Fatalf("ffprobe codecs = %v", got)
	}
}

func readFull(c net.Conn, b []byte) (int, error) {
	n := 0
	for n < len(b) {
		k, err := c.Read(b[n:])
		n += k
		if err != nil {
			return n, err
		}
	}
	return n, nil
}

// video2 and video3 on one URL share one audio ffmpeg (CPU: 3 encoders per camera,
// 2026-11-12): one Encoder start for both pushers, removing one keeps the other's encoder
// up, removing both stops it.
func TestChannelsOnOneURLShareAudioEncoder(t *testing.T) {
	needTool(t, "ffmpeg")
	vs := h264Source(t)
	afmt, aframes := aacFrames(t)
	url := serveRTSP(t, vs, afmt, aframes)
	d2, d3 := newSink(t), newSink(t)
	m := New(Options{Hwaccel: "none", Opus: true, Log: testLog(&syncBuf{}), Tune: fastTune})
	defer m.Close()
	chans := []rpc.Channel{
		{Channel: "video2", StreamName: "video2", RTSPURL: url, DestHost: d2.host(), DestPort: d2.port(), AudioEnabled: true, Opus: true},
		{Channel: "video3", StreamName: "video3", RTSPURL: url, DestHost: d3.host(), DestPort: d3.port(), AudioEnabled: true, Opus: true}}
	if _, err := m.Configure(rpc.ConfigureParams{DeviceID: "CAM1", Mode: "copy", Channels: chans}); err != nil {
		t.Fatal(err)
	}
	enc := m.cams["CAM1"].encs[url]
	waitFor(t, "both pushes with audio", 20*time.Second, func() bool {
		for _, s := range []*sink{d2, d3} {
			d, _ := s.snapshot()
			if _, c, ok := flvTags(d); !ok || c[9] < 5 || c[8] < 5 {
				return false
			}
		}
		return true
	})
	if len(m.cams["CAM1"].encs) != 1 || enc.Subscribers() != 2 || enc.Starts() != 1 || !enc.Running() {
		t.Fatalf("encs=%d subs=%d starts=%d running=%v", len(m.cams["CAM1"].encs), enc.Subscribers(), enc.Starts(), enc.Running())
	}
	if _, err := m.Configure(rpc.ConfigureParams{DeviceID: "CAM1", Mode: "copy", Channels: chans[:1]}); err != nil {
		t.Fatal(err)
	}
	if enc.Subscribers() != 1 || !enc.Running() || enc.Starts() != 1 {
		t.Fatalf("after one removed: subs=%d starts=%d running=%v", enc.Subscribers(), enc.Starts(), enc.Running())
	}
	if _, err := m.Configure(rpc.ConfigureParams{DeviceID: "CAM1", Mode: "copy"}); err != nil {
		t.Fatal(err)
	}
	if enc.Running() || enc.Subscribers() != 0 || len(m.cams["CAM1"].encs) != 0 {
		t.Fatalf("after both removed: subs=%d running=%v", enc.Subscribers(), enc.Running())
	}
}

// Two pushers on one URL see the same latency under Pace, also when the second channel
// joins mid-stream (live 2026-11-12: video2 last_frame_age 800 ms, video3 30 ms). The late
// pusher got the shared Opus encoder's frames from before its first IDR; its Opus grid ran
// ahead of its video and Pace held every video tag behind a future Opus tag.
func TestSharedEncoderLateJoinerKeepsLatency(t *testing.T) {
	needTool(t, "ffmpeg")
	vs := h264Source(t)
	afmt, aframes := aacFrames(t)
	url := serveRTSP(t, vs, afmt, aframes)
	d2, d3 := newSink(t), newSink(t)
	m := New(Options{Hwaccel: "none", Opus: true, Pace: 2 * time.Second, Log: testLog(&syncBuf{}), Tune: fastTune})
	defer m.Close()
	chans := []rpc.Channel{
		{Channel: "video3", StreamName: "video3", RTSPURL: url, DestHost: d3.host(), DestPort: d3.port(), AudioEnabled: true, Opus: true},
		{Channel: "video2", StreamName: "video2", RTSPURL: url, DestHost: d2.host(), DestPort: d2.port(), AudioEnabled: true, Opus: true}}
	m.Configure(rpc.ConfigureParams{DeviceID: "CAM1", Mode: "copy", Channels: chans[:1]})
	time.Sleep(1300 * time.Millisecond)
	m.Configure(rpc.ConfigureParams{DeviceID: "CAM1", Mode: "copy", Channels: chans})
	time.Sleep(5 * time.Second)
	for i := 0; i < 8; i++ {
		time.Sleep(300 * time.Millisecond)
		var age [2]int64
		for _, c := range m.Stats().(rpc.Stats).Cameras["CAM1"].Channels {
			if c.LastFrameAgeMs == nil {
				t.Fatalf("%s has sent no video", c.Channel)
			}
			age[map[string]int{"video3": 0, "video2": 1}[c.Channel]] = *c.LastFrameAgeMs
		}
		if d := age[1] - age[0]; d > 500 || d < -500 {
			t.Fatalf("sample %d: video3 age %d ms, video2 age %d ms", i, age[0], age[1])
		}
	}
}

// Opus per channel: off unless Protect asked (withOpus), at its rate, 48000 when not given or
// unsupported; a change restarts that channel's push (rate and trailer are per connection).
func TestOpusRatePerChannel(t *testing.T) {
	m := New(Options{Hwaccel: "none", Opus: true, Log: testLog(&syncBuf{})})
	defer m.Close()
	cam := &camera{id: "CAM1"}
	for _, c := range []struct {
		ch   rpc.Channel
		want int
	}{
		{rpc.Channel{}, 0},
		{rpc.Channel{OpusSampleRate: 24000}, 0},
		{rpc.Channel{Opus: true}, 48000},
		{rpc.Channel{Opus: true, OpusSampleRate: 24000}, 24000},
		{rpc.Channel{Opus: true, OpusSampleRate: 16000}, 16000},
		{rpc.Channel{Opus: true, OpusSampleRate: 44100}, 48000},
	} {
		if got := m.opusRate(cam, c.ch); got != c.want {
			t.Errorf("%+v: rate %d want %d", c.ch, got, c.want)
		}
	}
	off := New(Options{Hwaccel: "none", Log: testLog(&syncBuf{})})
	defer off.Close()
	if off.opusRate(cam, rpc.Channel{Opus: true, OpusSampleRate: 24000}) != 0 {
		t.Error("--opus off must win")
	}
	a, b := rpc.Channel{Opus: true, OpusSampleRate: 24000}, rpc.Channel{Opus: true, OpusSampleRate: 16000}
	if pushKey(a) == pushKey(b) || pushKey(a) == pushKey(rpc.Channel{}) {
		t.Error("opus rate/enable must be part of the push identity")
	}
	if pushKey(rpc.Channel{Opus: true}) != pushKey(rpc.Channel{Opus: true, OpusSampleRate: 48000}) {
		t.Error("0 and 48000 are the same rate")
	}
}

// --audio-share off: no shared Encoder, every pusher has its own pipeline. --opus-rate N
// forces the rate, and Opus on for a channel Protect asked withOpus even with --opus off;
// without it the channel's own opus_sample_rate counts.
func TestAudioShareOffAndForcedOpusRate(t *testing.T) {
	cam := &camera{id: "C", encs: map[string]*audio.Encoder{}}
	off := New(Options{Hwaccel: "none", NoAudioShare: true, Log: testLog(&syncBuf{})})
	if off.encoder(cam, "u") != nil || len(cam.encs) != 0 {
		t.Error("audio-share off still made a shared encoder")
	}
	on := New(Options{Hwaccel: "none", Log: testLog(&syncBuf{})})
	if on.encoder(cam, "u") == nil || on.encoder(cam, "u") != cam.encs["u"] {
		t.Error("audio-share on: want one encoder per URL")
	}
	ch := rpc.Channel{Opus: true, OpusSampleRate: 24000}
	for _, c := range []struct {
		o    Options
		ch   rpc.Channel
		want int
	}{
		{Options{Opus: true}, ch, 24000},
		{Options{Opus: true, OpusRate: 48000}, ch, 48000},
		{Options{Opus: false, OpusRate: 16000}, ch, 16000},
		{Options{Opus: false}, ch, 0},
		{Options{Opus: true, OpusRate: 48000}, rpc.Channel{}, 0}, // Protect did not ask for Opus
	} {
		c.o.Hwaccel, c.o.Log = "none", testLog(&syncBuf{})
		if got := New(c.o).opusRate(cam, c.ch); got != c.want {
			t.Errorf("%+v %+v: rate %d, want %d", c.o, c.ch, got, c.want)
		}
	}
}
