// SPDX-License-Identifier: AGPL-3.0-or-later

package transcode

import (
	"bytes"
	"strings"
	"testing"

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
