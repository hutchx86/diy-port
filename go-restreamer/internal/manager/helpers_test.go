// SPDX-License-Identifier: AGPL-3.0-or-later

package manager

import (
	"bytes"
	"encoding/binary"
	"fmt"
	"io"
	"log/slog"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/bluenviron/gortsplib/v4"
	"github.com/bluenviron/gortsplib/v4/pkg/base"
	"github.com/bluenviron/gortsplib/v4/pkg/description"
	"github.com/bluenviron/gortsplib/v4/pkg/format"
	"github.com/bluenviron/gortsplib/v4/pkg/format/rtph264"
	"github.com/bluenviron/mediacommon/v2/pkg/codecs/mpeg4audio"
	"github.com/pion/rtp"
)

func needTool(t *testing.T, name string) string {
	t.Helper()
	p, err := exec.LookPath(name)
	if err != nil {
		t.Skipf("no %s", name)
	}
	return p
}

// syncBuf is a log sink the test can read while the restreamer writes.
type syncBuf struct {
	mu sync.Mutex
	b  bytes.Buffer
}

func (s *syncBuf) Write(p []byte) (int, error) { s.mu.Lock(); defer s.mu.Unlock(); return s.b.Write(p) }
func (s *syncBuf) String() string              { s.mu.Lock(); defer s.mu.Unlock(); return s.b.String() }

func testLog(w io.Writer) *slog.Logger {
	return slog.New(slog.NewTextHandler(w, &slog.HandlerOptions{Level: slog.LevelDebug}))
}

