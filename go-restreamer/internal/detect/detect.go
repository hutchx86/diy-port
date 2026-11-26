// SPDX-License-Identifier: AGPL-3.0-or-later

// Package detect serves a camera's video1 to the detection process on a unix socket, byte for
// byte as onvif_push.py's DetectServer does, so diyport/shared_decode.py (and avclient)
// work unchanged with the Go restreamer. The point of shared decode: detection decodes the
// very video Protect plays, and every frame carries the wall time Protect places it at, so
// boxes line up with the picture (Live 2026-11-05 on Driveway: a separate RTSP pull put boxes
// ~1.9 s ahead of the truck).
//
// Wire format: a stream of records, each ">BdI" (kind, wall ms as a float64, length) followed
// by `length` bytes. Kind 0 is plain FLV bytes (the file header, then onMetaData and the
// sequence-header tags at timestamp 0); kind 1 is one video tag (11-byte tag header, payload,
// PreviousTagSize) whose wall field is the time Protect gives that frame. The FLV carries no
// per-tag timing trailers: those are Protect-only.
package detect

import (
	"encoding/binary"
	"log/slog"
	"math"
	"net"
	"os"
	"sync"
	"time"

	"diyport/go-restreamer/internal/flv"
	"diyport/go-restreamer/internal/media"
)

// Record kinds (Python DETECT_BYTES, DETECT_FRAME).
const (
	KindBytes = 0
	KindFrame = 1
)

const (
	recordSize = 13 // struct ">BdI"
	// QueueRecords is how far a client may fall behind before it is dropped (Python
	// DETECT_QUEUE = 300 tags).
	queueRecords = 300
	// ClockWait is how long a frame waits for the video1 push to send it before detection
	// falls back to its own clock (Python PROTECT_CLOCK_WAIT_S).
	ClockWait = 3 * time.Second
	// fallbackSync: a clock reading older than this is refreshed even without a key frame
	// (Python G3_SYNC_FALLBACK_S).
	fallbackSync = 10 * time.Second
	lagLogEvery  = 60 * time.Second
)

// Pack builds one record.
func Pack(kind byte, wallMs float64, data []byte) []byte {
	b := make([]byte, recordSize, recordSize+len(data))
	b[0] = kind
	binary.BigEndian.PutUint64(b[1:], math.Float64bits(wallMs))
	binary.BigEndian.PutUint32(b[9:], uint32(len(data)))
	return append(b, data...)
}

// Unpack reads a record header (the Python read_detect_records' DETECT_RECORD.unpack).
func Unpack(head []byte) (kind byte, wallMs float64, length int) {
	return head[0], math.Float64frombits(binary.BigEndian.Uint64(head[1:])), int(binary.BigEndian.Uint32(head[9:]))
}

// Server is a media.Subscriber for the channel's stream and the listener for its readers.
type Server struct {
	path  string
	clock *Clock
	log   *slog.Logger
	ln    net.Listener

	mu      sync.Mutex
	tracks  media.Tracks
	avcc    []byte
	have    bool
	clients map[*client]struct{}
	closed  bool
	wg      sync.WaitGroup
}

var _ media.Subscriber = (*Server)(nil)

// New listens on path (a stale socket file is removed first, as the Python does). clock is the
// video1 push's Clock; nil means every frame uses detection's own reading.
func New(path string, clock *Clock, log *slog.Logger) (*Server, error) {
	if log == nil {
		log = slog.Default()
	}
	_ = os.Remove(path)
	ln, err := net.Listen("unix", path)
	if err != nil {
		return nil, err
	}
	s := &Server{path: path, clock: clock, log: log, ln: ln, clients: map[*client]struct{}{}}
	s.wg.Add(1)
	go s.accept()
	return s, nil
}

func (s *Server) accept() {
	defer s.wg.Done()
	for {
		conn, err := s.ln.Accept()
		if err != nil {
			return
		}
		c := &client{s: s, conn: conn, q: make(chan any, queueRecords), done: make(chan struct{})}
		s.mu.Lock()
		if s.closed {
			s.mu.Unlock()
			conn.Close()
			return
		}
		s.clients[c] = struct{}{}
		s.mu.Unlock()
		s.wg.Add(1)
		go c.run()
	}
}

// Close stops listening, drops the readers and removes the socket file.
func (s *Server) Close() {
	s.mu.Lock()
	s.closed = true
	cs := make([]*client, 0, len(s.clients))
	for c := range s.clients {
		cs = append(cs, c)
	}
	s.mu.Unlock()
	s.ln.Close()
	for _, c := range cs {
		c.kill()
	}
	s.wg.Wait()
	_ = os.Remove(s.path)
}

func (s *Server) OnTracks(t media.Tracks) {
	if t.Video != media.H264 {
		return
	}
	avcc, err := flv.AVCDecoderConfigurationRecord(t.SPS, t.PPS)
	if err != nil {
		return
	}
	s.mu.Lock()
	changed := s.have && (string(avcc) != string(s.avcc) || t.Width != s.tracks.Width || t.Height != s.tracks.Height)
	s.tracks, s.avcc, s.have = t, avcc, true
	var cs []*client
	if changed {
		// A decoder fed from the old headers can't follow: readers reconnect (Python ends the
		// stream; shared_decode's feed ends and avclient restarts it).
		for c := range s.clients {
			cs = append(cs, c)
		}
	}
	s.mu.Unlock()
	for _, c := range cs {
		c.kill()
	}
}

func (s *Server) OnDisconnect(error) {
	s.mu.Lock()
	cs := make([]*client, 0, len(s.clients))
	for c := range s.clients {
		cs = append(cs, c)
	}
	s.have = false
	s.mu.Unlock()
	for _, c := range cs {
		c.kill()
	}
}

