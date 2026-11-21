// SPDX-License-Identifier: AGPL-3.0-or-later

//go:build darwin && arm64

package transcode

import (
	"bytes"
	"io"
	"log/slog"
	"os"
	"os/exec"
	"path/filepath"
	"sync"
	"testing"
	"time"

	"diyport/go-restreamer/internal/media"
)

// splitHEVC cuts an Annex-B HEVC file into access units (NALs without start codes). An AU
// starts at its first slice segment (first_slice_segment_in_pic_flag, the top bit of the byte
// after the 2-byte NAL header), taking any parameter sets just before it along.
func splitHEVC(data []byte) (aus []media.VideoAU, vps, sps, pps []byte) {
	var nals [][]byte
	sc := []byte{0, 0, 1}
	var starts []int
	for i := 0; ; {
		j := bytes.Index(data[i:], sc)
		if j < 0 {
			break
		}
		starts = append(starts, i+j+3)
		i += j + 3
	}
	for k, st := range starts {
		end := len(data)
		if k+1 < len(starts) {
			end = starts[k+1] - 3
		}
		nals = append(nals, bytes.TrimRight(data[st:end], "\x00"))
	}
	var cur media.VideoAU
	hasVCL := false
	flush := func() {
		if len(cur.NALUs) > 0 {
			aus = append(aus, cur)
		}
		cur, hasVCL = media.VideoAU{}, false
	}
	for _, n := range nals {
		ty := (n[0] >> 1) & 0x3f
		switch ty {
		case 32:
			vps = n
		case 33:
			sps = n
		case 34:
			pps = n
		}
		if ty < 32 && n[2]&0x80 != 0 && hasVCL {
			flush()
		}
		if ty >= 32 && hasVCL {
			flush()
		}
		cur.NALUs = append(cur.NALUs, n)
		if ty < 32 {
			hasVCL = true
			if ty == 19 || ty == 20 {
				cur.IDR = true
			}
		}
	}
	flush()
	return
}

// Live-ish: a real H.265 clip through the real VideoToolbox ffmpeg comes out as H.264 AUs,
// at source size and scaled, with audio passed along.
func TestVideoToolboxHEVCToH264(t *testing.T) {
	caps := Probe("", "videotoolbox")
	if !caps.OK() {
		t.Skipf("no videotoolbox: %s", caps.Reason)
	}
	dir := t.TempDir()
	clip := filepath.Join(dir, "clip.h265")
	enc := "libx265"
	if out, _ := exec.Command(caps.FFmpeg, "-hide_banner", "-encoders").Output(); !contains(out, "libx265") {
		enc = "hevc_videotoolbox"
	}
	args := []string{"-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=20",
		"-t", "4", "-c:v", enc, "-g", "20", "-bf", "0", "-f", "hevc", clip}
	if enc == "libx265" {
		args = append(args[:len(args)-3], "-x265-params", "log-level=none:bframes=0:keyint=20:repeat-headers=1", "-f", "hevc", clip)
	}
	if out, err := exec.Command(caps.FFmpeg, args...).CombinedOutput(); err != nil {
		t.Skipf("can't make an H.265 clip: %v %s", err, out)
	}
	data, err := os.ReadFile(clip)
	if err != nil {
		t.Fatal(err)
	}
	aus, vps, sps, pps := splitHEVC(data)
	if len(aus) < 40 || vps == nil {
		t.Fatalf("clip split into %d AUs", len(aus))
	}

	tc := New(Config{Name: "test", Caps: caps, Log: slog.New(slog.NewTextHandler(io.Discard, nil)),
		Profiles: []Profile{{Name: "video1", BitrateKbps: 2000}, {Name: "video3", Width: 640, Height: 360, BitrateKbps: 500, GOP: 24}}})
	defer tc.Close()
	var mu sync.Mutex
	outs := map[string]*lockedSink{"video1": {mu: &mu}, "video3": {mu: &mu}}
	for n, o := range outs {
		tc.Output(n, o)
	}
	tc.OnTracks(media.Tracks{Video: media.H265, VPS: vps, SPS: sps, PPS: pps, Width: 1280, Height: 720, FPS: 20,
		Audio: media.PCMU, AudioRate: 8000, AudioChannels: 1})
	pts := time.Duration(0)
	for i, au := range aus {
		au.PTS = pts
		tc.OnVideo(au)
		if i%5 == 0 && i > 0 {
			tc.OnAudio(media.AudioFrame{PTS: pts, Data: []byte{0xff, 0xff}})
		}
		pts += 50 * time.Millisecond
		time.Sleep(15 * time.Millisecond)
	}
	deadline := time.Now().Add(10 * time.Second)
	for time.Now().Before(deadline) {
		mu.Lock()
		// Both outputs: waiting on video1 alone checked video3 while it was still a few AUs
		// behind (67 of 80, flaky with the live instance also using VideoToolbox).
		n := min(len(outs["video1"].video), len(outs["video3"].video))
		mu.Unlock()
		if n >= len(aus)-12 {
			break
		}
		time.Sleep(50 * time.Millisecond)
	}
	mu.Lock()
	defer mu.Unlock()
	for name, want := range map[string][2]int{"video1": {1280, 720}, "video3": {640, 360}} {
		o := outs[name]
		if len(o.tracks) == 0 || o.tracks[0].Video != media.H264 || o.tracks[0].Width != want[0] || o.tracks[0].Height != want[1] {
			t.Fatalf("%s tracks = %+v", name, o.tracks)
		}
		if len(o.video) < len(aus)-12 {
			t.Fatalf("%s: %d AUs out of %d in", name, len(o.video), len(aus))
		}
		if !o.video[0].IDR {
			t.Errorf("%s: first AU isn't an IDR", name)
		}
		idrs := 0
		for i, au := range o.video {
			if au.IDR {
				idrs++
			}
			if i > 0 && au.PTS < o.video[i-1].PTS {
				t.Fatalf("%s: PTS stepped back at %d: %v", name, i, ptsList(o.video))
			}
		}
		if idrs < 2 {
			t.Errorf("%s: %d IDRs", name, idrs)
		}
		if len(o.audio) == 0 {
			t.Errorf("%s: no audio passed through", name)
		}
	}
	if st := tc.Stats(); st.Mode != ModeTranscode || st.Restarts != 0 {
		t.Errorf("stats = %+v", st)
	}
}

func contains(b []byte, s string) bool { return len(b) > 0 && indexOf(string(b), s) >= 0 }
func indexOf(h, n string) int {
	for i := 0; i+len(n) <= len(h); i++ {
		if h[i:i+len(n)] == n {
			return i
		}
	}
	return -1
}

// lockedSink is a sink whose callbacks share a mutex with the test's reads.
type lockedSink struct {
	mu     *sync.Mutex
	tracks []media.Tracks
	video  []media.VideoAU
	audio  []media.AudioFrame
}

func (s *lockedSink) OnTracks(t media.Tracks) {
	s.mu.Lock()
	s.tracks = append(s.tracks, t)
	s.mu.Unlock()
}
func (s *lockedSink) OnVideo(a media.VideoAU) {
	s.mu.Lock()
	s.video = append(s.video, a)
	s.mu.Unlock()
}
func (s *lockedSink) OnAudio(f media.AudioFrame) {
	s.mu.Lock()
	s.audio = append(s.audio, f)
	s.mu.Unlock()
}
func (s *lockedSink) OnDisconnect(error) {}

func ptsList(v []media.VideoAU) []int {
	var o []int
	for _, a := range v {
		o = append(o, int(a.PTS/time.Millisecond))
	}
	return o
}
