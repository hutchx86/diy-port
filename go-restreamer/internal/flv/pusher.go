// SPDX-License-Identifier: AGPL-3.0-or-later

package flv

import (
	"context"
	"fmt"
	"io"
	"log/slog"
	"net"
	"os"
	"path/filepath"
	"sync"
	"sync/atomic"
	"time"

	"diyport/go-restreamer/internal/audio"
	"diyport/go-restreamer/internal/media"
)

// Config for one channel's push.
type Config struct {
	Addr         string // dest_host:dest_port (Protect, normally :7550)
	StreamName   string
	AudioEnabled bool
	FFmpegPath   string // for audio transcodes; "" looks on PATH
	Opus         bool   // also push an Opus track (type 10) for the browser live view
	OpusRate     int    // its sample rate: encode rate and trailer timescale (0: 48000)
	// AudioEncoder, when set, is the ffmpeg shared by the channels on one camera URL (the
	// manager feeds it); nil: this pusher runs its own.
	AudioEncoder  *audio.Encoder
	TranscodeG711 bool          // G.711 -> AAC instead of the Python's copy; see package audio
	LegacySync    bool          // clock sync every 5 s + early sync instead of the G3 key-frame sync
	RetryInterval time.Duration // base wait between connections (Python retryInterval=1s)
	MaxRetry      time.Duration // cap after repeated failures (Python PUSH_RETRY_MAX_S=30s)
	DialTimeout   time.Duration // Python connectTimeout=5s
	StallTimeout  time.Duration // no tag for this long ends the connection (Python STALL_S=30s)
	QueueTags     int           // Python QUEUE_TAGS=600
	// Pace > 0 holds each tag until connection start + Pace + its ts and stamps its trailer
	// with that time, not the write time. The trailer is the wall clock Protect plays by; on
	// this Mac's Wi-Fi (LAN pings 4-167 ms) the camera's frames arrive in clumps and stall up
	// to 4.9 s, and stamping writes as they came made Protect's browser audio stutter (tee
	// capture 2026-11-12 11:42 on a Hikvision camera: AAC ts exactly 32 ms apart, write gaps to 719 ms).
	// A frame later than Pace re-bases the clock there (one jump, then smooth again).
	Pace time.Duration
	Log  *slog.Logger

	// Hooks for the shared-decode clock (see internal/detect): the Python's ProtectClock is fed
	// the same three events by FlvPush. pts values are on the source's clock (media PTS), so
	// a reader that saw the same frames can map them. All are optional and called from the
	// push goroutine; they must not block.
	OnStart func()                                  // a connection began (its clock syncs restart)
	OnSync  func(pts time.Duration, wall time.Time) // an onClockSync went to Protect
	OnWrote func(pts time.Duration)                 // a video frame went to Protect
}

// Stats is a snapshot for get_stats.
type Stats struct {
	BytesSent     uint64
	LastFrameTime time.Time // last video frame written to Protect; zero before the first
	Reconnects    uint64    // connections made after the first
	Connected     bool
}

const audioHold = 200 * time.Millisecond

type pendAudio struct {
	it item
	at time.Time
}

type item struct {
	kind  byte // 'S' start, 'T' tag
	typ   byte
	ts    int64
	key   bool
	body  []byte
	start *startInfo
}

// startInfo is what a connection needs to write its header and sequence headers.
type startInfo struct {
	base   time.Duration // PTS of the first IDR: tag timestamps are relative to it
	avcc   []byte
	format audio.Format
	meta   ECMAArray
}

