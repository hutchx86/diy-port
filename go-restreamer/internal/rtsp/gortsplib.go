// SPDX-License-Identifier: AGPL-3.0-or-later

package rtsp

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"time"

	"github.com/bluenviron/gortsplib/v4"
	"github.com/bluenviron/gortsplib/v4/pkg/base"
	"github.com/bluenviron/gortsplib/v4/pkg/description"
	"github.com/bluenviron/gortsplib/v4/pkg/format"
	"github.com/bluenviron/mediacommon/v2/pkg/codecs/h264"
	"github.com/bluenviron/mediacommon/v2/pkg/codecs/h265"
	"github.com/pion/rtp"

	"diyport/go-restreamer/internal/media"
)

// GortsplibDialer is the production Dialer: TCP interleaved (as the AI Port's own
// `rtspsrc protocols=tcp`; UDP loses packets on a busy Wi-Fi/Mac), one H.264/H.265 video
// track and the first AAC, G.711 or Opus audio track.
func GortsplibDialer(ctx context.Context, rawURL string, sink Sink) error {
	u, err := base.ParseURL(rawURL)
	if err != nil {
		return fmt.Errorf("bad rtsp url")
	}
	tcp := gortsplib.TransportTCP
	c := &gortsplib.Client{
		Scheme: u.Scheme, Host: u.Host, Transport: &tcp,
		ReadTimeout: 10 * time.Second, WriteTimeout: 10 * time.Second,
	}
	if err := c.Start2(); err != nil {
		return err
	}
	defer c.Close()

	desc, _, err := c.Describe(u)
	if err != nil {
		return err
	}
	s := &session{c: c, sink: sink}
	if err := s.setup(desc); err != nil {
		return err
	}
	if _, err := c.Play(nil); err != nil {
		return err
	}
	waitErr := make(chan error, 1)
	go func() { waitErr <- c.Wait() }()
	select {
	case <-ctx.Done():
		return nil // deferred Close ends the client
	case err := <-waitErr:
		return err
	}
}

// session converts one client's RTP into media types.
type session struct {
	c    *gortsplib.Client
	sink Sink

	tracks   media.Tracks
	ready    bool // tracks sent; nothing is forwarded before
	needSPS  bool // parameter sets weren't in the SDP: wait for them in-band
	h265     bool
	spsSeen  []byte
	ppsSeen  []byte
	vpsSeen  []byte
	videoMed *description.Media
	audioMed *description.Media
	audioFmt format.Format
}

func (s *session) setup(desc *description.Session) error {
	var h264f *format.H264
	var h265f *format.H265
	vm := desc.FindFormat(&h264f)
	if vm == nil {
		if vm = desc.FindFormat(&h265f); vm == nil {
			return errors.New("no H.264 or H.265 video track")
		}
	}
	s.videoMed = vm
	if _, err := s.c.Setup(desc.BaseURL, vm, 0, 0); err != nil {
		return err
	}
	if h264f != nil {
		s.tracks.Video = media.H264
		s.tracks.SPS, s.tracks.PPS = h264f.SPS, h264f.PPS
		dec, err := h264f.CreateDecoder()
		if err != nil {
			return err
		}
		s.c.OnPacketRTP(vm, h264f, func(pkt *rtp.Packet) { s.onVideo(pkt, func() ([][]byte, error) { return dec.Decode(pkt) }) })
	} else {
		s.h265 = true
		s.tracks.Video = media.H265
		s.tracks.VPS, s.tracks.SPS, s.tracks.PPS = h265f.VPS, h265f.SPS, h265f.PPS
		dec, err := h265f.CreateDecoder()
		if err != nil {
			return err
		}
		s.c.OnPacketRTP(vm, h265f, func(pkt *rtp.Packet) { s.onVideo(pkt, func() ([][]byte, error) { return dec.Decode(pkt) }) })
	}
	s.applySPS()
	s.needSPS = s.tracks.Width == 0

	if err := s.setupAudio(desc); err != nil {
		return err
	}
	if !s.needSPS {
		s.send()
	}
	return nil
}

