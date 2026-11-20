// SPDX-License-Identifier: AGPL-3.0-or-later

package rtsp

import (
	"bytes"
	"fmt"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"testing"
	"time"

	"github.com/bluenviron/gortsplib/v4"
	"github.com/bluenviron/gortsplib/v4/pkg/base"
	"github.com/bluenviron/gortsplib/v4/pkg/description"
	"github.com/bluenviron/gortsplib/v4/pkg/format"
	"github.com/bluenviron/gortsplib/v4/pkg/format/rtph264"

	"diyport/go-restreamer/internal/media"
)

type testServer struct{ stream *gortsplib.ServerStream }

func (h *testServer) OnDescribe(*gortsplib.ServerHandlerOnDescribeCtx) (*base.Response, *gortsplib.ServerStream, error) {
	return &base.Response{StatusCode: base.StatusOK}, h.stream, nil
}
func (h *testServer) OnSetup(*gortsplib.ServerHandlerOnSetupCtx) (*base.Response, *gortsplib.ServerStream, error) {
	return &base.Response{StatusCode: base.StatusOK}, h.stream, nil
}
func (h *testServer) OnPlay(*gortsplib.ServerHandlerOnPlayCtx) (*base.Response, error) {
	return &base.Response{StatusCode: base.StatusOK}, nil
}

// h264AUs cuts an ffmpeg Annex-B H.264 clip into access units at AUD boundaries.
func h264AUs(t *testing.T, data []byte) (aus [][][]byte, sps, pps []byte) {
	var cur [][]byte
	for _, part := range bytes.Split(data, []byte{0, 0, 1}) {
		part = bytes.TrimRight(part, "\x00")
		if len(part) == 0 {
			continue
		}
		switch part[0] & 0x1f {
		case 9:
			if len(cur) > 0 {
				aus = append(aus, cur)
			}
			cur = nil
			continue
		case 7:
			sps = part
		case 8:
			pps = part
		}
		cur = append(cur, part)
	}
	if len(cur) > 0 {
		aus = append(aus, cur)
	}
	return
}

// A real RTSP server (gortsplib's own) streams a short H.264 clip over TCP; the pool's real
// dialer must turn it into Tracks with the SPS's size and IDR-flagged AUs, shared by two
// subscribers on one session.
func TestGortsplibDialerAgainstLocalServer(t *testing.T) {
	ff, err := exec.LookPath("ffmpeg")
	if err != nil {
		t.Skip("no ffmpeg to make a clip")
	}
	clip := filepath.Join(t.TempDir(), "c.h264")
	if out, err := exec.Command(ff, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=20",
		"-t", "2", "-c:v", "libx264", "-g", "10", "-bf", "0", "-bsf:v", "h264_metadata=aud=insert", "-f", "h264", clip).CombinedOutput(); err != nil {
		t.Skipf("no libx264: %v %s", err, out)
	}
	data, _ := os.ReadFile(clip)
	aus, sps, pps := h264AUs(t, data)
	if len(aus) < 20 || sps == nil || pps == nil {
		t.Fatalf("clip: %d AUs", len(aus))
	}

	l, _ := net.Listen("tcp", "127.0.0.1:0")
	addr := l.Addr().String()
	l.Close()
	h := &testServer{}
	srv := &gortsplib.Server{Handler: h, RTSPAddress: addr}
	if err := srv.Start(); err != nil {
		t.Fatal(err)
	}
	defer srv.Close()
	desc := &description.Session{Medias: []*description.Media{{
		Type:    description.MediaTypeVideo,
		Formats: []format.Format{&format.H264{PayloadTyp: 96, SPS: sps, PPS: pps, PacketizationMode: 1}},
	}}}
	h.stream = &gortsplib.ServerStream{Server: srv, Desc: desc}
	if err := h.stream.Initialize(); err != nil {
		t.Fatal(err)
	}
	defer h.stream.Close()
	stop := make(chan struct{})
	defer close(stop)
	go func() {
		enc := &rtph264.Encoder{PayloadType: 96, PacketizationMode: 1}
		if enc.Init() != nil {
			return
		}
		ts := uint32(0)
		for i := 0; ; i = (i + 1) % len(aus) {
			pkts, _ := enc.Encode(aus[i])
			for _, p := range pkts {
				p.Timestamp = ts
				_ = h.stream.WritePacketRTP(desc.Medias[0], p)
			}
			ts += 4500
			select {
			case <-stop:
				return
			case <-time.After(20 * time.Millisecond):
			}
		}
	}()

	p := NewPool(nil, quiet())
	url := fmt.Sprintf("rtsp://user:secret@%s/cam", addr)
	a, b := &rec{}, &rec{}
	p.Acquire(url, a)
	p.Acquire(url, b)
	defer p.Release(url, a)
	defer p.Release(url, b)
	waitFor(t, "video", func() bool { _, v, _ := b.snap(); return len(v) >= 15 })
	if n, _, _ := a.snap(); n != 1 {
		t.Fatalf("tracks callbacks = %d", n)
	}
	if st := p.Stats(); len(st) != 1 || st[0].Sessions != 1 || st[0].Subscribers != 2 || !st[0].Connected {
		t.Fatalf("stats = %+v", st)
	}
	_, v, _ := b.snap()
	for i := 1; i < len(v); i++ {
		if v[i] < v[i-1] {
			t.Fatalf("pts not monotonic: %v", v)
		}
	}
	var tr media.Tracks
	rt := &trackGrab{out: &tr}
	p.Acquire(url, rt) // late joiner: gets the live tracks synchronously
	p.Release(url, rt)
	if tr.Video != media.H264 || tr.Width != 320 || tr.Height != 240 {
		t.Fatalf("tracks = %+v", tr)
	}
}

type trackGrab struct {
	rec
	out *media.Tracks
}

func (g *trackGrab) OnTracks(t media.Tracks) { *g.out = t }
