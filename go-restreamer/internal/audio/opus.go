// SPDX-License-Identifier: AGPL-3.0-or-later

package audio

import (
	"bytes"
	"fmt"
	"io"
	"os/exec"
	"sync"
)

// Opus track for Protect's browser live view. yi-protect (FlvPush.cpp, main.cpp), the working
// streamer: the web/desktop live view plays ONLY an Opus track carried as FLV tag type 10,
// 48 kHz mono, 20 ms frames, 64 kbps (main.cpp opus_encoder_create(48000, 1) +
// OPUS_SET_BITRATE(64000), 960 samples per frame); AAC (type 8) serves recording and mobile.
const (
	OpusRate     = 48000
	OpusFrameLen = 960 // samples per 20 ms packet at 48 kHz
	OpusFrameDur = 20  // ms
)

// OpusRateFor maps a requested Opus sample rate to one libopus takes (8/12/16/24/48 kHz): 0 is
// "not given" and means 48000; ok is false when the request was something else (48000 is
// returned and the caller warns).
func OpusRateFor(r int) (rate int, ok bool) {
	switch r {
	case 0:
		return OpusRate, true
	case 8000, 12000, 16000, 24000, 48000:
		return r, true
	}
	return OpusRate, false
}

// silentOpus is a 20 ms mono CELT fullband silence packet (TOC 0xF8, then the two bytes the
// encoder writes for digital silence). It stands in when there is no audio or it is
// disabled, the way yi-protect encodes zeroed PCM when muted (main.cpp: "still decode so
// the encoder learns the real rate, but hand it silence").
var silentOpus = []byte{0xF8, 0xFF, 0xFE}

// aacRates is the ADTS/ASC sampling_frequency_index table.
var aacRates = [...]int{96000, 88200, 64000, 48000, 44100, 32000, 24000, 22050, 16000, 12000, 11025, 8000, 7350}

// ASCRate is the sample rate named by an AAC AudioSpecificConfig, 0 if unknown.
func ASCRate(asc []byte) int {
	if len(asc) < 2 {
		return 0
	}
	if i := int(asc[0]&7)<<1 | int(asc[1]>>7); i < len(aacRates) {
		return aacRates[i]
	}
	return 0
}

// adtsHeader wraps a raw AAC access unit of n bytes for ffmpeg's ADTS demuxer, from the
// camera's AudioSpecificConfig (object type, rate index, channels). ok is false for a config
// ADTS can't express.
func adtsHeader(asc []byte, n int) (h [7]byte, ok bool) {
	if len(asc) < 2 {
		return h, false
	}
	aot, sfi, ch := int(asc[0]>>3), int(asc[0]&7)<<1|int(asc[1]>>7), int(asc[1]>>3)&0xF
	if aot < 1 || aot > 4 || sfi > 12 || ch > 7 {
		return h, false
	}
	l := n + 7
	h = [7]byte{0xFF, 0xF1, byte((aot-1)<<6 | sfi<<2 | ch>>2), byte(ch&3<<6 | l>>11), byte(l >> 3),
		byte(l<<5) | 0x1F, 0xFC}
	return h, true
}

var (
	opusEncMu sync.Mutex
	opusEnc   = map[string]bool{}
)

// opusEncoderAvailable reports whether this ffmpeg has libopus. Checked up front because an
// ffmpeg that can't open its Opus output dies, taking the AAC output with it.
func opusEncoderAvailable(path string) bool {
	opusEncMu.Lock()
	defer opusEncMu.Unlock()
	if v, ok := opusEnc[path]; ok {
		return v
	}
	out, err := exec.Command(path, "-hide_banner", "-encoders").Output()
	v := err == nil && bytes.Contains(out, []byte(" libopus "))
	opusEnc[path] = v
	return v
}

// oggPackets reads Opus packets out of an Ogg stream (ffmpeg's `-f ogg -page_duration 20000`
// writes one 20 ms packet per page, so each leaves ffmpeg as soon as it is encoded instead of
// in a muxer's buffer-sized burst). It skips OpusHead/OpusTags and handles packets that span
// pages or share one, so it does not depend on that page layout.
type oggPackets struct {
	r       io.Reader
	partial []byte
	ready   [][]byte
}

func (o *oggPackets) next() ([]byte, error) {
	for len(o.ready) == 0 {
		var h [27]byte
		if _, err := io.ReadFull(o.r, h[:]); err != nil {
			return nil, err
		}
		if string(h[:4]) != "OggS" {
			return nil, fmt.Errorf("lost Ogg sync")
		}
		segs := make([]byte, h[26])
		if _, err := io.ReadFull(o.r, segs); err != nil {
			return nil, err
		}
		for _, lace := range segs {
			buf := make([]byte, lace)
			if _, err := io.ReadFull(o.r, buf); err != nil {
				return nil, err
			}
			o.partial = append(o.partial, buf...)
			if lace == 255 {
				continue
			}
			p := o.partial
			o.partial = nil
			if bytes.HasPrefix(p, []byte("OpusHead")) || bytes.HasPrefix(p, []byte("OpusTags")) || len(p) == 0 {
				continue
			}
			o.ready = append(o.ready, p)
		}
	}
	p := o.ready[0]
	o.ready = o.ready[1:]
	return p, nil
}
