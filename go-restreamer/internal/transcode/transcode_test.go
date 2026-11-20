// SPDX-License-Identifier: AGPL-3.0-or-later

package transcode

import (
	"bytes"
	"strings"
	"testing"
	"time"

	"diyport/go-restreamer/internal/media"
)

func TestAUSplitter(t *testing.T) {
	// Two AUs, mixed 3- and 4-byte start codes, fed a byte at a time so every boundary
	// (including a start code split across writes) is exercised.
	stream := []byte{0, 0, 0, 1, 0x09, 0xf0, // AUD
		0, 0, 1, 0x67, 1, 2, // SPS
		0, 0, 0, 1, 0x68, 3, // PPS
		0, 0, 1, 0x65, 4, 5, 6, 0, // IDR slice, trailing zero belongs to the next 4-byte code
		0, 0, 0, 1, 0x09, 0xf0, // AUD
		0, 0, 1, 0x41, 7, 8, // P slice
		0, 0, 0, 1, 0x09, 0xf0} // AUD (closes the second AU)
	var got [][][]byte
	s := &auSplitter{emit: func(n [][]byte) { got = append(got, n) }}
	for _, b := range stream {
		s.write([]byte{b})
	}
	if len(got) != 2 || len(got[0]) != 3 || len(got[1]) != 1 {
		t.Fatalf("got %v", got)
	}
	if !bytes.Equal(got[0][2], []byte{0x65, 4, 5, 6}) || !bytes.Equal(got[1][0], []byte{0x41, 7, 8}) {
		t.Fatalf("nal bytes wrong: %v", got)
	}
}

func TestAnnexB(t *testing.T) {
	out := annexB(nil, []byte{1, 2}, nil, []byte{3})
	if !bytes.Equal(out, []byte{0, 0, 0, 1, 1, 2, 0, 0, 0, 1, 3}) {
		t.Fatalf("%v", out)
	}
}

func TestBuildArgs(t *testing.T) {
	c := Caps{FFmpeg: "ffmpeg", Backend: "videotoolbox", Encoder: "h264_videotoolbox"}
	src := media.Tracks{Video: media.H265, Width: 2560, Height: 1440}
	a := strings.Join(buildArgs(c, src, 20, []Profile{
		{Name: "video1", BitrateKbps: 4000},
		{Name: "video3", Width: 640, Height: 360, BitrateKbps: 500, GOP: 24},
	}), " ")
	for _, want := range []string{
		"-hwaccel videotoolbox", "-f hevc -i pipe:0",
		"-c:v h264_videotoolbox -realtime 1 -bf 0 -profile:v main -b:v 4000k -g 20",
		"scale_vt=w=640:h=360", "-b:v 500k -g 24", "pipe:3", "pipe:4",
	} {
		if !strings.Contains(a, want) {
			t.Errorf("args missing %q:\n%s", want, a)
		}
	}
	if strings.Count(a, "scale_vt") != 1 {
		t.Errorf("source-size profile must not scale:\n%s", a)
	}
}

type sink struct {
	tracks []media.Tracks
	video  []media.VideoAU
	audio  []media.AudioFrame
	disc   []error
}

func (s *sink) OnTracks(t media.Tracks)    { s.tracks = append(s.tracks, t) }
func (s *sink) OnVideo(a media.VideoAU)    { s.video = append(s.video, a) }
func (s *sink) OnAudio(f media.AudioFrame) { s.audio = append(s.audio, f) }
func (s *sink) OnDisconnect(err error)     { s.disc = append(s.disc, err) }

func TestHEVCWithoutEncoderIsUnsupported(t *testing.T) {
	tc := New(Config{Caps: Caps{FFmpeg: "ffmpeg", Reason: "test"}, Profiles: []Profile{{Name: "video1"}}})
	if err := tc.Check(media.H265); err != ErrUnsupportedHEVC {
		t.Fatalf("Check(H265) = %v", err)
	}
	if err := tc.Check(media.H264); err != nil {
		t.Fatalf("Check(H264) = %v", err)
	}
	out := &sink{}
	tc.Output("video1", out)
	tc.OnTracks(media.Tracks{Video: media.H265})
	if len(out.disc) != 1 || out.disc[0] != ErrUnsupportedHEVC || tc.Stats().Mode != ModeUnsupported {
		t.Fatalf("disc=%v mode=%v", out.disc, tc.Stats().Mode)
	}
}

func TestH264WithoutEncoderCopies(t *testing.T) {
	tc := New(Config{Caps: Caps{FFmpeg: "ffmpeg", Reason: "test"}, Profiles: []Profile{{Name: "video1"}, {Name: "video3"}}})
	a, b := &sink{}, &sink{}
	tc.Output("video1", a)
	tc.Output("video3", b)
	tc.OnTracks(media.Tracks{Video: media.H264, SPS: []byte{0x67}, PPS: []byte{0x68}})
	tc.OnVideo(media.VideoAU{IDR: true, NALUs: [][]byte{{0x65}}})
	tc.OnAudio(media.AudioFrame{Data: []byte{1}})
	for _, s := range []*sink{a, b} {
		if len(s.tracks) != 1 || len(s.video) != 1 || len(s.audio) != 1 {
			t.Fatalf("copy mode delivered %+v", s)
		}
	}
	if tc.Stats().Mode != ModeCopy {
		t.Fatalf("mode = %v", tc.Stats().Mode)
	}
}