// Pusher is a media.Subscriber that keeps one channel pushed to Protect, as onvif_push.py's
// FlvPush does: wait for an IDR, connect, write header + onMetaData + (G3 initial sync) +
// AVC and AAC sequence headers, then tags with ms timestamps from the first IDR's PTS,
// reconnecting with backoff on any failure.
type Pusher struct {
	unpark chan struct{} // Unpark wakes a parked push
	parked atomic.Bool
	cfg    Config
	log    *slog.Logger
	norm   *audio.Normalizer

	mu      sync.Mutex
	tracks  media.Tracks
	avcc    []byte
	format  audio.Format
	haveTrk bool
	waiting bool // true until an IDR starts the next connection
	base    time.Duration
	lastV   int64
	lastA   int64 // last AAC stamp (Opus has lastO; the merged sequence is kept by the reorder buffer)
	lastO   int64 // last Opus stamp
	// Audio reorder buffer (Opus on): AAC (ffmpeg stdout) and Opus (fd 3) reach us out of ts
	// order, and the one monotonic audio guard bent the earlier ones forward (live capture:
	// AAC gaps 0,4,8,12 instead of 32, Opus 24,32,60 instead of 20). Each track is stamped
	// on its own grid and the two merged here in ts order; a tag is released once both
	// tracks have delivered at or beyond its ts, or after audioHold. Stamps are never
	// altered: a tag that would still step back is dropped and counted.
	pend       []pendAudio
	hiA, hiO   int64 // highest ts delivered per track
	lastRel    int64 // ts of the last released audio tag
	audioDrops atomic.Uint64
	q          chan item
	resync     chan struct{} // closed-and-replaced signal: tracks changed or the queue overflowed
	resyncCh   chan struct{}

	bytes      atomic.Uint64
	lastFrame  atomic.Int64 // unix nanos
	reconnects atomic.Uint64
	connected  atomic.Bool

	ctx    context.Context
	cancel context.CancelFunc
	wg     sync.WaitGroup
	connMu sync.Mutex
	conn   net.Conn
}

var _ media.Subscriber = (*Pusher)(nil)

func NewPusher(cfg Config) *Pusher {
	if cfg.RetryInterval <= 0 {
		cfg.RetryInterval = time.Second
	}
	if cfg.MaxRetry <= 0 {
		cfg.MaxRetry = 30 * time.Second
	}
	if cfg.DialTimeout <= 0 {
		cfg.DialTimeout = 5 * time.Second
	}
	if cfg.StallTimeout <= 0 {
		cfg.StallTimeout = 30 * time.Second
	}
	if cfg.QueueTags <= 0 {
		cfg.QueueTags = 600
	}
	if cfg.Log == nil {
		cfg.Log = slog.Default()
	}
	p := &Pusher{unpark: make(chan struct{}, 1), cfg: cfg, log: cfg.Log, waiting: true, lastO: -opusStepMS, q: make(chan item, cfg.QueueTags),
		resyncCh: make(chan struct{}, 1)}
	p.ctx, p.cancel = context.WithCancel(context.Background())
	p.norm = audio.New(audio.Config{Enabled: cfg.AudioEnabled, TranscodeG711: cfg.TranscodeG711,
		FFmpegPath: cfg.FFmpegPath, Log: cfg.Log, OpusOut: p.opusOut(), OpusRate: cfg.OpusRate, Encoder: cfg.AudioEncoder}, p.onAudioOut)
	p.wg.Add(1)
	go p.run()
	return p
}

// SetAudioEncoder moves the pusher to another URL's shared encoder.
func (p *Pusher) SetAudioEncoder(e *audio.Encoder) { p.norm.SetEncoder(e) }

// Close stops the push and its ffmpeg.
func (p *Pusher) Close() {
	p.cancel()
	p.connMu.Lock()
	if p.conn != nil {
		p.conn.Close()
	}
	p.connMu.Unlock()
	p.wg.Wait()
	p.norm.Close()
}

func (p *Pusher) Stats() Stats {
	s := Stats{BytesSent: p.bytes.Load(), Reconnects: p.reconnects.Load(), Connected: p.connected.Load()}
	if n := p.lastFrame.Load(); n != 0 {
		s.LastFrameTime = time.Unix(0, n)
	}
	return s
}

// OnTracks takes new stream parameters. A change that alters the sequence headers ends the
// running connection; the push resumes at the next IDR.
func (p *Pusher) OnTracks(t media.Tracks) {
	var avcc []byte
	if t.Video == media.H264 {
		var err error
		if avcc, err = AVCDecoderConfigurationRecord(t.SPS, t.PPS); err != nil {
			p.log.Warn("flv: unusable video config", "stream", p.cfg.StreamName, "err", err)
			return
		}
	} else {
		// Protect's ingest only takes AVC here; HEVC must come through transcode.
		p.log.Warn("flv: video codec not pushable", "stream", p.cfg.StreamName, "codec", t.Video)
		return
	}
	format := p.norm.SetTracks(t)
	p.mu.Lock()
	changed := p.haveTrk && (!format.Equal(p.format) || string(avcc) != string(p.avcc) ||
		t.Width != p.tracks.Width || t.Height != p.tracks.Height)
	p.tracks, p.avcc, p.format, p.haveTrk = t, avcc, format, true
	if changed {
		p.resetLocked()
	}
	p.mu.Unlock()
	if changed {
		p.signal()
	}
}

