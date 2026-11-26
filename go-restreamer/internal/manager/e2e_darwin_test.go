// SPDX-License-Identifier: AGPL-3.0-or-later

//go:build darwin && arm64

package manager

import (
	"bytes"
	"os"
	"os/exec"
	"path/filepath"
	"slices"
	"testing"
	"time"

	"github.com/bluenviron/gortsplib/v4/pkg/format"
	"github.com/bluenviron/gortsplib/v4/pkg/format/rtph265"

	"diyport/go-restreamer/internal/rpc"
	"diyport/go-restreamer/internal/transcode"
)

// h265Source makes an H.265 clip and cuts it into AUs (an AU starts at the first slice segment
// of a picture, any parameter sets just before it riding along).
func h265Source(t *testing.T, ff string) *videoSource {
	clip := filepath.Join(t.TempDir(), "c.h265")
	enc := "libx265"
	if out, _ := exec.Command(ff, "-hide_banner", "-encoders").Output(); !bytes.Contains(out, []byte("libx265")) {
		enc = "hevc_videotoolbox"
	}
	args := []string{"-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=20",
		"-t", "2", "-c:v", enc, "-g", "20", "-bf", "0", "-f", "hevc", clip}
	if enc == "libx265" {
		args = append(args[:len(args)-3], "-x265-params", "log-level=none:bframes=0:keyint=20:repeat-headers=1", "-f", "hevc", clip)
	}
	if out, err := exec.Command(ff, args...).CombinedOutput(); err != nil {
		t.Skipf("can't make an H.265 clip: %v %s", err, out)
	}
	data, _ := os.ReadFile(clip)
	var nals [][]byte
	for _, part := range bytes.Split(data, []byte{0, 0, 1}) {
		if part = bytes.TrimRight(part, "\x00"); len(part) > 0 {
			nals = append(nals, part)
		}
	}
	var aus [][][]byte
	var cur [][]byte
	var vps, sps, pps []byte
	hasVCL := false
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
		if (ty < 32 && n[2]&0x80 != 0 && hasVCL) || (ty >= 32 && hasVCL) {
			aus = append(aus, cur)
			cur, hasVCL = nil, false
		}
		cur = append(cur, n)
		if ty < 32 {
			hasVCL = true
		}
	}
	if len(cur) > 0 {
		aus = append(aus, cur)
	}
	if len(aus) < 20 || vps == nil {
		t.Fatalf("clip split into %d AUs", len(aus))
	}
	e := &rtph265.Encoder{PayloadType: 96}
	if err := e.Init(); err != nil {
		t.Fatal(err)
	}
	return &videoSource{aus: aus, encode: e.Encode, format: &format.H265{PayloadTyp: 96, VPS: vps, SPS: sps, PPS: pps}}
}

// An H.265 camera configured with mode "copy" (all Python ever sends) is routed through the
// VideoToolbox transcode on its Tracks, and Protect gets H.264 + (silent) AAC.
func TestEndToEndH265Transcode(t *testing.T) {
	caps := transcode.Probe("", "videotoolbox")
	if !caps.OK() {
		t.Skipf("no videotoolbox: %s", caps.Reason)
	}
	vs := h265Source(t, caps.FFmpeg)
	url := serveRTSP(t, vs, nil, nil)
	dst := newSink(t)
	logs := &syncBuf{}
	m := New(Options{Hwaccel: "videotoolbox", Caps: &caps, Log: testLog(logs), Tune: fastTune})
	defer m.Close()
	if _, err := m.Configure(rpc.ConfigureParams{DeviceID: "HEVC1", Mode: "copy", Channels: []rpc.Channel{{
		Channel: "video1", StreamName: "video1", RTSPURL: url, DestHost: dst.host(), DestPort: dst.port(), AudioEnabled: true}}}); err != nil {
		t.Fatal(err)
	}
	plain := pushedFLV(t, dst, 30, 20)
	if got := probeCodecs(t, plain); !slices.Contains(got, "h264") || !slices.Contains(got, "aac") || slices.Contains(got, "hevc") {
		t.Fatalf("ffprobe codecs = %v", got)
	}
	st := m.Stats().(rpc.Stats)
	if len(st.Transcode) != 1 || st.Transcode[0].Mode != "transcode" || st.Cameras["HEVC1"].Channels[0].Error != "" {
		t.Errorf("stats = %+v", st)
	}
}

// Without a hardware encoder an H.265 camera is reported, not pushed.
func TestH265WithoutEncoderReportsError(t *testing.T) {
	ff := needTool(t, "ffmpeg")
	vs := h265Source(t, ff)
	url := serveRTSP(t, vs, nil, nil)
	dst := newSink(t)
	caps := transcode.Caps{FFmpeg: ff, Reason: "test: none"}
	m := New(Options{Hwaccel: "none", Caps: &caps, Log: testLog(&syncBuf{}), Tune: fastTune})
	defer m.Close()
	if _, err := m.Configure(rpc.ConfigureParams{DeviceID: "HEVC2", Channels: []rpc.Channel{{
		Channel: "video1", StreamName: "video1", RTSPURL: url, DestHost: dst.host(), DestPort: dst.port()}}}); err != nil {
		t.Fatal(err)
	}
	waitFor(t, "ERR_UNSUPPORTED_CODEC_HEVC in stats", 10*time.Second, func() bool {
		return m.Stats().(rpc.Stats).Cameras["HEVC2"].Channels[0].Error == "ERR_UNSUPPORTED_CODEC_HEVC"
	})
}
