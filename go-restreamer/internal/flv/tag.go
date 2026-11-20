// SPDX-License-Identifier: AGPL-3.0-or-later

package flv

import (
	"encoding/binary"
	"time"
)

const (
	tagAudio  = 8
	tagVideo  = 9
	tagOpus   = 10 // Opus rides in its own tag type, not 8 (yi-protect FlvPush.cpp writeFlvTag(out, 10, ...))
	tagScript = 18

	// Timing trailer timescales "as captured from real cameras by unifi-cam-proxy" (Python
	// _TIMESCALE_*): 90 kHz for video, 11025 for script tags (and G.711, which the Python
	// never changed). Audio tags carry their own sample rate, as yi-protect's working
	// streamer writes them: AAC trailer 16000 (0x003E80) for its 16 kHz AAC, Opus 48000
	// (0x00BB80); ours is AAC at 32000.
	timescaleVideo = 90000
	timescaleOther = 11025
	timescaleOpus  = 48000

	// opusConfig is the type-10 config tag sent once at connection start, as real cameras
	// do (yi-protect FlvPush.cpp: opusCfg = {0xcf, 0x00, 0x03, 0x02}). What its bytes mean is
	// unknown (no rate field is evident: yi sent them for 48 kHz Opus after advertising 16 kHz,
	// and Protect's sources only show opusSampleRate in ChangeVideoSettings), so they are
	// sent unchanged for every rate. Data tags are the raw Opus packet (TOC byte first) with
	// no prefix.
	opusStepMS = 20 // Opus stamps are a 20 ms grid (one packet per 960 samples at 48 kHz)

	// Clock sync cadences (Python CLOCK_SYNC_INTERVAL_S / G3_SYNC_FALLBACK_S).
	syncInterval = 5 * time.Second
	syncG3Max    = 10 * time.Second
)

// mpma is the onMpma payload sent with every clock sync ("matching a real G3", Python _MPMA).
var mpma = Object{
	{"cs", Object{{"cur", 1500000}, {"max", 1500000}, {"min", 1500000}}},
	{"m", Object{{"cur", 1500000}, {"max", 1500000}, {"min", 1500000}}},
	{"r", 0},
	{"sp", Object{{"cur", 1500000}, {"max", 1500000}, {"min", 1500000}}},
	{"t", 75000.0},
}

// opusConfig: see above.
var opusConfig = []byte{0xCF, 0x00, 0x03, 0x02}

// header is the FLV header with audio+video flags (7 is what the Python writes: the byte
// is 5 in a plain FLV with both tracks; Protect's extendedFlv sets the 0x02 bit too) plus
// PreviousTagSize0.
func header() []byte {
	return []byte{'F', 'L', 'V', 1, 7, 0, 0, 0, 9, 0, 0, 0, 0}
}

// appendTag appends one FLV tag and its PreviousTagSize (Python _flv_tag).
func appendTag(b []byte, typ byte, payload []byte, ts int64) []byte {
	t := uint32(int32(ts))
	n := len(payload)
	b = append(b, typ, byte(n>>16), byte(n>>8), byte(n),
		byte(t>>16), byte(t>>8), byte(t), byte(t>>24), 0, 0, 0)
	b = append(b, payload...)
	return binary.BigEndian.AppendUint32(b, uint32(n+11))
}

// appendTrailer is the 16-byte timing trailer after every tag (Python timing_trailer):
// 0, the 24-bit timescale, 4 zero bytes, then wall time since the connection started in that
// timescale. The Python's deliberate difference from unifi-cam-proxy: elapsed is in the
// timescale the trailer declares (clock_sync.py's 10 us units made Protect 7.x drop frames).
func appendTrailer(b []byte, scale uint32, elapsed time.Duration) []byte {
	b = append(b, 0, byte(scale>>16), byte(scale>>8), byte(scale), 0, 0, 0, 0)
	return binary.BigEndian.AppendUint64(b, uint64(elapsed.Seconds()*float64(scale)))
}

// scale is the trailer timescale of a tag type on this connection.
func (w *writer) scale(typ byte) uint32 {
	switch {
	case typ == tagVideo:
		return timescaleVideo
	case typ == tagOpus && w.opusRate > 0:
		return w.opusRate // the rate Protect asked for: it reads this tag's clock in this scale
	case typ == tagOpus:
		return timescaleOpus
	case typ == tagAudio && w.aacRate > 0:
		return w.aacRate
	}
	return timescaleOther
}

// writer builds one connection's bytes in onvif_push.py's order. It does no I/O: each method
// returns the bytes for a single write.
type writer struct {
	start, last time.Time
	g3          bool
	sync        bool // legacy mode: a sync is owed after the first video frame
	early       bool
	opusRate    uint32 // Opus sample rate: the type-10 trailer timescale (0: timescaleOpus)
	aacRate     uint32 // AAC sample rate: the audio tags' trailer timescale (0: G.711, timescaleOther)
}

func (w *writer) tag(now time.Time, typ byte, payload []byte, ts int64) []byte {
	b := appendTag(nil, typ, payload, ts)
	return appendTrailer(b, w.scale(typ), now.Sub(w.start))
}

// begin starts a connection: header then the clock bookkeeping Python's header() does.
func (w *writer) begin(now time.Time) []byte {
	w.start, w.last = now, now
	return header()
}

// initialSync is the G3 connection-start pair after onMetaData and before the codec configs:
// onMpma then onClockSync at stream time 0 (Python initial_sync).
func (w *writer) initialSync(now time.Time) []byte {
	w.last = now
	b := w.tag(now, tagScript, ScriptPayload("onMpma", mpma), 0)
	return append(b, w.tag(now, tagScript, ScriptPayload("onClockSync", Object{
		{"streamClock", 0}, {"streamClockBase", 0}, {"wallClock", float64(now.UnixMilli())}}), 0)...)
}

// syncIfDue returns the clock sync to write before a tag, or nil (Python sync_if_due: G3 syncs
// before every key frame with a 10 s fallback; the old mode every 5 s and once right after
// the first video frame, since Protect drops a recording file closed before its first sync).
func (w *writer) syncIfDue(now time.Time, ts int64, keyframe bool) []byte {
	var due bool
	if w.g3 {
		due = keyframe || now.Sub(w.last) >= syncG3Max
	} else {
		due = w.sync || now.Sub(w.last) >= syncInterval
	}
	if !due {
		return nil
	}
	w.sync, w.last = false, now
	b := w.tag(now, tagScript, ScriptPayload("onClockSync", Object{
		{"streamClock", ts}, {"streamClockBase", 0}, {"wallClock", float64(now.UnixMilli())}}), ts)
	return append(b, w.tag(now, tagScript, ScriptPayload("onMpma", mpma), 0)...)
}

// afterVideo arms the legacy mode's early sync after the first AVC NALU frame.
func (w *writer) afterVideo(payload []byte) {
	if !w.g3 && w.early && len(payload) > 1 && payload[1] == 1 {
		w.early, w.sync = false, true
	}
}

// Video payloads. AVCPacketType 0 is the sequence header, 1 a frame (composition time 0:
// the cameras send no B-frames, so PTS equals DTS).
func videoConfigPayload(avcc []byte) []byte {
	return append([]byte{0x17, 0, 0, 0, 0}, avcc...)
}

func videoFramePayload(key bool, avcc []byte) []byte {
	ft := byte(0x27)
	if key {
		ft = 0x17
	}
	return append([]byte{ft, 1, 0, 0, 0}, avcc...)
}
