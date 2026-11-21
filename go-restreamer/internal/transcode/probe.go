// SPDX-License-Identifier: AGPL-3.0-or-later

// Package transcode decodes a camera's video once and re-encodes it as H.264 for each output
// profile, with one supervised ffmpeg per camera (the AI Port itself decodes once and runs one
// encoder per channel: S0 at a 1 s GOP, S1 and S2 smaller; onvif_push.py's "option C").
// ffmpeg and not CGo bindings: a crashed or wedged VideoToolbox session then costs a
// process, not the restreamer.
package transcode

import (
	"context"
	"errors"
	"os"
	"os/exec"
	"runtime"
	"strings"
	"time"
)

// ErrUnsupportedHEVC is what an H.265 camera gets when this machine has no hardware H.264
// encoder to turn it into something Protect plays (rpc maps it to ERR_UNSUPPORTED_CODEC_HEVC).
// An H.264 camera in the same case is passed through instead.
var ErrUnsupportedHEVC = errors.New("H.265 camera and no hardware H.264 encoder (ERR_UNSUPPORTED_CODEC_HEVC)")

// Caps is what Probe found.
type Caps struct {
	FFmpeg  string // resolved binary; empty when there is none
	Backend string // "videotoolbox", "rkmpp", "vaapi" or "" for none
	Encoder string // the ffmpeg encoder name for Backend
	Reason  string // why Backend is empty, for get_stats and logs
}

// OK reports whether hardware H.264 encoding is available.
func (c Caps) OK() bool { return c.Backend != "" }

// FindFFmpeg resolves the binary: given path, PATH, then Homebrew (launchd's PATH has no
// Homebrew, which is how the Python side ended up hard-coding gst-launch's location).
func FindFFmpeg(bin string) string {
	if bin != "" {
		if p, err := exec.LookPath(bin); err == nil {
			return p
		}
		return ""
	}
	if p, err := exec.LookPath("ffmpeg"); err == nil {
		return p
	}
	for _, p := range []string{"/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg"} {
		if _, err := os.Stat(p); err == nil {
			return p
		}
	}
	return ""
}

// Probe asks ffmpeg what it can encode with. hwaccel is "auto" (or ""), "none", or one of
// "videotoolbox", "rkmpp", "vaapi" to force a backend.
func Probe(ffmpegBin, hwaccel string) Caps {
	bin := FindFFmpeg(ffmpegBin)
	if bin == "" {
		return Caps{Reason: "ffmpeg not found"}
	}
	if hwaccel == "none" {
		return Caps{FFmpeg: bin, Reason: "hardware acceleration disabled"}
	}
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	out, err := exec.CommandContext(ctx, bin, "-hide_banner", "-encoders").Output()
	if err != nil {
		return Caps{FFmpeg: bin, Reason: "ffmpeg -encoders failed: " + err.Error()}
	}
	has := func(enc string) bool {
		for _, l := range strings.Split(string(out), "\n") {
			if f := strings.Fields(l); len(f) >= 2 && f[1] == enc {
				return true
			}
		}
		return false
	}
	var candidates []string
	switch {
	case hwaccel != "" && hwaccel != "auto":
		candidates = []string{hwaccel}
	case runtime.GOOS == "darwin":
		candidates = []string{"videotoolbox"}
	default:
		candidates = []string{"rkmpp", "vaapi"}
	}
	for _, b := range candidates {
		enc := "h264_" + b
		if !has(enc) {
			continue
		}
		if b == "vaapi" {
			if _, err := os.Stat(vaapiDevice); err != nil {
				continue
			}
		}
		return Caps{FFmpeg: bin, Backend: b, Encoder: enc}
	}
	return Caps{FFmpeg: bin, Reason: "no h264 hardware encoder among " + strings.Join(candidates, ", ")}
}

const vaapiDevice = "/dev/dri/renderD128"
