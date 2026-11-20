// SPDX-License-Identifier: AGPL-3.0-or-later

package manager

import (
	"slices"
	"testing"
	"time"

	"diyport/go-restreamer/internal/rpc"
	"diyport/go-restreamer/internal/transcode"
)

// aiport mode end to end, without a hardware encoder (the router passes the camera's H.264
// through): a camera with audio gets header flags 7, AAC re-encoded at 48 kHz and the
// ubnt_streamer script tags; a camera without gets flags 3 and no audio tags at all.
func TestAIPortModeWithAndWithoutAudio(t *testing.T) {
	needTool(t, "ffmpeg")
	for _, withAudio := range []bool{true, false} {
		vs := h264Source(t)
		var url string
		if withAudio {
			afmt, aframes := aacFrames(t)
			url = serveRTSP(t, vs, afmt, aframes)
		} else {
			url = serveRTSP(t, vs, nil, nil)
		}
		dst := newSink(t)
		m := New(Options{Hwaccel: "none", Log: testLog(&syncBuf{})})
		if _, err := m.Configure(rpc.ConfigureParams{DeviceID: "AIPORT1", Mode: "aiport", Channels: []rpc.Channel{{
			Channel: "video1", StreamName: "video1", RTSPURL: url, DestHost: dst.host(), DestPort: dst.port(),
			AudioEnabled: true, BitrateKbps: 4000, FPS: 25}}}); err != nil {
			t.Fatal(err)
		}
		wantAudio := 0
		if withAudio {
			wantAudio = 20
		}
		var plain []byte
		waitFor(t, "aiport push", 25*time.Second, func() bool {
			d, _ := dst.snapshot()
			p, counts, ok := flvTags(d)
			if !ok || counts[9] < 30 || counts[8] < wantAudio {
				return false
			}
			plain, _ = p, counts
			return true
		})
		d, _ := dst.snapshot()
		_, counts, _ := flvTags(d)
		wantFlags := byte(0x03)
		if withAudio {
			wantFlags = 0x07
		}
		if d[4] != wantFlags {
			t.Errorf("withAudio=%v: header flags %#x, want %#x", withAudio, d[4], wantFlags)
		}
		if !withAudio && counts[8] != 0 {
			t.Errorf("audio-less camera got %d audio tags (no filler allowed)", counts[8])
		}
		if withAudio {
			if got := probeCodecs(t, plain); !slices.Contains(got, "h264") || !slices.Contains(got, "aac") {
				t.Errorf("ffprobe codecs %v", got)
			}
		}
		for _, name := range []string{"onMetaData", "onMpma", "onClockSync", "videoBandwidth", "streamName"} {
			if !containsBytes(d, name) {
				t.Errorf("stream lacks %s", name)
			}
		}
		if containsBytes(d, "onFI") {
			t.Error("unexpected script tag")
		}
		m.Close()
	}
}

func containsBytes(b []byte, s string) bool {
	for i := 0; i+len(s) <= len(b); i++ {
		if string(b[i:i+len(s)]) == s {
			return true
		}
	}
	return false
}

// profileFor: the three encoders ubnt_av_aiport builds (spec 2, 3).
func TestAIPortProfiles(t *testing.T) {
	cam := &camera{mode: modeAIPort}
	cases := []struct {
		c    rpc.Channel
		want transcode.Profile
	}{
		{rpc.Channel{Channel: "video1"}, transcode.Profile{Name: "video1", MaxShortSide: 2160, BitrateKbps: 8000}},
		{rpc.Channel{Channel: "video1", BitrateKbps: 5000, FPS: 20}, transcode.Profile{Name: "video1", MaxShortSide: 2160, BitrateKbps: 5000, FPS: 20, GOP: 20}},
		{rpc.Channel{Channel: "video2", BitrateKbps: 9999}, transcode.Profile{Name: "video2", FitWidth: 1280, FPS: 24, BitrateKbps: 2000, GOP: 24}},
		{rpc.Channel{Channel: "video3"}, transcode.Profile{Name: "video3", FitWidth: 640, FPS: 24, BitrateKbps: 300, GOP: 24}},
	}
	for _, c := range cases {
		if got := profileFor(cam, c.c); got != c.want {
			t.Errorf("%s: %+v want %+v", c.c.Channel, got, c.want)
		}
	}
	if got := profileFor(&camera{mode: "copy"}, rpc.Channel{Channel: "video2", Width: 1, Height: 2, BitrateKbps: 3}); got.FitWidth != 0 || got.Width != 1 {
		t.Errorf("non-aiport profile %+v", got)
	}
}

// Switching a camera between aiport and another mode starts it over, since the pushes differ.
func TestAIPortModeSwitchRestartsCamera(t *testing.T) {
	m := New(Options{Hwaccel: "none", Log: testLog(&syncBuf{}), Caps: &transcode.Caps{}})
	defer m.Close()
	dst := newSink(t)
	p := func(mode string) rpc.ConfigureParams {
		return rpc.ConfigureParams{DeviceID: "SW", Mode: mode, Channels: []rpc.Channel{{Channel: "video1", StreamName: "video1",
			RTSPURL: "rtsp://127.0.0.1:1/x", DestHost: dst.host(), DestPort: dst.port()}}}
	}
	m.Configure(p("copy"))
	first := m.cams["SW"]
	m.Configure(p("transcode"))
	if m.cams["SW"] != first {
		t.Error("copy -> transcode must keep the camera")
	}
	m.Configure(p(modeAIPort))
	if m.cams["SW"] == first || m.cams["SW"].mode != modeAIPort {
		t.Error("-> aiport must rebuild the camera")
	}
	if _, ok := m.cams["SW"].chans["video1"].pusher.(interface{ Drops() uint64 }); !ok {
		t.Error("aiport camera has the wrong pusher")
	}
}
