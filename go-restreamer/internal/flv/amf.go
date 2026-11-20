// SPDX-License-Identifier: AGPL-3.0-or-later

// Package flv writes what Protect's extendedFlv ingest takes: FLV with header flags 7, a
// 16-byte timing trailer after every tag, onMetaData with a streamName, and onClockSync/onMpma
// script tags. The wire format is onvif_push.py's (ExtendedFlvWriter), which Protect is proven
// to accept; comments cite it.
package flv

import (
	"encoding/binary"
	"math"
	"sort"
)

// Prop is one AMF0 object entry; objects keep their order, as the Python's dicts do.
type Prop struct {
	Key string
	Val any
}

// Object is an AMF0 anonymous object (marker 3). ECMAArray is the same entries as an
// ECMA array (marker 8), which is how ffmpeg writes onMetaData.
type (
	Object    []Prop
	ECMAArray []Prop
)

// Map is an AMF0 object written with its keys sorted byte-wise, the order ubnt_streamer's
// std::map<string, variant> serializer produces (decompiled FUN_00200f80, docs/aiport-onvif-push-spec.md
// 4.1). Values may be Map, bool, string or any numeric type (all numbers go out as doubles).
type Map map[string]any

func appendString(b []byte, s string) []byte {
	b = binary.BigEndian.AppendUint16(b, uint16(len(s)))
	return append(b, s...)
}

// AppendAMF appends v as AMF0: float64/int/uint (number), bool, string, Object, ECMAArray.
func AppendAMF(b []byte, v any) []byte {
	switch x := v.(type) {
	case float64:
		b = append(b, 0x00)
		return binary.BigEndian.AppendUint64(b, math.Float64bits(x))
	case int:
		return AppendAMF(b, float64(x))
	case int32:
		return AppendAMF(b, float64(x))
	case uint32:
		return AppendAMF(b, float64(x))
	case float32:
		return AppendAMF(b, float64(x))
	case Map:
		keys := make([]string, 0, len(x))
		for k := range x {
			keys = append(keys, k)
		}
		sort.Strings(keys) // Go compares strings byte-wise, as std::string's operator< does
		b = append(b, 0x03)
		for _, k := range keys {
			b = AppendAMF(appendString(b, k), x[k])
		}
		return append(b, 0x00, 0x00, 0x09)
	case int64:
		return AppendAMF(b, float64(x))
	case uint64:
		return AppendAMF(b, float64(x))
	case bool:
		if x {
			return append(b, 0x01, 0x01)
		}
		return append(b, 0x01, 0x00)
	case string:
		b = append(b, 0x02)
		return appendString(b, x)
	case Object:
		b = append(b, 0x03)
		for _, p := range x {
			b = AppendAMF(appendString(b, p.Key), p.Val)
		}
		return append(b, 0x00, 0x00, 0x09)
	case ECMAArray:
		b = append(b, 0x08)
		b = binary.BigEndian.AppendUint32(b, uint32(len(x)))
		for _, p := range x {
			b = AppendAMF(appendString(b, p.Key), p.Val)
		}
		return append(b, 0x00, 0x00, 0x09)
	}
	panic("flv: unsupported AMF value")
}

// ScriptPayload is a script tag's body: the name as an AMF string, then the value.
func ScriptPayload(name string, v any) []byte {
	return AppendAMF(append([]byte{0x02}, appendStringBytes(name)...), v)
}

func appendStringBytes(s string) []byte { return appendString(nil, s) }