// h264AUs cuts an ffmpeg Annex-B H.264 clip into access units at AUD boundaries.
func h264AUs(data []byte) (aus [][][]byte, sps, pps []byte) {
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

// videoSource is what a test RTSP server plays on a loop.
type videoSource struct {
	aus    [][][]byte
	format format.Format
	encode func([][]byte) ([]*rtp.Packet, error)
}

func h264Source(t *testing.T) *videoSource {
	ff := needTool(t, "ffmpeg")
	clip := filepath.Join(t.TempDir(), "c.h264")
	if out, err := exec.Command(ff, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=20",
		"-t", "2", "-c:v", "libx264", "-g", "10", "-bf", "0", "-bsf:v", "h264_metadata=aud=insert", "-f", "h264", clip).CombinedOutput(); err != nil {
		t.Skipf("no libx264: %v %s", err, out)
	}
	data, _ := os.ReadFile(clip)
	aus, sps, pps := h264AUs(data)
	if len(aus) < 20 || sps == nil || pps == nil {
		t.Fatalf("clip: %d AUs", len(aus))
	}
	enc := &rtph264.Encoder{PayloadType: 96, PacketizationMode: 1}
	if err := enc.Init(); err != nil {
		t.Fatal(err)
	}
	return &videoSource{aus: aus, encode: enc.Encode,
		format: &format.H264{PayloadTyp: 96, SPS: sps, PPS: pps, PacketizationMode: 1}}
}

// aacFrames makes AAC-LC (32 kHz mono) access units with ffmpeg.
func aacFrames(t *testing.T) (*format.MPEG4Audio, [][]byte) {
	ff := needTool(t, "ffmpeg")
	clip := filepath.Join(t.TempDir(), "a.aac")
	if out, err := exec.Command(ff, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=32000",
		"-t", "2", "-ac", "1", "-c:a", "aac", "-f", "adts", clip).CombinedOutput(); err != nil {
		t.Skipf("no aac encoder: %v %s", err, out)
	}
	data, _ := os.ReadFile(clip)
	var pk mpeg4audio.ADTSPackets
	if err := pk.Unmarshal(data); err != nil || len(pk) < 10 {
		t.Fatalf("adts: %v (%d)", err, len(pk))
	}
	var aus [][]byte
	for _, p := range pk {
		aus = append(aus, p.AU)
	}
	return &format.MPEG4Audio{PayloadTyp: 97, SizeLength: 13, IndexLength: 3, IndexDeltaLength: 3,
		Config: &mpeg4audio.AudioSpecificConfig{Type: mpeg4audio.ObjectTypeAACLC, SampleRate: 32000, ChannelCount: 1}}, aus
}

type testHandler struct{ stream *gortsplib.ServerStream }

func (h *testHandler) OnDescribe(*gortsplib.ServerHandlerOnDescribeCtx) (*base.Response, *gortsplib.ServerStream, error) {
	return &base.Response{StatusCode: base.StatusOK}, h.stream, nil
}
func (h *testHandler) OnSetup(*gortsplib.ServerHandlerOnSetupCtx) (*base.Response, *gortsplib.ServerStream, error) {
	return &base.Response{StatusCode: base.StatusOK}, h.stream, nil
}
func (h *testHandler) OnPlay(*gortsplib.ServerHandlerOnPlayCtx) (*base.Response, error) {
	return &base.Response{StatusCode: base.StatusOK}, nil
}

// serveRTSP runs a gortsplib server playing vs (20 fps) and, if audio is non-nil, AAC, on a
// loop until the test ends. It returns the camera URL, with credentials the logs must not show.
func serveRTSP(t *testing.T, vs *videoSource, afmt *format.MPEG4Audio, aframes [][]byte) string {
	l, _ := net.Listen("tcp", "127.0.0.1:0")
	addr := l.Addr().String()
	l.Close()
	h := &testHandler{}
	srv := &gortsplib.Server{Handler: h, RTSPAddress: addr}
	if err := srv.Start(); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(srv.Close)
	desc := &description.Session{Medias: []*description.Media{{Type: description.MediaTypeVideo, Formats: []format.Format{vs.format}}}}
	if afmt != nil {
		desc.Medias = append(desc.Medias, &description.Media{Type: description.MediaTypeAudio, Formats: []format.Format{afmt}})
	}
	h.stream = &gortsplib.ServerStream{Server: srv, Desc: desc}
	if err := h.stream.Initialize(); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(h.stream.Close)
	stop := make(chan struct{})
	t.Cleanup(func() { close(stop) })
	go func() {
		ts := uint32(0)
		for i := 0; ; i = (i + 1) % len(vs.aus) {
			pkts, _ := vs.encode(vs.aus[i])
			for _, p := range pkts {
				p.Timestamp = ts
				_ = h.stream.WritePacketRTP(desc.Medias[0], p)
			}
			ts += 4500
			select {
			case <-stop:
				return
			case <-time.After(50 * time.Millisecond):
			}
		}
	}()
	if afmt != nil {
		enc, err := afmt.CreateEncoder()
		if err != nil {
			t.Fatal(err)
		}
		go func() {
			ts := uint32(0)
			for i := 0; ; i = (i + 1) % len(aframes) {
				pkts, _ := enc.Encode([][]byte{aframes[i]})
				for _, p := range pkts {
					p.Timestamp = ts
					_ = h.stream.WritePacketRTP(desc.Medias[1], p)
				}
				ts += 1024
				select {
				case <-stop:
					return
				case <-time.After(32 * time.Millisecond):
				}
			}
		}()
	}
	return fmt.Sprintf("rtsp://user:secret@%s/cam", addr)
}

// sink is a fake Protect ingest: a TCP listener collecting whatever one push sends.
type sink struct {
	l    net.Listener
	mu   sync.Mutex
	data []byte
	conn int
}

func newSink(t *testing.T) *sink {
	l, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	s := &sink{l: l}
	t.Cleanup(func() { l.Close() })
	go func() {
		for {
			c, err := l.Accept()
			if err != nil {
				return
			}
			s.mu.Lock()
			s.conn++
			s.data = nil
			s.mu.Unlock()
			go func() {
				defer c.Close()
				buf := make([]byte, 64<<10)
				for {
					n, err := c.Read(buf)
					s.mu.Lock()
					s.data = append(s.data, buf[:n]...)
					s.mu.Unlock()
					if err != nil {
						return
					}
				}
			}()
		}
	}()
	return s
}

func (s *sink) host() string { h, _, _ := net.SplitHostPort(s.l.Addr().String()); return h }
func (s *sink) port() int    { return s.l.Addr().(*net.TCPAddr).Port }
func (s *sink) snapshot() ([]byte, int) {
	s.mu.Lock()
	defer s.mu.Unlock()
	return append([]byte(nil), s.data...), s.conn
}

// flvTags walks a pushed stream: the 13-byte header, then per tag 11 + payload + 4 +
// the 16-byte trailer. It returns the stream with the trailers stripped (a plain FLV
// ffprobe can read) and the number of tags by type.
func flvTags(data []byte) (plain []byte, counts map[byte]int, ok bool) {
	counts = map[byte]int{}
	if len(data) < 13 || string(data[:3]) != "FLV" {
		return nil, counts, false
	}
	plain = append(plain, data[:13]...)
	pos := 13
	for pos+11 <= len(data) {
		n := int(data[pos+1])<<16 | int(data[pos+2])<<8 | int(data[pos+3])
		end := pos + 11 + n + 4 + 16
		if end > len(data) {
			break
		}
		if prev := binary.BigEndian.Uint32(data[pos+11+n:]); int(prev) != n+11 {
			return nil, counts, false
		}
		plain = append(plain, data[pos:pos+11+n+4]...)
		counts[data[pos]]++
		pos = end
	}
	return plain, counts, true
}

func waitFor(t *testing.T, what string, d time.Duration, f func() bool) {
	t.Helper()
	deadline := time.Now().Add(d)
	for time.Now().Before(deadline) {
		if f() {
			return
		}
		time.Sleep(25 * time.Millisecond)
	}
	t.Fatalf("timed out waiting for %s", what)
}

// probeCodecs runs ffprobe on a plain FLV and returns its stream codec names.
func probeCodecs(t *testing.T, plain []byte) []string {
	fp := needTool(t, "ffprobe")
	f := filepath.Join(t.TempDir(), "out.flv")
	if err := os.WriteFile(f, plain, 0o644); err != nil {
		t.Fatal(err)
	}
	out, err := exec.Command(fp, "-v", "error", "-f", "flv", "-show_entries", "stream=codec_name", "-of", "csv=p=0", f).CombinedOutput()
	if err != nil {
		t.Fatalf("ffprobe: %v %s", err, out)
	}
	return strings.Fields(string(out))
}