// OnDisconnect: the source dropped. Frames stop; the connection idles until the stall
// timeout, and the next IDR after OnTracks starts a fresh one.
func (p *Pusher) OnDisconnect(error) {
	p.mu.Lock()
	p.resetLocked()
	p.mu.Unlock()
	p.signal()
}

func (p *Pusher) signal() {
	select {
	case p.resyncCh <- struct{}{}:
	default:
	}
}

// resetLocked drops queued tags and waits for an IDR again.
func (p *Pusher) resetLocked() {
	p.waiting = true
	p.pend = nil
	p.norm.Stop()
	for {
		select {
		case <-p.q:
		default:
			return
		}
	}
}

func (p *Pusher) enqueueLocked(it item) {
	select {
	case p.q <- it:
	default:
		// Fell behind the camera (Python: queue cleared and the push ends).
		p.log.Warn("flv: push fell behind, resyncing", "stream", p.cfg.StreamName)
		p.resetLocked()
		p.signal()
	}
}

func (p *Pusher) OnVideo(au media.VideoAU) {
	p.mu.Lock()
	defer p.mu.Unlock()
	if !p.haveTrk {
		return
	}
	if p.waiting {
		if !au.IDR {
			return
		}
		p.waiting = false
		p.base = au.PTS
		p.lastV, p.lastA, p.lastO = 0, 0, -opusStepMS
		p.hiA, p.hiO, p.lastRel, p.pend = -1, -1, 0, nil
		p.drainLocked()
		p.norm.Start(au.PTS)
		p.enqueueLocked(item{kind: 'S', start: &startInfo{base: au.PTS, avcc: p.avcc, format: p.format,
			meta: p.metadata()}})
	}
	// Silence (or transcoded audio's pacing) first, so audio is never behind video.
	p.mu.Unlock()
	p.norm.OnVideo(au.PTS)
	p.mu.Lock()
	if p.waiting {
		return
	}
	p.flushAudioLocked() // the hold bound must work when a track went quiet
	ts := p.clamp(&p.lastV, au.PTS)
	p.enqueueLocked(item{kind: 'T', typ: tagVideo, ts: ts, key: au.IDR,
		body: videoFramePayload(au.IDR, NALUsToAVCC(au.NALUs))})
}

func (p *Pusher) drainLocked() {
	for {
		select {
		case <-p.q:
		default:
			return
		}
	}
}

// OnAudio forwards a camera frame to the normalizer, which calls onAudioOut.
func (p *Pusher) OnAudio(f media.AudioFrame) { p.norm.OnAudio(f) }

// onAudioOut receives normalized frames (any goroutine) and queues them as tags.
func (p *Pusher) onAudioOut(f media.AudioFrame) {
	p.mu.Lock()
	defer p.mu.Unlock()
	if p.waiting || len(f.Data) == 0 {
		return
	}
	ts := p.clamp(&p.lastA, f.PTS)
	it := item{kind: 'T', typ: tagAudio, ts: ts, body: audioPayload(p.format, f.Data)}
	if !p.cfg.Opus {
		p.enqueueLocked(it)
		return
	}
	p.hiA = max(p.hiA, ts)
	p.addAudioLocked(it)
}

// addAudioLocked inserts into the ts-ordered buffer (stable: equal ts keep arrival order)
// and releases what is ready.
func (p *Pusher) addAudioLocked(it item) {
	i := len(p.pend)
	for i > 0 && p.pend[i-1].it.ts > it.ts {
		i--
	}
	p.pend = append(p.pend, pendAudio{})
	copy(p.pend[i+1:], p.pend[i:])
	p.pend[i] = pendAudio{it, time.Now()}
	p.flushAudioLocked()
}

func (p *Pusher) flushAudioLocked() {
	ready := min(p.hiA, p.hiO)
	for len(p.pend) > 0 && !p.waiting {
		h := p.pend[0]
		if h.it.ts > ready && time.Since(h.at) < audioHold {
			return
		}
		p.pend = p.pend[1:]
		if h.it.ts < p.lastRel {
			p.audioDrops.Add(1)
			p.log.Debug("flv: audio tag would step back, dropped", "stream", p.cfg.StreamName,
				"ts", h.it.ts, "last", p.lastRel)
			continue
		}
		p.lastRel = h.it.ts
		p.enqueueLocked(h.it)
	}
}