func (s *Server) OnAudio(media.AudioFrame) {} // detection decodes video only

func (s *Server) OnVideo(au media.VideoAU) {
	s.mu.Lock()
	if !s.have || len(s.clients) == 0 {
		s.mu.Unlock()
		return
	}
	cs := make([]*client, 0, len(s.clients))
	for c := range s.clients {
		cs = append(cs, c)
	}
	tracks, avcc := s.tracks, s.avcc
	s.mu.Unlock()
	var payload []byte // built once, shared by the readers
	for _, c := range cs {
		if payload == nil {
			payload = framePayload(au.IDR, flv.NALUsToAVCC(au.NALUs))
		}
		c.video(au, payload, tracks, avcc)
	}
}

func (s *Server) drop(c *client) {
	s.mu.Lock()
	delete(s.clients, c)
	s.mu.Unlock()
}

func framePayload(key bool, avcc []byte) []byte {
	ft := byte(0x27)
	if key {
		ft = 0x17
	}
	return append([]byte{ft, 1, 0, 0, 0}, avcc...)
}

// tag is an FLV tag plus its PreviousTagSize, no trailer.
func tag(typ byte, payload []byte, ts int64) []byte {
	t := uint32(int32(ts))
	n := len(payload)
	b := make([]byte, 0, 15+n)
	b = append(b, typ, byte(n>>16), byte(n>>8), byte(n), byte(t>>16), byte(t>>8), byte(t), byte(t>>24), 0, 0, 0)
	b = append(b, payload...)
	return binary.BigEndian.AppendUint32(b, uint32(n+11))
}

// queued is a video frame awaiting its wall time: resolved in the writer goroutine, once the
// push has sent the frame.
type queued struct {
	pts time.Duration
	own float64 // detection's own wall reading, ms
	tag []byte
}

type client struct {
	s    *Server
	conn net.Conn
	q    chan any // []byte (a ready record) or queued
	done chan struct{}
	once sync.Once

	// Touched only from the Server's OnVideo goroutine (the pull's), which is serial.
	started bool
	base    time.Duration
	syncWal float64 // detection's own mapping: wall ms and stream ms of the last sync
	syncTS  int64
	haveSyn bool
	wallAt  time.Time
}

func (c *client) kill() {
	c.once.Do(func() {
		close(c.done)
		c.conn.Close()
	})
}

func (c *client) put(v any) {
	select {
	case c.q <- v:
	default:
		c.kill() // fell behind: it reconnects at a key frame
	}
}

// video queues one frame, first starting the reader at its first IDR: the FLV header, the
// metadata and the AVC sequence header at timestamp 0, as DetectClient's "start" item does.
func (c *client) video(au media.VideoAU, payload []byte, t media.Tracks, avcc []byte) {
	select {
	case <-c.done:
		return
	default:
	}
	if !c.started {
		if !au.IDR {
			return
		}
		c.started, c.base = true, au.PTS
		c.put(Pack(KindBytes, 0, []byte{'F', 'L', 'V', 1, 1, 0, 0, 0, 9, 0, 0, 0, 0}))
		fps := t.FPS
		if fps == 0 {
			fps = 20
		}
		meta := flv.ECMAArray{{Key: "duration", Val: 0}, {Key: "width", Val: t.Width},
			{Key: "height", Val: t.Height}, {Key: "videodatarate", Val: 0},
			{Key: "framerate", Val: fps}, {Key: "videocodecid", Val: 7}}
		c.put(Pack(KindBytes, 0, tag(18, flv.ScriptPayload("onMetaData", meta), 0)))
		c.put(Pack(KindBytes, 0, tag(9, append([]byte{0x17, 0, 0, 0, 0}, avcc...), 0)))
	}
	ts := (au.PTS - c.base).Milliseconds()
	if ts < 0 {
		ts = 0
	}
	now := time.Now()
	nowMs := float64(now.UnixMilli())
	if !c.haveSyn || au.IDR || now.Sub(c.wallAt) >= fallbackSync {
		c.syncWal, c.syncTS, c.haveSyn, c.wallAt = nowMs, ts, true, now
	}
	own := c.syncWal + float64(ts-c.syncTS)
	c.put(queued{pts: au.PTS, own: own, tag: tag(9, payload, ts)})
}

func (c *client) run() {
	defer c.s.wg.Done()
	defer c.s.drop(c)
	defer c.kill()
	var lags []float64
	fallbacks := 0
	logged := time.Now()
	for {
		var v any
		select {
		case <-c.done:
			return
		case v = <-c.q:
		}
		var rec []byte
		switch x := v.(type) {
		case []byte:
			rec = x
		case queued:
			wall := x.own
			if c.s.clock != nil {
				if w, ok := c.s.clock.Wall(x.pts, ClockWait, c.done); ok {
					wall = w
					lags = append(lags, w-x.own)
				} else {
					fallbacks++
				}
			}
			rec = Pack(KindFrame, wall, x.tag)
			if time.Since(logged) >= lagLogEvery {
				logged = time.Now()
				if len(lags) > 0 {
					var sum, max float64
					for _, l := range lags {
						sum += l
						max = math.Max(max, l)
					}
					c.s.log.Info("detect: video1 push lag", "avg_ms", int(sum/float64(len(lags))),
						"max_ms", int(max), "frames", len(lags), "own_clock", fallbacks)
				} else if fallbacks > 0 {
					c.s.log.Warn("detect: frames on our own clock, no video1 push sent them", "frames", fallbacks)
				}
				lags, fallbacks = nil, 0
			}
		}
		if _, err := c.conn.Write(rec); err != nil {
			return
		}
	}
}
