// SPDX-License-Identifier: AGPL-3.0-or-later

package transcode

import "bytes"

// auSplitter turns ffmpeg's raw Annex-B H.264 output into access units. The encoder's output
// has no timing, so ffmpeg is told to insert an access unit delimiter (h264_metadata=aud=insert)
// at the start of each, and an AU ends where the next AUD begins. That holds back the last AU
// by one frame; the alternative, guessing from slice headers, breaks on multi-slice frames.
type auSplitter struct {
	buf []byte
	cur [][]byte
	// earlyAUD: the AUD at the buffer's tail already closed the AU before its own bytes were
	// complete (see write), so its nal() call mustn't close another.
	earlyAUD bool
	emit     func(nalus [][]byte)
}

var startCode = []byte{0, 0, 1}

// write adds bytes from ffmpeg. NALs are complete only once the next start code is seen, so
// the tail after the last start code stays buffered.
func (s *auSplitter) write(p []byte) {
	s.buf = append(s.buf, p...)
	var starts []int // index of each NAL's first byte (just past its start code)
	for i := 0; ; {
		j := bytes.Index(s.buf[i:], startCode)
		if j < 0 {
			break
		}
		starts = append(starts, i+j+3)
		i += j + 3
	}
	for k := 0; k+1 < len(starts); k++ {
		end := starts[k+1] - 3
		// A 4-byte start code leaves a zero on the previous NAL; trailing zeros aren't NAL data.
		nal := bytes.TrimRight(s.buf[starts[k]:end], "\x00")
		s.nal(append([]byte(nil), nal...))
	}
	if len(starts) > 0 {
		last := starts[len(starts)-1] - 3
		// The AU ends at the next AUD's start code, not when that 2-byte NAL is complete:
		// don't hold a frame back for the one after it.
		if !s.earlyAUD && starts[len(starts)-1] < len(s.buf) && s.buf[starts[len(starts)-1]]&0x1f == 9 {
			s.flush()
			s.earlyAUD = true
		}
		s.buf = append(s.buf[:0], s.buf[last:]...)
	} else if len(s.buf) > 4<<20 {
		s.buf = s.buf[:0] // no start code in 4 MB: garbage, don't grow forever
	}
}

func (s *auSplitter) nal(n []byte) {
	if len(n) == 0 {
		return
	}
	if n[0]&0x1f == 9 { // AUD: previous AU is complete
		if !s.earlyAUD {
			s.flush()
		}
		s.earlyAUD = false
		return
	}
	s.cur = append(s.cur, n)
}

// annexB serializes NALUs with 4-byte start codes.
func annexB(dst []byte, nalus ...[]byte) []byte {
	for _, n := range nalus {
		if len(n) == 0 {
			continue
		}
		dst = append(dst, 0, 0, 0, 1)
		dst = append(dst, n...)
	}
	return dst
}

func (s *auSplitter) flush() {
	if len(s.cur) > 0 {
		s.emit(s.cur)
		s.cur = nil
	}
}