// opusOut is the Normalizer's Opus sink, nil when Opus is off.
func (p *Pusher) opusOut() func(media.AudioFrame) {
	if !p.cfg.Opus {
		return nil
	}
	return p.onOpusOut
}

// onOpusOut queues one Opus packet as a type-10 tag. Its stamp must be a smooth 20 ms grid:
// yi-protect FlvPush.cpp keeps "Opus on its own steady 20 ms grid" (oTagMs >= lastOpusMs + 20)
// because bunchy Opus stamps garble Protect's low-latency web live audio. yi's lastAudioMs:
// "ms orders the AAC and Opus tags of a connection as one audio sequence (a 1 ms step back
// between the tracks terminates it)" - kept by merging both tracks in ts order (see pend)
// rather than by bending stamps. Pace then releases it with the other tags.
func (p *Pusher) onOpusOut(f media.AudioFrame) {
	p.mu.Lock()
	defer p.mu.Unlock()
	if p.waiting || len(f.Data) == 0 {
		return
	}
	ts := max((f.PTS - p.base).Milliseconds(), p.lastO+opusStepMS)
	p.lastO = ts
	p.hiO = max(p.hiO, ts)
	p.addAudioLocked(item{kind: 'T', typ: tagOpus, ts: ts, body: f.Data})
}

// clamp converts a PTS to ms from the base and keeps one track's timestamps non-decreasing.
func (p *Pusher) clamp(last *int64, pts time.Duration) int64 {
	ts := (pts - p.base).Milliseconds()
	if ts < *last {
		ts = *last
	}
	*last = ts
	return ts
}

// audioPayload is an FLV audio tag body. AAC: 0xAF (format 10, 44 kHz flag, 16-bit, stereo
// flag: always these for AAC) + packet type 1. G.711 as ffmpeg's muxer writes it: format 7
// (A-law) or 8 (mu-law), "special" 8 kHz rate code 0, 16-bit.
func audioPayload(f audio.Format, data []byte) []byte {
	switch f.Codec {
	case media.PCMA:
		return append([]byte{0x72}, data...)
	case media.PCMU:
		return append([]byte{0x82}, data...)
	}
	return append([]byte{0xAF, 1}, data...)
}

func audioConfigPayload(f audio.Format) []byte {
	return append([]byte{0xAF, 0}, f.Config...)
}

// metadata is onMetaData as ffmpeg's FLV muxer writes it (the Python forwards ffmpeg's), with
// streamName as the first entry as set_stream_name puts it. Protect reads streamName; the rest
// is for players.
func (p *Pusher) metadata() ECMAArray {
	t := p.tracks
	fps := t.FPS
	if fps == 0 {
		fps = 20
	}
	m := ECMAArray{
		{"streamName", p.cfg.StreamName},
		{"duration", 0}, {"width", t.Width}, {"height", t.Height},
		{"videodatarate", 0}, {"framerate", fps}, {"videocodecid", 7},
	}
	f := p.format
	switch f.Codec {
	case media.PCMA, media.PCMU:
		id := 7
		if f.Codec == media.PCMU {
			id = 8
		}
		m = append(m, Prop{"audiodatarate", 64}, Prop{"audiosamplerate", 8000}, Prop{"audiosamplesize", 16},
			Prop{"stereo", false}, Prop{"audiocodecid", id})
	default:
		m = append(m, Prop{"audiodatarate", 32}, Prop{"audiosamplerate", f.Rate}, Prop{"audiosamplesize", 16},
			Prop{"stereo", f.Channels == 2}, Prop{"audiocodecid", 10})
	}
	return append(m, Prop{"encoder", "diyport-go"}, Prop{"filesize", 0})
}

