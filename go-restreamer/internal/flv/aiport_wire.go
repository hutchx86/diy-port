// SPDX-License-Identifier: AGPL-3.0-or-later

package flv

import (
	"encoding/binary"
	"math/bits"
)

// The byte layer of the "AI Port exact" push: what ubnt_streamer v5.1.12 writes to Protect's
// :7550 ingest, from docs/aiport-onvif-push-spec.md (decompile evidence; section numbers below
// are that file's). Everything here is a pure function of its arguments so the layouts can be
// tested byte for byte; aiport.go holds the state machine that feeds them.

const (
	// Trailer timescales: video 90 kHz (4.3); AAC at the encoder's 48 kHz (7); Opus at the
	// rate Protect asked for (opusSampleRate, 4.4).
	aiportVideoScale = 90000
	aiportAACRate    = 48000

	// Video codec ids in the tag's first byte, high nibble 1 key / 2 inter (4.3): H.264 is 7
	// (0x17/0x27), H.265 is 8 (0x18/0x28).
	codecIDAVC  = 7
	codecIDHEVC = 8
)

// aiportHeader is the 13-byte FLV header: flags 0x07 with audio, 0x03 without (video | extended,
// 0x02 being extendedFormat; 4.2). The real streamer sets the audio bit only when the track
// exists, so an audio-less camera gets no audio tags and no fake silence.
func aiportHeader(hasAudio bool) []byte {
	flags := byte(0x01 | 0x02)
	if hasAudio {
		flags |= 0x04
	}
	return []byte{'F', 'L', 'V', 1, flags, 0, 0, 0, 9, 0, 0, 0, 0}
}

// ticksOf converts a monotonic time in ns to ticks of scale (ns * scale / 1e9) without
// overflowing: monotonic_ns * 90000 overflows uint64 after about 56 days of uptime.
func ticksOf(ns int64, scale uint32) uint64 {
	if ns < 0 {
		ns = 0
	}
	hi, lo := bits.Mul64(uint64(ns), uint64(scale))
	q, _ := bits.Div64(hi, lo, 1_000_000_000)
	return q
}

// audioTicks is the streamer's audio clock (FUN_001aa980): ns -> 48 kHz ticks, then rescaled to
// the track's timescale.
func audioTicks(ns int64, scale uint32) uint64 {
	return ticksOf(ns, aiportAACRate) * uint64(scale) / aiportAACRate
}

// msOf is ticks -> whole milliseconds (floor). Every tag's FLV timestamp is msOf(ticks) -
// baseDtsMillis, so "trailer ms minus tag ts" is the base for every tag (4.3).
func msOf(ticks uint64, scale uint32) int64 {
	s := uint64(scale)
	return int64(ticks/s*1000 + ticks%s*1000/s)
}

// ticksFromMs is msOf's inverse for whole milliseconds.
func ticksFromMs(ms int64, scale uint32) uint64 {
	return uint64(ms) * uint64(scale) / 1000
}

// aiportTag is one tag, PreviousTagSize, and the 16-byte extendedFlv trailer (4.3): u32
// timescale, u32 cts in ticks of that timescale, u64 absolute monotonic pts ticks.
func aiportTag(typ byte, payload []byte, tsMs int64, scale, cts uint32, ticks uint64) []byte {
	b := appendTag(make([]byte, 0, len(payload)+11+4+16), typ, payload, tsMs)
	b = binary.BigEndian.AppendUint32(b, scale)
	b = binary.BigEndian.AppendUint32(b, cts)
	return binary.BigEndian.AppendUint64(b, ticks)
}

// aiportVideoPayload is a video tag body: frame/codec byte, AVCPacketType (0 config, 1 frame),
// 24-bit cts in ms (4.3), then the record or the length-prefixed NALs.
func aiportVideoPayload(codecID byte, key, config bool, ctsMs uint32, data []byte) []byte {
	ft := byte(2)
	if key {
		ft = 1
	}
	pt := byte(1)
	if config {
		ft, pt = 1, 0 // a sequence header is always typed as a key frame (17 00 ..., 4.4)
	}
	b := make([]byte, 0, len(data)+5)
	b = append(b, ft<<4|codecID, pt, byte(ctsMs>>16), byte(ctsMs>>8), byte(ctsMs))
	return append(b, data...)
}

// aacConfigPayload is the AAC sequence header body: AF 00 + the two AudioSpecificConfig bytes
// (FUN_001c5720, 4.4).
func aacConfigPayload(asc []byte) []byte { return append([]byte{0xAF, 0x00}, asc...) }

// aacDataPayload is an AAC data body: AF 01 + the raw frame (the 7 ADTS bytes already skipped;
// FUN_001c6920).
func aacDataPayload(raw []byte) []byte { return append([]byte{0xAF, 0x01}, raw...) }

// ascFromADTS derives the AudioSpecificConfig from an ADTS header with the firmware's own
// arithmetic (FUN_001c5720, 4.4). For AAC-LC 48 kHz mono it yields 11 88.
func ascFromADTS(h []byte) [2]byte {
	return [2]byte{
		(h[2]>>3)&7 | ((h[2]>>6)+1)*8,
		byte(((h[2]&1)<<2|h[3]>>6)<<3) | byte((h[2]>>2)<<7),
	}
}