func TestProbeNone(t *testing.T) {
	if c := Probe("", "none"); c.OK() || c.Reason == "" {
		t.Fatalf("Probe none = %+v", c)
	}
	if c := Probe("/nonexistent/ffmpeg", "auto"); c.OK() || c.FFmpeg != "" {
		t.Fatalf("Probe missing = %+v", c)
	}
}

// The AI Port exact profiles' geometry (ubnt_av_aiport FUN_001101c8): HQ keeps the source size
// capped at 2160 on the short side, AQ is width 1280 and LQ width 640 (portrait: that is the height),
// the other side following the aspect, even.
func TestProfileTarget(t *testing.T) {
	cases := []struct {
		p       Profile
		w, h    int
		scaled  bool
		comment string
	}{
		{Profile{MaxShortSide: 2160}, 2560, 1440, false, "HQ under the cap"},
		{Profile{MaxShortSide: 2160}, 7680, 4320, true, "HQ capped (short side): 3840x2160"},
		{Profile{FitWidth: 1280}, 2560, 1440, true, "AQ 1280x720"},
		{Profile{FitWidth: 640}, 2560, 1440, true, "LQ 640x360"},
		{Profile{FitWidth: 640}, 1920, 1088, true, "odd ratio rounds to even"},
		{Profile{FitWidth: 1280}, 1080, 1920, true, "portrait: height 1280"},
		{Profile{Width: 640, Height: 360}, 1920, 1080, true, "explicit size"},
	}
	want := [][2]int{{2560, 1440}, {3840, 2160}, {1280, 720}, {640, 360}, {640, 362}, {720, 1280}, {640, 360}}
	for i, c := range cases {
		w, h, scaled := c.p.target(media.Tracks{Width: c.w, Height: c.h})
		if w != want[i][0] || h != want[i][1] || scaled != c.scaled {
			t.Errorf("%s: %dx%d scaled=%v, want %dx%d", c.comment, w, h, scaled, want[i][0], want[i][1])
		}
	}
}

func TestBuildArgsAIPortProfiles(t *testing.T) {
	c := Caps{FFmpeg: "ffmpeg", Backend: "videotoolbox", Encoder: "h264_videotoolbox"}
	src := media.Tracks{Video: media.H264, Width: 2560, Height: 1440}
	a := strings.Join(buildArgs(c, src, 20, []Profile{
		{Name: "video1", MaxShortSide: 2160, BitrateKbps: 8000},
		{Name: "video2", FitWidth: 1280, FPS: 24, BitrateKbps: 2000, GOP: 24},
		{Name: "video3", FitWidth: 640, FPS: 24, BitrateKbps: 300, GOP: 24},
	}), " ")
	for _, want := range []string{
		"-b:v 8000k -g 20", // source fps 20: a 1 s GOP
		"-vf scale_vt=w=1280:h=720,fps=24 -c:v h264_videotoolbox", "-b:v 2000k -g 24",
		"-vf scale_vt=w=640:h=360,fps=24", "-b:v 300k -g 24",
	} {
		if !strings.Contains(a, want) {
			t.Errorf("args missing %q:\n%s", want, a)
		}
	}
	if strings.Count(a, "fps=") != 2 || strings.Count(a, "scale_vt") != 2 {
		t.Errorf("HQ must neither scale nor resample:\n%s", a)
	}
	// A 24 fps source needs no fps filter.
	a = strings.Join(buildArgs(c, src, 24, []Profile{{Name: "video2", FitWidth: 1280, FPS: 24}}), " ")
	if strings.Contains(a, "fps=") {
		t.Errorf("24 -> 24 resampled:\n%s", a)
	}
}

// A resampled profile's AUs are stamped on its own grid: the first takes the first input's PTS,
// each next steps 1/FPS, and a drift past a second snaps back to the newest input.
func TestResampledPTSGrid(t *testing.T) {
	tr := New(Config{Name: "t", Caps: Caps{Reason: "none"}, Profiles: []Profile{{Name: "a", FPS: 24}, {Name: "b"}}})
	r := tr.startRun(media.Tracks{Video: media.H264, Width: 640, Height: 360, FPS: 20})
	defer r.cancel()
	r.ptsMu.Lock()
	r.ptsQ[0] = []time.Duration{10 * time.Second, 10*time.Second + 50*time.Millisecond}
	r.ptsQ[1] = []time.Duration{10 * time.Second, 10*time.Second + 50*time.Millisecond}
	r.latestIn = 10*time.Second + 50*time.Millisecond
	r.ptsMu.Unlock()
	p0 := r.nextPTS(0)
	p1 := r.nextPTS(0)
	if p0 != 10*time.Second || p1-p0 != time.Second/24 {
		t.Errorf("grid %v %v", p0, p1)
	}
	// The 1:1 profile pairs by count.
	if a, b := r.nextPTS(1), r.nextPTS(1); a != 10*time.Second || b != 10*time.Second+50*time.Millisecond {
		t.Errorf("1:1 pts %v %v", a, b)
	}
	r.ptsMu.Lock()
	r.latestIn = 20 * time.Second
	r.ptsMu.Unlock()
	if got := r.nextPTS(0); got != 20*time.Second {
		t.Errorf("drifted grid not snapped: %v", got)
	}
}