// run waits for a start item, pushes until the connection ends, then backs off. Like
// FlvPush._run: a push that sent nothing counts as failed; Protect's retryInterval doubles
// after the third failure in a row up to MaxRetry.
func (p *Pusher) run() {
	defer p.wg.Done()
	first, failed, short := true, 0, 0
	for p.ctx.Err() == nil {
		var it item
		select {
		case <-p.ctx.Done():
			return
		case <-p.resyncCh:
			continue
		case it = <-p.q:
		}
		if it.kind != 'S' {
			continue
		}
		if !first {
			p.reconnects.Add(1)
		}
		first = false
		began := time.Now()
		if p.pushOnce(it.start) {
			failed = 0
		} else {
			failed++
		}
		// Protect refusing the stream (a stale name after a console reboot: live 2026-11-12
		// 19:08, video2 reconnected every 3-4 s for good) ends each connection within seconds.
		// After rejectsToPark of those, hold the channel until Protect asks for it again.
		if time.Since(began) < rejectWindow {
			short++
		} else {
			short = 0
		}
		if short >= rejectsToPark {
			short = 0
			p.parked.Store(true)
			p.log.Warn("flv: Protect keeps closing this push; holding it until Protect asks again",
				"stream", p.cfg.StreamName)
			select {
			case <-p.ctx.Done():
				return
			case <-p.unpark:
			}
			p.parked.Store(false)
			p.log.Info("flv: Protect asked again; resuming push", "stream", p.cfg.StreamName)
		}
		p.mu.Lock()
		p.resetLocked()
		p.mu.Unlock()
		select {
		case <-p.ctx.Done():
			return
		case <-time.After(retryDelay(p.cfg.RetryInterval, p.cfg.MaxRetry, failed)):
		}
		// Anything queued while backing off belongs to a dead session; the next IDR starts anew.
		p.mu.Lock()
		p.resetLocked()
		p.mu.Unlock()
	}
}

const (
	rejectWindow  = 10 * time.Second
	rejectsToPark = 3
)

// Unpark resumes a push parked after Protect kept rejecting it; a no-op otherwise.
func (p *Pusher) Unpark() {
	select {
	case p.unpark <- struct{}{}:
	default:
	}
}

// Parked reports whether the push is held after repeated rejections.
func (p *Pusher) Parked() bool { return p.parked.Load() }

func retryDelay(base, max time.Duration, failed int) time.Duration {
	if failed == 0 {
		return base
	}
	d := base
	for i := 3; i < failed && d < max; i++ {
		d *= 2
	}
	return min(d, max)
}

