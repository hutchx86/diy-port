// SPDX-License-Identifier: AGPL-3.0-or-later

package transcode

import (
	"fmt"
	"math"
	"os"
	"strconv"
	"strings"

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
		w, h, scaled := p.target(src)
		resample := p.resamples(fps)
		fpsF := "fps=" + strconv.FormatFloat(p.FPS, 'f', -1, 64)
		switch c.Backend {
		case "videotoolbox":
			var vf []string
			if scaled {
				vf = append(vf, fmt.Sprintf("scale_vt=w=%d:h=%d", w, h))
			}
			if resample {
				vf = append(vf, fpsF)
			}
			if len(vf) > 0 {
				a = append(a, "-vf", strings.Join(vf, ","))
			}
			a = append(a, "-c:v", c.Encoder, "-realtime", "1", "-bf", "0", "-profile:v", "main")
		case "vaapi":
			vf := "format=nv12"
			if scaled {
				vf = fmt.Sprintf("scale=%d:%d,format=nv12", w, h)
			}
			if resample {
				vf = fpsF + "," + vf
			}
			a = append(a, "-vf", vf+",hwupload", "-c:v", c.Encoder, "-bf", "0", "-profile:v", "main")
		default:
			var vf []string
			if scaled {
				vf = append(vf, fmt.Sprintf("scale=%d:%d", w, h))
			}
			if resample {
				vf = append(vf, fpsF)
			}
			if len(vf) > 0 {
				a = append(a, "-vf", strings.Join(vf, ","))
			}
			a = append(a, "-c:v", c.Encoder, "-bf", "0", "-profile:v", "main")
		}
		a = append(a, "-b:v", fmt.Sprintf("%dk", kbps), "-g", strconv.Itoa(gop),
			"-fps_mode", "passthrough", "-bsf:v", metadataBSF(),
			"-f", "h264", "pipe:"+strconv.Itoa(3+i))
	}
	return a
}

// target is the picture size this profile encodes at for a source of src's size, and whether
// that differs from the source. Rules, from the AI Port's ubnt_av_aiport (FUN_001101c8, spec 2):
// FitWidth N is "width N" for a landscape source and "height N" for a portrait one, the other
// side keeping the aspect; MaxShortSide N caps the short side (HQ: 0x870 = 2160); explicit Width
// and Height win over both. The spec says the HQ cap is on the "long side", but its own HQ
// default is 3840x2160 (a 4K camera is not shrunk), so the cap has to be on the side that is 2160
// there: the short one. A literal long-side cap would turn 2560x1440 into 2160x1216. The other side is rounded to an even number: the aspect helper
// FUN_0010e698's rounding is not decompiled (mod-2 or mod-16, open question 1), and 4:2:0
// needs even.
func (p Profile) target(src media.Tracks) (w, h int, scaled bool) {
	w, h = src.Width, src.Height
	switch {
	case p.Width > 0 && p.Height > 0:
		w, h = p.Width, p.Height
	case w <= 0 || h <= 0:
	case p.FitWidth > 0:
		if w >= h {
			w, h = p.FitWidth, evenRound(float64(h)*float64(p.FitWidth)/float64(w))
		} else {
			w, h = evenRound(float64(w)*float64(p.FitWidth)/float64(h)), p.FitWidth
		}
	case p.MaxShortSide > 0 && min(w, h) > p.MaxShortSide:
		if w >= h {
			w, h = evenRound(float64(w)*float64(p.MaxShortSide)/float64(h)), p.MaxShortSide
		} else {
			w, h = p.MaxShortSide, evenRound(float64(h)*float64(p.MaxShortSide)/float64(w))
		}
	}
	return w, h, w != src.Width || h != src.Height
}

// evenRound rounds to the nearest even integer.
func evenRound(v float64) int { return int(math.Round(v/2)) * 2 }

// resamples reports whether the profile's frame rate differs from the source's, so ffmpeg's fps
// filter has to duplicate or drop frames (output AUs are then no longer 1:1 with input AUs).
func (p Profile) resamples(srcFPS float64) bool {
	return p.FPS > 0 && math.Abs(p.FPS-srcFPS) > 0.01
}

// metadataBSF: AUDs always; AIPORT_ONVIF_VUI_TV=1 also clears video_full_range_flag. Hikvision
// sources arrive full range (yuvj420p, color_range=pc) and scale_vt keeps that, while native
// UniFi medium streams are limited range (live 2026-11-12 21:32: F4E2C60D4B4C_1 tv, ours pc).
// Experiment for the blank dashboard HQ (video2); it only rewrites the flag, not the pixels.
func metadataBSF() string {
	if os.Getenv("AIPORT_ONVIF_VUI_TV") == "1" {
		return "h264_metadata=aud=insert:video_full_range_flag=0"
	}
	return "h264_metadata=aud=insert"
}