// aacASC48Mono is ascFromADTS of the encoder's output: AAC-LC (profile 1 in ADTS), index 3
// (48 kHz), one channel. Checked against ascFromADTS in the tests.
var aacASC48Mono = []byte{0x11, 0x88}

// opusRateTag maps the Opus sample rate to the config tag's last byte (FUN_001c58f0): 48000 -> 0,
// 24000 -> 1, 16000 -> 2, 12000 -> 3, 8000 -> 4, anything else 0 (4.4).
func opusRateTag(rate int) byte {
	switch rate {
	case 24000:
		return 1
	case 16000:
		return 2
	case 12000:
		return 3
	case 8000:
		return 4
	}
	return 0
}

// opusConfigPayload is the type-10 sequence header body: CF 00 03 <rateTag>.
func opusConfigPayload(rate int) []byte { return []byte{0xCF, 0x00, 0x03, opusRateTag(rate)} }

// mpmaBitrates is the onMpma payload's source: the encoder's adaptive-bitrate state (4.5). The
// real struct carries cs/sp/m {min,max,cur} and a target; see aiportMpma for what we put in.
type mpmaBitrates struct {
	cs, sp, m [3]uint32 // cur, max, min
	t         uint32
}

// aiportMpma is the onMpma object: {cs:{cur,max,min}, m:{...}, r:0, sp:{...}, t}, keys sorted.
func aiportMpma(v mpmaBitrates) Map {
	tri := func(a [3]uint32) Map { return Map{"cur": a[0], "max": a[1], "min": a[2]} }
	return Map{"cs": tri(v.cs), "m": tri(v.m), "r": 0.0, "sp": tri(v.sp), "t": v.t}
}

// uniformMpma is the value set we send: the channel's configured bitrate in every slot. The real
// values are the adaptive state of ubnt_streamer's encoder (cs camera-server, sp server-player,
// m motion adaptive, t target; their mapping to bitRateVbrMin/Max and the minClient/minMotion
// adaptive settings is inferred, spec 4.5 and open question 5). With no adaptation here, max,
// min and cur all equal the bitrate Protect configured, and t is that same target (the firmware
// takes t = cs.min when its +0xb0 flag is set).
func uniformMpma(bps uint32) mpmaBitrates {
	tri := [3]uint32{bps, bps, bps}
	return mpmaBitrates{cs: tri, sp: tri, m: tri, t: bps}
}

// aiportClockSync is the onClockSync object (4.5): streamClock is the timeline's last pts in ms
// minus the base (also the tag ts), streamClockBase the base itself (absolute monotonic ms),
// wallClock gettimeofday in ms.
func aiportClockSync(streamClock, base, wallMs int64) Map {
	return Map{"streamClock": float64(streamClock), "streamClockBase": float64(base), "wallClock": float64(wallMs)}
}

// aiportRtt is the onRtt object: {encDelay, id: 4-byte string, rttMillis} (4.5).
func aiportRtt(encDelayMs float64, id string, rttMs float64) Map {
	return Map{"encDelay": encDelayMs, "id": id, "rttMillis": rttMs}
}

// rttMillis is what the streamer computes for a type-4 feedback frame: (local ticks in the
// frame's timescale - the frame's timestamp) * 1000 / timescale, or -1 when the frame's
// timescale is 0 (6, FUN_001cd4d0).
func rttMillis(localNs int64, scale uint32, ts uint64) float64 {
	if scale == 0 {
		return -1
	}
	local := ticksOf(localNs, scale)
	return float64(int64(local)-int64(ts)) * 1000 / float64(scale)
}

// feedback is one console-to-streamer frame (6): u16 type, u16 len, payload.
type feedback struct {
	typ     uint16
	payload []byte
}

// parseFeedback takes whole frames off the front of buf and returns them with the unread rest
// (a partial frame stays buffered, as FUN_001d3020 does).
func parseFeedback(buf []byte) (frames []feedback, rest []byte) {
	for len(buf) >= 4 {
		n := int(binary.BigEndian.Uint16(buf[2:]))
		if len(buf) < 4+n {
			break
		}
		frames = append(frames, feedback{binary.BigEndian.Uint16(buf), buf[4 : 4+n : 4+n]})
		buf = buf[4+n:]
	}
	return frames, buf
}

// ascForRate is the AAC-LC mono AudioSpecificConfig for a sample rate: object type 2, the
// ISO 14496-3 frequency index, channel configuration 1. 48000 -> 11 88 (aacASC48Mono), 8000 ->
// 15 88 (what Protect's own ONVIF pull of a Hikvision camera carried, live 2026-11-12).
func ascForRate(rate int) []byte {
	idx := 3
	for i, r := range []int{96000, 88200, 64000, 48000, 44100, 32000, 24000, 22050, 16000, 12000, 11025, 8000, 7350} {
		if r == rate {
			idx = i
		}
	}
	v := 2<<11 | idx<<7 | 1<<3
	return []byte{byte(v >> 8), byte(v)}
}