func (p *Pusher) pushOnce(si *startInfo) (sentAny bool) {
	d := net.Dialer{Timeout: p.cfg.DialTimeout}
	conn, err := d.DialContext(p.ctx, "tcp", p.cfg.Addr)
	if err != nil {
		p.log.Warn("flv: connect failed", "stream", p.cfg.StreamName, "err", err)
		return false
	}
	p.connMu.Lock()
	p.conn = conn
	p.connMu.Unlock()
	p.connected.Store(true)
	defer func() {
		p.connected.Store(false)
		conn.Close()
	}()
	// Drain what Protect writes back (~25 B/s on some pushes): unread, its window fills.
	go io.Copy(io.Discard, conn)
	p.log.Info("flv: push started", "stream", p.cfg.StreamName, "addr", p.cfg.Addr)

	var sent int
	// DIYPORT_RESTREAMER_TEE=<dir>: a debugging copy of this push, <stream>.flv plus
	// <stream>.times ("unix_ns bytes" per write), to see when each tag reached Protect.
	var teeData, teeTimes *os.File
	if dir := os.Getenv("DIYPORT_RESTREAMER_TEE"); dir != "" {
		base := filepath.Join(dir, p.cfg.StreamName)
		teeData, _ = os.Create(base + ".flv")
		teeTimes, _ = os.Create(base + ".times")
		defer func() {
			if teeData != nil {
				teeData.Close()
			}
			if teeTimes != nil {
				teeTimes.Close()
			}
		}()
	}
	write := func(b []byte) error {
		if teeData != nil && teeTimes != nil {
			teeData.Write(b)
			fmt.Fprintf(teeTimes, "%d %d\n", time.Now().UnixNano(), len(b))
		}
		conn.SetWriteDeadline(time.Now().Add(30 * time.Second))
		n, err := conn.Write(b)
		sent += n
		p.bytes.Add(uint64(n))
		return err
	}
	w := &writer{g3: !p.cfg.LegacySync, early: p.cfg.LegacySync, opusRate: uint32(p.cfg.OpusRate)}
	if si.format.Codec == media.AAC {
		w.aacRate = uint32(si.format.Rate)
		if w.aacRate == 0 {
			w.aacRate = uint32(audio.ASCRate(si.format.Config))
		}
	}
	defer func() { sentAny = sent > 0 }()

	now := time.Now()
	if write(w.begin(now)) != nil {
		return
	}
	if p.cfg.OnStart != nil {
		p.cfg.OnStart()
	}
	// Config tags: meta, video config, audio config, all at ts 0, in the Python's order; the G3
	// initial sync goes right after onMetaData and before the codec configs.
	configs := []struct {
		typ     byte
		payload []byte
	}{
		{tagScript, ScriptPayload("onMetaData", si.meta)},
		{tagVideo, videoConfigPayload(si.avcc)},
		{tagAudio, audioConfigPayload(si.format)},
	}
	if si.format.Codec != media.AAC {
		configs = configs[:2] // G.711 has no sequence header
	}
	if p.cfg.Opus {
		// After the AAC config, as yi-protect writes them.
		configs = append(configs, struct {
			typ     byte
			payload []byte
		}{tagOpus, opusConfig})
	}
	for _, c := range configs {
		// Config tags and the initial sync go on the paced clock too (start + Pace), where
		// the first media frame lands. Stamped with the real time, the audio config read as
		// "last audio frame" 2000 ms behind the first video frame and ms logged diff=2000
		// exceeds threshold on every new push (live 2026-11-12 00:37Z), stalling new viewers.
		now = w.start.Add(p.cfg.Pace)
		if write(w.tag(now, c.typ, c.payload, 0)) != nil {
			return
		}
		if c.typ == tagScript && w.g3 {
			t := w.start.Add(p.cfg.Pace)
			if write(w.initialSync(t)) != nil {
				return
			}
			if p.cfg.OnSync != nil {
				p.cfg.OnSync(si.base, t)
			}
		}
	}
	var paceShift time.Duration // Pace: total re-basing after stalls longer than Pace
	stall := time.NewTimer(p.cfg.StallTimeout)
	defer stall.Stop()
	for {
		if !stall.Stop() {
			select {
			case <-stall.C:
			default:
			}
		}
		stall.Reset(p.cfg.StallTimeout)
		select {
		case <-p.ctx.Done():
			return
		case <-p.resyncCh:
			return // tracks changed, source dropped, or fell behind
		case <-stall.C:
			p.log.Warn("flv: no frames, ending push", "stream", p.cfg.StreamName)
			return
		case it := <-p.q:
			if it.kind != 'T' {
				return // a new start overtook the resync signal: end this connection, run() resets
			}
			now = time.Now()
			if p.cfg.Pace > 0 {
				target := w.start.Add(paceShift + p.cfg.Pace + time.Duration(it.ts)*time.Millisecond)
				if wait := target.Sub(now); wait > 0 {
					// A forward PTS jump can wait longer than StallTimeout: keep the watchdog
					// and the resync signal live here, or a dead connection stays open.
					select {
					case <-p.ctx.Done():
						return
					case <-p.resyncCh:
						return // tracks changed, source dropped, or fell behind
					case <-stall.C:
						p.log.Warn("flv: no frames, ending push", "stream", p.cfg.StreamName)
						return
					case <-time.After(wait):
					}
				} else if -wait > p.cfg.Pace {
					// Stalled past the buffer: re-base so this frame again has the whole Pace
					// ahead of it. Re-basing to now left no buffer at all, so the next Wi-Fi
					// hiccup re-based again (tee 2026-11-12 11:48: 136.5 s then 140.6 s).
					shift := -wait + p.cfg.Pace
					paceShift += shift
					p.log.Info("flv: pace re-based after a stall", "stream", p.cfg.StreamName,
						"late_ms", (-wait - p.cfg.Pace).Milliseconds(), "shift_ms", shift.Milliseconds())
					select {
					case <-p.ctx.Done():
						return
					case <-time.After(p.cfg.Pace):
					}
					target = now.Add(p.cfg.Pace)
				}
				now = target
			}
			if b := w.syncIfDue(now, it.ts, it.key && it.typ == tagVideo); b != nil {
				if write(b) != nil {
					return
				}
				if p.cfg.OnSync != nil {
					// Without the stall shift: Protect's playback doesn't follow a re-base.
					// Live 2026-11-15 11:24 on Driveway: a re-base (shift_ms=2013) moved the
					// detection walls 2 s (push lag 0.1 -> 2.1 s), and boxes stopped lining up.
					p.cfg.OnSync(si.base+time.Duration(it.ts)*time.Millisecond, now.Add(-paceShift))
				}
			}
			if write(w.tag(now, it.typ, it.body, it.ts)) != nil {
				return
			}
			if it.typ == tagVideo {
				if p.cfg.OnWrote != nil {
					p.cfg.OnWrote(si.base + time.Duration(it.ts)*time.Millisecond)
				}
				p.lastFrame.Store(now.UnixNano())
				w.afterVideo(it.body)
			}
		}
	}
}