// setupAudio picks AAC over G.711 over Opus; a camera that offers several gets the one
// Protect takes without a transcode (internal/audio converts the rest).
func (s *session) setupAudio(desc *description.Session) error {
	var aac *format.MPEG4Audio
	var g711 *format.G711
	var opus *format.Opus
	var m *description.Media
	if m = desc.FindFormat(&aac); m != nil {
		cfg, err := aac.Config.Marshal()
		if err != nil {
			return err
		}
		s.tracks.Audio, s.tracks.AudioRate, s.tracks.AudioChannels = media.AAC, aac.Config.SampleRate, aac.Config.ChannelCount
		s.tracks.AudioConfig = cfg
		dec, err := aac.CreateDecoder()
		if err != nil {
			return err
		}
		s.audioFmt = aac
		s.audioMed = m
		if _, err := s.c.Setup(desc.BaseURL, m, 0, 0); err != nil {
			return err
		}
		s.c.OnPacketRTP(m, aac, func(pkt *rtp.Packet) {
			aus, err := dec.Decode(pkt)
			if err != nil || !s.ready {
				return
			}
			pts, ok := s.pts(m, pkt)
			if !ok {
				return
			}
			// Several AUs in one packet: spread them one frame (1024 samples) apart.
			for i, au := range aus {
				d := time.Duration(i) * 1024 * time.Second / time.Duration(aac.Config.SampleRate)
				s.sink.Audio(media.AudioFrame{PTS: pts + d, Data: au})
			}
		})
		return nil
	}
	var codec media.Codec
	if m = desc.FindFormat(&g711); m != nil {
		codec = media.PCMA
		if g711.MULaw {
			codec = media.PCMU
		}
		s.tracks.AudioRate, s.tracks.AudioChannels = g711.SampleRate, g711.ChannelCount
		s.audioFmt = g711
	} else if m = desc.FindFormat(&opus); m != nil {
		codec = media.Opus
		s.tracks.AudioRate, s.tracks.AudioChannels = 48000, opus.ChannelCount
		s.audioFmt = opus
	} else {
		return nil
	}
	s.tracks.Audio = codec
	s.audioMed = m
	if _, err := s.c.Setup(desc.BaseURL, m, 0, 0); err != nil {
		return err
	}
	// One G.711 or Opus frame (or a run of samples) per packet: the payload is the data.
	s.c.OnPacketRTP(m, s.audioFmt, func(pkt *rtp.Packet) {
		if !s.ready || len(pkt.Payload) == 0 {
			return
		}
		if pts, ok := s.pts(m, pkt); ok {
			s.sink.Audio(media.AudioFrame{PTS: pts, Data: append([]byte(nil), pkt.Payload...)})
		}
	})
	return nil
}

func (s *session) pts(m *description.Media, pkt *rtp.Packet) (time.Duration, bool) {
	return s.c.PacketPTS(m, pkt) //nolint:staticcheck // PTS as a Duration is what media wants
}

func (s *session) send() {
	s.ready = true
	s.sink.Tracks(s.tracks)
}

// applySPS fills Width/Height/FPS from the current SPS.
func (s *session) applySPS() {
	if len(s.tracks.SPS) == 0 {
		return
	}
	if s.h265 {
		var sps h265.SPS
		if sps.Unmarshal(s.tracks.SPS) == nil {
			s.tracks.Width, s.tracks.Height, s.tracks.FPS = sps.Width(), sps.Height(), sps.FPS()
		}
		return
	}
	var sps h264.SPS
	if sps.Unmarshal(s.tracks.SPS) == nil {
		s.tracks.Width, s.tracks.Height, s.tracks.FPS = sps.Width(), sps.Height(), sps.FPS()
	}
}

func (s *session) onVideo(pkt *rtp.Packet, decode func() ([][]byte, error)) {
	// PTS first, on every packet: gortsplib only starts a track's clock at a packet that is
	// itself a random-access point (SPS, PPS, IDR), which for a fragmented IDR is its first
	// fragment, not the last one that completes the AU. Until then nothing is usable anyway.
	pts, ok := s.pts(s.videoMed, pkt)
	if !ok {
		return
	}
	// ErrMorePacketsNeeded and a non-starting fragment after loss are routine: skip.
	au, err := decode()
	if err != nil {
		return
	}
	// In-band parameter sets: a change (new resolution after a camera setting change) is
	// new Tracks, and when the SDP had none the first ones make the Tracks.
	changed := false
	for _, n := range au {
		if len(n) == 0 {
			continue
		}
		var kind string
		if s.h265 {
			switch h265.NALUType((n[0] >> 1) & 0x3f) {
			case h265.NALUType_VPS_NUT:
				kind = "vps"
			case h265.NALUType_SPS_NUT:
				kind = "sps"
			case h265.NALUType_PPS_NUT:
				kind = "pps"
			}
		} else {
			switch h264.NALUType(n[0] & 0x1f) {
			case h264.NALUTypeSPS:
				kind = "sps"
			case h264.NALUTypePPS:
				kind = "pps"
			}
		}
		set := func(dst *[]byte) {
			if !bytes.Equal(*dst, n) {
				*dst = append([]byte(nil), n...)
				changed = true
			}
		}
		switch kind {
		case "vps":
			set(&s.tracks.VPS)
		case "sps":
			set(&s.tracks.SPS)
		case "pps":
			set(&s.tracks.PPS)
		}
	}
	if changed {
		s.applySPS()
		if s.tracks.Width > 0 && len(s.tracks.PPS) > 0 {
			s.needSPS = false
			s.send()
		}
	}
	if !s.ready {
		return
	}
	idr := false
	if s.h265 {
		idr = h265.IsRandomAccess(au)
	} else {
		idr = h264.IsRandomAccess(au)
	}
	s.sink.Video(media.VideoAU{PTS: pts, NALUs: au, IDR: idr})
}
