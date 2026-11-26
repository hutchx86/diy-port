// SPDX-License-Identifier: AGPL-3.0-or-later

package flv

import (
	"encoding/binary"
	"errors"
)

// SplitAnnexB splits an Annex-B byte stream into NAL units (start codes removed; both
// 3- and 4-byte codes). Trailing zero bytes before the next start code are dropped.
func SplitAnnexB(b []byte) [][]byte {
	var nalus [][]byte
	start := -1
	i := 0
	for i+2 < len(b) {
		if b[i] == 0 && b[i+1] == 0 && b[i+2] == 1 {
			if start >= 0 {
				nalus = append(nalus, trimZeros(b[start:i]))
			}
			i += 3
			start = i
			continue
		}
		i++
	}
	if start >= 0 && start < len(b) {
		nalus = append(nalus, trimZeros(b[start:]))
	}
	return nalus
}

func trimZeros(b []byte) []byte {
	for len(b) > 0 && b[len(b)-1] == 0 {
		b = b[:len(b)-1]
	}
	return b
}

// AnnexBToAVCC converts an Annex-B access unit to 4-byte length-prefixed NAL units.
func AnnexBToAVCC(b []byte) []byte { return NALUsToAVCC(SplitAnnexB(b)) }

// NALUsToAVCC length-prefixes NAL units (4-byte lengths, the AVCDecoderConfigurationRecord's
// lengthSizeMinusOne = 3). Access unit delimiters (type 9) are dropped: Protect gets them
// from nothing and the decoder config says where frames start.
func NALUsToAVCC(nalus [][]byte) []byte {
	n := 0
	for _, u := range nalus {
		n += 4 + len(u)
	}
	out := make([]byte, 0, n)
	for _, u := range nalus {
		if len(u) == 0 || u[0]&0x1F == 9 {
			continue
		}
		out = binary.BigEndian.AppendUint32(out, uint32(len(u)))
		out = append(out, u...)
	}
	return out
}

// AVCDecoderConfigurationRecord builds the avcC box body from one SPS and PPS (raw NAL units).
// For High profiles (100, 110, 122, 244, 44, ...) the spec's chroma/bit-depth extension is
// appended as 4:2:0 8-bit, which is what a camera H.264 stream is; ffmpeg's muxer writes the
// same bytes for such streams.
func AVCDecoderConfigurationRecord(sps, pps []byte) ([]byte, error) {
	if len(sps) < 4 || len(pps) == 0 {
		return nil, errors.New("flv: SPS/PPS missing")
	}
	b := []byte{1, sps[1], sps[2], sps[3], 0xFF, 0xE1}
	b = binary.BigEndian.AppendUint16(b, uint16(len(sps)))
	b = append(b, sps...)
	b = append(b, 1)
	b = binary.BigEndian.AppendUint16(b, uint16(len(pps)))
	b = append(b, pps...)
	switch sps[1] {
	case 100, 110, 122, 244, 44, 83, 86, 118, 128, 138, 139, 134:
		b = append(b, 0xFC|1, 0xF8, 0xF8, 0) // chroma 4:2:0, luma/chroma 8-bit, no SPS ext
	}
	return b, nil
}
