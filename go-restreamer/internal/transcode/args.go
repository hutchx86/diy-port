// SPDX-License-Identifier: AGPL-3.0-or-later

package transcode

import (
	"fmt"
	"math"
	"strconv"

	"diyport/go-restreamer/internal/media"
)

// buildArgs is the ffmpeg command: Annex-B on stdin, one raw H.264 pipe (fd 3+i) per profile,
// an AUD ahead of every AU so the reader can split them (see auSplitter).
//
// videotoolbox: decode on the GPU and keep frames there (videotoolbox_vld) so scale_vt scales
// them without a download; the encoder settings are onvif_push.py's vtenc_h264 ones in ffmpeg
// terms (realtime, no B-frames, main profile, GOP one second for video1, 24 for the subs).
// rkmpp / vaapi are untested (no such hardware on the dev Mac): software decode, hardware
// encode, scaled on the CPU.
func buildArgs(c Caps, src media.Tracks, fps float64, profiles []Profile) []string {
	a := []string{"-hide_banner", "-loglevel", "error", "-nostdin"}
	switch c.Backend {
	case "videotoolbox":
		a = append(a, "-hwaccel", "videotoolbox", "-hwaccel_output_format", "videotoolbox_vld")
	case "vaapi":
		a = append(a, "-init_hw_device", "vaapi=va:"+vaapiDevice, "-filter_hw_device", "va")
	}
	in := "h264"
	if src.Video == media.H265 {
		in = "hevc"
	}
	// Raw Annex-B has no clock: -r says what it is, and the output pairs PTS by count.
	// (No -fflags nobuffer / -analyzeduration 0: they make the raw HEVC parser mis-split frames,
	// "Could not find ref with POC", and only the first GOP decodes.)
	a = append(a, "-flags", "low_delay", "-r", strconv.FormatFloat(fps, 'f', -1, 64), "-f", in, "-i", "pipe:0")
	for i, p := range profiles {
		kbps := p.BitrateKbps
		if kbps <= 0 {
			kbps = 2000
		}
		gop := p.GOP
		if gop <= 0 {
			gop = int(math.Round(fps))
		}
		a = append(a, "-map", "0:v:0", "-an")
		scaled := p.Width > 0 && p.Height > 0 && (p.Width != src.Width || p.Height != src.Height)
		switch c.Backend {
		case "videotoolbox":
			if scaled {
				a = append(a, "-vf", fmt.Sprintf("scale_vt=w=%d:h=%d", p.Width, p.Height))
			}
			a = append(a, "-c:v", c.Encoder, "-realtime", "1", "-bf", "0", "-profile:v", "main")
		case "vaapi":
			vf := "format=nv12"
			if scaled {
				vf = fmt.Sprintf("scale=%d:%d,format=nv12", p.Width, p.Height)
			}
			a = append(a, "-vf", vf+",hwupload", "-c:v", c.Encoder, "-bf", "0", "-profile:v", "main")
		default:
			if scaled {
				a = append(a, "-vf", fmt.Sprintf("scale=%d:%d", p.Width, p.Height))
			}
			a = append(a, "-c:v", c.Encoder, "-bf", "0", "-profile:v", "main")
		}
		a = append(a, "-b:v", fmt.Sprintf("%dk", kbps), "-g", strconv.Itoa(gop),
			"-fps_mode", "passthrough", "-bsf:v", "h264_metadata=aud=insert",
			"-f", "h264", "pipe:"+strconv.Itoa(3+i))
	}
	return a
}
