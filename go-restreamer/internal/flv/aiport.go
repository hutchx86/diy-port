// SPDX-License-Identifier: AGPL-3.0-or-later

package flv

import (
	"context"
	"encoding/binary"
	"fmt"
	"io"
	"log/slog"
	"net"
	"os"
	"path/filepath"
	"sync"
	"sync/atomic"
	"syscall"
	"time"

	"diyport/go-restreamer/internal/audio"
	"diyport/go-restreamer/internal/media"
)

// AIPortPusher pushes one channel the way ubnt_streamer does (docs/aiport-onvif-push-spec.md,
// decompile evidence of AI Port firmware v5.1.12), where Pusher keeps onvif_push.py's
// ffmpeg-era behaviour. Differences that matter on the wire:
//   - one TCP per channel with a flat retryInterval, a bounded byte queue (frames dropped, not the
//     connection resynced) and IP_TOS 184 (5);
//   - tag timestamps are dts_ms - baseDtsMillis, with baseDtsMillis fixed by the first video frame
//     and kept across reconnects; every tag's trailer carries the absolute monotonic pts in the
//     tag's own timescale (4.3);
//   - onMpma + onClockSync before every key frame, throttled onMpma otherwise, no timer-driven
//     sync (4.5, 4.6); config tags once per connection, again only when SPS/PPS change;
//   - header flags 0x07 only when there is audio, no silent filler (4.2);
//   - the console's feedback frames are read on the same socket and onRtt answered (6).
//
// Frames come from a transcode output (video) and a shared audio.Encoder (AAC at 48 kHz, and
// Opus when Protect asked). The producer side builds each frame's complete bytes under mu; one
// writer goroutine owns the connection.
type AIPortPusher struct {
	cfg AIPortConfig
	log *slog.Logger

	mu     sync.Mutex
	tracks media.Tracks
	haveTr bool
	avcc   []byte
	// Audio: joined while the camera has a codec ffmpeg can decode and audio is on.
	enc      *audio.Encoder
	audioOn  bool
	opusOn   bool
	audioKey string
	leave    func()
	audMu    sync.Mutex // serializes Join/leave, which must run without mu (see joinAudio)

	anchorSet bool
	anchor    int64 // ns: absolute monotonic = anchor + source PTS
	needKey   bool  // no video until a key frame (start, tracks changed, source dropped)
	baseSet   bool
	baseMs    int64 // baseDtsMillis
	lastVDts  int64
	lastADts  int64
	tl        timeline
	lastCfg   []byte // the avcC the last key frame chunk was built with

	mpma        mpmaBitrates
	mpmaChanged bool
	mpmaLast    time.Duration // Mono() of the last onMpma; mpmaSent says whether there was one
	mpmaSent    bool

	// Audio reorder: AAC and Opus reach us from two ffmpeg readers out of ts order; they are
	// merged here by ts and released once both tracks have delivered past a tag, or after
	// audioHold (the same bound as Pusher's, for the same 1 ms-step-back reason).
	pend       []aiPending
	hiA, hiO   int64
	audioDrops atomic.Uint64

	q       []chunk
	qBytes  int
	drops   atomic.Uint64
	dropLog time.Time
	sig     chan struct{}
	kick    chan struct{} // end the connection (stream shape changed)
	rtt     chan []byte   // onRtt tags from the feedback reader to the writer

	sentKey    atomic.Bool // a key frame chunk has gone out: later connections use the reconnect sequence
	bytes      atomic.Uint64
	lastFrame  atomic.Int64
	reconnects atomic.Uint64
	connected  atomic.Bool
	// stopped: the console sent feedback type 3 (spec 6: ubnt_streamer sets the stream's stop flag,
	// +0xb8). Live 2026-11-12 21:51 on a Hikvision camera: after unpair/re-pair the cached video1/video2
	// streamNames had no ingest point any more; ms answered every reconnect with type 3 and we
	// redialled once a second (66 times) until reconfigured. Stop redialling; a new streamName
	// from ChangeVideoSettings builds a new pusher.
	stopped atomic.Bool

	cntA, cntO audioCounter // AudioCounter state, under mu

	ctx    context.Context
	cancel context.CancelFunc
	wg     sync.WaitGroup
	connMu sync.Mutex
	conn   net.Conn
}

var _ media.Subscriber = (*AIPortPusher)(nil)

// AIPortConfig is one channel's push.
type AIPortConfig struct {
	Addr       string // dest_host:dest_port
	StreamName string
	Channel    string // video1, video2, video3: streamOrdinal 0/1/2, streamId 1/2/4
	// Width, Height, FPS, BitrateBps are the channel's ChangeVideoSettings values, used where the
	// stream does not say: onMetaData's videoBandwidth, and the size and fps when tracks lack them.
	Width, Height int
	FPS           float64
	BitrateBps    int

	AudioEnabled bool
	Opus         bool // Protect asked withOpus
	OpusRate     int  // opusSampleRate (0: 48000)
	AACRate      int  // AAC sample rate the encoder produces (0: 48000, the box's avenc_aac)
	// AudioCounter: audio timestamps are first-frame pts + n frame durations instead of each
	// encoder frame's own pts. Live 2026-11-12 23:05 (a Hikvision camera via ms in Safari's mode): our
	// audio tfdt jumped +-2..4 ticks at 48 kHz in 5 of 77 fragments; native ONVIF audio never
	// did, and Safari spun on ours. Re-anchors when the counter drifts > 100 ms from the source.
	AudioCounter bool
	AudioEncoder *audio.Encoder
	FFmpegPath   string

	RetryInterval time.Duration // destination URI retryInterval (default 1 s)
	DialTimeout   time.Duration // connectTimeout, clamped to 1..60 s (default 1 s)
	QueueBytes    int           // output queue limit (default 5 MiB)
	TOS           int           // IP_TOS (default 184)
	AudioHold     time.Duration // reorder bound (default 200 ms)
	Log           *slog.Logger

	// Now and Mono are the wall clock and the monotonic clock the trailers are in; tests inject.
	Now  func() time.Time
	Mono func() time.Duration

	// Hooks for the shared-decode clock, as Config's.
	OnStart func()
	OnSync  func(pts time.Duration, wall time.Time)
	OnWrote func(pts time.Duration)
}

// timeline is the last pts seen on any track: script tags carry its timescale and ticks (4.3).
type timeline struct {
	ns    int64
	ticks uint64
	scale uint32
}

type aiPending struct {
	opus bool
	pts  time.Duration
	data []byte
	at   time.Time
}

// chunk is what one write puts on the wire: for a key frame the onMpma/onClockSync pair, the
// config tag (written only on a connection that lacks it, or when SPS/PPS changed) and the frame.
type chunk struct {
	key, video bool
	pre, cfg   []byte
	cfgChanged bool
	body       []byte
	pts        time.Duration // source PTS, for the hooks
	wall       time.Time
	synced     bool
	tl         timeline
}

func (c *chunk) size() int { return len(c.pre) + len(c.cfg) + len(c.body) }

const (
	aiportQueueBytes  = 5 << 20 // _maxNetworkOutBuffer's default (5)
	aiportDropLogGap  = 30 * time.Second
	aiportMpmaChanged = 500 * time.Millisecond // 4.5 throttle
	aiportMpmaIdle    = 10 * time.Second
	aiportTOS         = 184
	// A key frame this far behind the last video dts resets the timeline: a new base ("Force reset
	// timeline", FUN_001cae50). Smaller steps back are skipped frames.
	aiportResetBackMs = 1000

	// monoFloor keeps the absolute monotonic values uptime-sized instead of near zero: the real
	// ones are CLOCK_MONOTONIC. Which origin is irrelevant to Protect, only the base relation is.
	monoFloor = time.Hour
)

var procStart = time.Now()

func monoNow() time.Duration { return monoFloor + time.Since(procStart) }

// NewAIPortPusher starts the writer goroutine; it connects once the first key frame exists.
func NewAIPortPusher(cfg AIPortConfig) *AIPortPusher {
	if cfg.RetryInterval <= 0 {
		cfg.RetryInterval = time.Second
	}
	if cfg.DialTimeout < time.Second {
		cfg.DialTimeout = time.Second
	} else if cfg.DialTimeout > time.Minute {
		cfg.DialTimeout = time.Minute
	}
	if cfg.QueueBytes <= 0 {
		cfg.QueueBytes = aiportQueueBytes
	}
	if cfg.TOS == 0 {
		cfg.TOS = aiportTOS
	}
	if cfg.AudioHold <= 0 {
		cfg.AudioHold = 200 * time.Millisecond
	}
	if cfg.Log == nil {
		cfg.Log = slog.Default()
	}
	if cfg.Now == nil {
		cfg.Now = time.Now
	}
	if cfg.Mono == nil {
		cfg.Mono = monoNow
	}
	if cfg.OpusRate <= 0 {
		cfg.OpusRate = audio.OpusRate
	}
	p := &AIPortPusher{cfg: cfg, log: cfg.Log, enc: cfg.AudioEncoder, needKey: true, lastVDts: -1, lastADts: -1,
		hiA: -1, hiO: -1, sig: make(chan struct{}, 1), kick: make(chan struct{}, 1), rtt: make(chan []byte, 8)}
	p.mpma = uniformMpma(uint32(max(cfg.BitrateBps, 0)))
	p.ctx, p.cancel = context.WithCancel(context.Background())
	p.wg.Add(1)
	go p.run()
	return p
}

// SetAudioEncoder moves the pusher to another URL's shared encoder (rejoined on the next tracks).
func (p *AIPortPusher) SetAudioEncoder(e *audio.Encoder) {
	p.mu.Lock()
	p.enc = e
	p.mu.Unlock()
}

// Unpark exists for the manager's pusher interface: this push never parks (the real streamer
// retries every retryInterval for good).
func (p *AIPortPusher) Unpark() {}

// Close stops the push and leaves the audio encoder.
func (p *AIPortPusher) Close() {
	p.cancel()
	p.connMu.Lock()
	if p.conn != nil {
		p.conn.Close()
	}
	p.connMu.Unlock()
	p.wg.Wait()
	p.audMu.Lock()
	p.mu.Lock()
	lv := p.leave
	p.leave, p.audioKey = nil, ""
	p.mu.Unlock()
	p.audMu.Unlock()
	if lv != nil {
		lv()
	}
}

// Stats is the same snapshot Pusher gives get_stats.
func (p *AIPortPusher) Stats() Stats {
	s := Stats{BytesSent: p.bytes.Load(), Reconnects: p.reconnects.Load(), Connected: p.connected.Load()}
	if n := p.lastFrame.Load(); n != 0 {
		s.LastFrameTime = time.Unix(0, n)
	}
	return s
}

// Drops is how many frames the output queue refused.
func (p *AIPortPusher) Drops() uint64 { return p.drops.Load() }

// SetBitrate records a new channel bitrate (a runtime ChangeVideoParams): the next onMpma carries
// it, within the 500 ms changed-value throttle.
func (p *AIPortPusher) SetBitrate(bps int) {
	p.mu.Lock()
	defer p.mu.Unlock()
	if v := uniformMpma(uint32(max(bps, 0))); v != p.mpma {
		p.mpma, p.mpmaChanged = v, true
	}
	p.cfg.BitrateBps = bps
}

// OnTracks takes the stream's parameters. New SPS/PPS only show up as a config tag before the
// next key frame; a change of picture size or of audio presence changes onMetaData, so the
// connection restarts (the real streamer is restarted by ubnt_av_dep_mod.sh for it, 1).
func (p *AIPortPusher) OnTracks(t media.Tracks) {
	if t.Video != media.H264 {
		// aiport mode always re-encodes, so the pusher only ever sees H.264. An hvcC record
		// builder (spec 4.4, open question 6) is not implemented.
		p.log.Warn("flv: aiport push takes H.264 only", "stream", p.cfg.StreamName, "codec", t.Video)
		return
	}
	avcc, err := AVCDecoderConfigurationRecord(t.SPS, t.PPS)
	if err != nil {
		p.log.Warn("flv: unusable video config", "stream", p.cfg.StreamName, "err", err)
		return
	}
	p.audMu.Lock()
	defer p.audMu.Unlock()
	p.mu.Lock()
	prevAudio, prevW, prevH, had := p.audioOn, p.tracks.Width, p.tracks.Height, p.haveTr
	p.tracks, p.avcc, p.haveTr = t, avcc, true
	wantAudio := p.cfg.AudioEnabled && p.enc != nil && t.Audio != media.None
	opusOn := wantAudio && p.cfg.Opus
	if opusOn && !audio.OpusAvailable(p.cfg.FFmpegPath) {
		p.log.Warn("flv: ffmpeg with libopus not found, no Opus track", "stream", p.cfg.StreamName)
		opusOn = false
	}
	key := ""
	if wantAudio {
		key = fmt.Sprintf("%p/%v/%d/%d/%x/%v", p.enc, t.Audio, t.AudioRate, t.AudioChannels, t.AudioConfig, opusOn)
	}
	rejoin := key != p.audioKey
	oldLeave := p.leave
	if rejoin {
		p.leave, p.audioKey = nil, key
		p.audioOn, p.opusOn = wantAudio, opusOn
		p.pend, p.hiA, p.hiO = nil, -1, -1
	}
	shape := had && (prevAudio != p.audioOn || prevW != t.Width || prevH != t.Height)
	enc := p.enc
	p.mu.Unlock()
	// Leave and Join run without mu: an encoder's close waits for the reader goroutine, whose
	// last frame may be waiting for mu (Normalizer.detachLocked has the live deadlock).
	if rejoin {
		if oldLeave != nil {
			oldLeave()
		}
		if wantAudio {
			var opusCb func(media.AudioFrame)
			if opusOn {
				opusCb = p.onOpus
			}
			lv := enc.Join(t, true, p.cfg.OpusRate, p.onAAC, opusCb)
			p.mu.Lock()
			p.leave = lv
			p.mu.Unlock()
		}
	}
	if shape {
		p.log.Info("flv: stream shape changed, restarting push", "stream", p.cfg.StreamName)
		select {
		case p.kick <- struct{}{}:
		default:
		}
	}
}

// OnDisconnect: the source dropped, so its PTS clock restarts with the next session. The TCP
// connection stays; the new session is anchored on the monotonic clock again (base kept) and
// resumes at its first key frame.
func (p *AIPortPusher) OnDisconnect(error) {
	p.audMu.Lock()
	defer p.audMu.Unlock()
	p.mu.Lock()
	p.haveTr, p.anchorSet, p.needKey = false, false, true
	p.pend, p.hiA, p.hiO, p.audioKey = nil, -1, -1, ""
	lv := p.leave
	p.leave = nil
	p.mu.Unlock()
	if lv != nil {
		lv()
	}
}

// OnAudio ignores the camera's own frames: audio arrives from the shared encoder.
func (p *AIPortPusher) OnAudio(media.AudioFrame) {}

// OnVideo builds the frame's chunk: for a key frame onMpma, onClockSync and the config tag ahead
// of it; for others a throttled onMpma.
func (p *AIPortPusher) OnVideo(au media.VideoAU) {
	p.mu.Lock()
	defer p.mu.Unlock()
	if !p.haveTr || (p.needKey && !au.IDR) {
		return
	}
	if !p.anchorSet {
		p.anchor, p.anchorSet = int64(p.cfg.Mono())-int64(au.PTS), true
	}
	ns := p.anchor + int64(au.PTS)
	ticks := ticksOf(ns, aiportVideoScale)
	dts := msOf(ticks, aiportVideoScale)
	switch {
	case !p.baseSet:
		p.baseMs, p.baseSet = dts, true // the first video frame fixes the base (FUN_001c4c90)
	case dts < p.lastVDts:
		if !au.IDR || p.lastVDts-dts < aiportResetBackMs {
			return // backward dts is skipped
		}
		p.log.Info("flv: timeline reset", "stream", p.cfg.StreamName, "back_ms", p.lastVDts-dts)
		p.baseMs, p.lastADts = dts, -1
	}
	p.needKey, p.lastVDts = false, dts
	p.tlUpdate(ns, ticks, aiportVideoScale)

	c := chunk{video: true, key: au.IDR, pts: au.PTS, tl: p.tl}
	now := p.cfg.Now()
	if au.IDR {
		c.pre = append(c.pre, p.mpmaTagLocked(true)...)
		c.pre = append(c.pre, p.scriptTagLocked("onClockSync",
			aiportClockSync(p.tl.ms()-p.baseMs, p.baseMs, now.UnixMilli()))...)
		c.wall, c.synced = now, true
		c.cfg = aiportTag(tagVideo, aiportVideoPayload(codecIDAVC, true, true, 0, p.avcc), dts-p.baseMs,
			aiportVideoScale, 0, ticks)
		c.cfgChanged = string(p.avcc) != string(p.lastCfg)
		p.lastCfg = p.avcc
	} else {
		c.pre = p.mpmaTagLocked(false)
	}
	c.body = aiportTag(tagVideo, aiportVideoPayload(codecIDAVC, au.IDR, false, 0, NALUsToAVCC(frameNALUs(au.NALUs))),
		dts-p.baseMs, aiportVideoScale, 0, ticks)
	p.enqueueLocked(c)
	p.flushAudioLocked() // the hold bound has to work when a track went quiet
}

func (tl timeline) ms() int64 { return msOf(tl.ticks, tl.scale) }

// tlUpdate moves the timeline to the latest pts of any track.
func (p *AIPortPusher) tlUpdate(ns int64, ticks uint64, scale uint32) {
	if ns >= p.tl.ns {
		p.tl = timeline{ns, ticks, scale}
	}
}

// scriptTagLocked is a script tag stamped with the timeline's last pts: its timescale, its value,
// tag ts = ms - base (4.3).
func (p *AIPortPusher) scriptTagLocked(name string, v any) []byte {
	return aiportTag(tagScript, ScriptPayload(name, v), p.tl.ms()-p.baseMs, p.tl.scale, 0, p.tl.ticks)
}

// mpmaTagLocked is the onMpma tag if one is due: always before a key frame, otherwise after 500 ms
// when a value changed, else after 10 s (FUN_001c7d20, 4.5).
func (p *AIPortPusher) mpmaTagLocked(force bool) []byte {
	now := p.cfg.Mono()
	since := now - p.mpmaLast
	if !force && p.mpmaSent {
		if !(p.mpmaChanged && since >= aiportMpmaChanged) && since < aiportMpmaIdle {
			return nil
		}
	}
	p.mpmaLast, p.mpmaSent, p.mpmaChanged = now, true, false
	return p.scriptTagLocked("onMpma", aiportMpma(p.mpma))
}

func (p *AIPortPusher) onAAC(f media.AudioFrame)  { p.onAudioFrame(false, f) }
func (p *AIPortPusher) onOpus(f media.AudioFrame) { p.onAudioFrame(true, f) }

func (p *AIPortPusher) onAudioFrame(opus bool, f media.AudioFrame) {
	p.mu.Lock()
	defer p.mu.Unlock()
	if !p.baseSet || !p.anchorSet || len(f.Data) == 0 {
		return // audio is dropped until the video base exists (FUN_001c7fd0)
	}
	if !p.opusOn {
		opus = false
	}
	if p.cfg.AudioCounter {
		f.PTS = p.countedPTS(opus, f.PTS)
	}
	ns := p.anchor + int64(f.PTS)
	if opus {
		p.hiO = max(p.hiO, ns)
	} else {
		p.hiA = max(p.hiA, ns)
	}
	it := aiPending{opus: opus, pts: f.PTS, data: append([]byte(nil), f.Data...), at: time.Now()}
	i := len(p.pend)
	for i > 0 && p.pend[i-1].pts > it.pts {
		i--
	}
	p.pend = append(p.pend, aiPending{})
	copy(p.pend[i+1:], p.pend[i:])
	p.pend[i] = it
	p.flushAudioLocked()
}

func (p *AIPortPusher) flushAudioLocked() {
	ready := p.hiA
	if p.opusOn {
		ready = min(p.hiA, p.hiO)
	}
	for len(p.pend) > 0 {
		h := p.pend[0]
		ns := p.anchor + int64(h.pts)
		if p.opusOn && ns > ready && time.Since(h.at) < p.cfg.AudioHold {
			return
		}
		p.pend = p.pend[1:]
		scale, typ, payload := uint32(p.aacRate()), byte(tagAudio), aacDataPayload(h.data)
		if h.opus {
			scale, typ, payload = uint32(p.cfg.OpusRate), tagOpus, h.data
		}
		ticks := audioTicks(ns, scale)
		dts := msOf(ticks, scale)
		if dts < p.baseMs || dts < p.lastADts {
			p.audioDrops.Add(1)
			continue
		}
		p.lastADts = dts
		p.tlUpdate(ns, ticks, scale)
		p.enqueueLocked(chunk{body: aiportTag(typ, payload, dts-p.baseMs, scale, 0, ticks), tl: p.tl})
	}
}

// enqueueLocked drops the frame when it would take the queue past its limit, logging at most
// every 30 s ("Frames are skipped! The current bandwidth is not enough!", FUN_001d2140). No
// resync follows: the next frame produced is the next one written (5).
func (p *AIPortPusher) enqueueLocked(c chunk) {
	if p.qBytes+c.size() > p.cfg.QueueBytes {
		p.drops.Add(1)
		if now := time.Now(); now.Sub(p.dropLog) >= aiportDropLogGap {
			p.dropLog = now
			p.log.Warn("flv: frames are skipped, the current bandwidth is not enough", "stream", p.cfg.StreamName,
				"queued", p.qBytes, "dropped_total", p.drops.Load())
		}
		return
	}
	p.q = append(p.q, c)
	p.qBytes += c.size()
	select {
	case p.sig <- struct{}{}:
	default:
	}
}

func (p *AIPortPusher) pop() (chunk, bool) {
	p.mu.Lock()
	defer p.mu.Unlock()
	if len(p.q) == 0 {
		return chunk{}, false
	}
	c := p.q[0]
	p.q[0] = chunk{}
	p.q = p.q[1:]
	p.qBytes -= c.size()
	return c, true
}

func (p *AIPortPusher) purge() {
	p.mu.Lock()
	p.q, p.qBytes = nil, 0
	p.mu.Unlock()
}

// meta is onMetaData (4.2): sorted keys, doubles. Audio fields are 0 without audio. The audio
// numbers are the streamer's template (64000 bit/s, 1 channel, 16000 Hz: ubnt_av_dep_mod.sh only
// flips audio.enabled in it) even though the AAC itself is 48 kHz; the decoder takes the real rate
// from the config tag.
func (p *AIPortPusher) metaLocked() Map {
	ord := map[string]int{"video1": 0, "video2": 1, "video3": 2}[p.cfg.Channel]
	sid := []int{1, 2, 4}[ord]
	w, h, fps := p.tracks.Width, p.tracks.Height, p.cfg.FPS
	if w == 0 || h == 0 {
		w, h = p.cfg.Width, p.cfg.Height
	}
	if fps == 0 {
		fps = p.tracks.FPS
	}
	m := Map{
		"audioBandwidth": 0, "audioChannels": 0, "audioFrequency": 0,
		"channelId": ord, "extendedFormat": true, "hasAudio": p.audioOn, "hasVideo": true,
		"streamId": sid, "streamName": p.cfg.StreamName,
		"videoBandwidth": p.cfg.BitrateBps, "videoFps": fps, "videoHeight": h, "videoWidth": w,
	}
	if p.audioOn {
		m["audioBandwidth"], m["audioChannels"], m["audioFrequency"] = 64000, 1, 16000
	}
	return m
}

// run connects, serves one connection until it ends, waits retryInterval, repeats.
func (p *AIPortPusher) run() {
	defer p.wg.Done()
	started := false
	for p.ctx.Err() == nil {
		var first *chunk
		if !p.sentKey.Load() {
			// First connection: header, onMetaData, then the first key frame's own onMpma/
			// onClockSync/config/frame (4.6), so wait for that frame.
			c, ok := p.waitKey()
			if !ok {
				return
			}
			first = &c
		}
		conn, err := p.dial()
		if err != nil {
			p.log.Warn("flv: connect failed", "stream", p.cfg.StreamName, "err", err)
		} else {
			if started {
				p.reconnects.Add(1)
			}
			started = true
			p.serve(conn, first)
		}
		if p.stopped.Load() {
			return
		}
		select {
		case <-p.ctx.Done():
			return
		case <-time.After(p.cfg.RetryInterval):
		}
	}
}

// waitKey discards queued chunks until a key frame's arrives.
func (p *AIPortPusher) waitKey() (chunk, bool) {
	for {
		for {
			c, ok := p.pop()
			if !ok {
				break
			}
			if c.key {
				return c, true
			}
		}
		select {
		case <-p.ctx.Done():
			return chunk{}, false
		case <-p.sig:
		}
	}
}

func (p *AIPortPusher) dial() (net.Conn, error) {
	d := net.Dialer{Timeout: p.cfg.DialTimeout, Control: func(_, _ string, c syscall.RawConn) error {
		return c.Control(func(fd uintptr) {
			if err := syscall.SetsockoptInt(int(fd), syscall.IPPROTO_IP, syscall.IP_TOS, p.cfg.TOS); err != nil {
				p.log.Debug("flv: IP_TOS not set", "err", err)
			}
		})
	}}
	conn, err := d.DialContext(p.ctx, "tcp", p.cfg.Addr)
	if err != nil {
		return nil, err
	}
	p.connMu.Lock()
	p.conn = conn
	p.connMu.Unlock()
	return conn, nil
}

// serve writes one connection's byte stream. first is the key frame chunk of the very first
// connection; otherwise this is a reconnect and the sequence is header, onMetaData, video config,
// AAC config, Opus config, then the next key frame on (4.6).
func (p *AIPortPusher) serve(conn net.Conn, first *chunk) {
	p.connected.Store(true)
	defer func() {
		p.connected.Store(false)
		conn.Close()
	}()
	p.log.Info("flv: push started", "stream", p.cfg.StreamName, "addr", p.cfg.Addr)
	select { // a kick from before this connection is stale
	case <-p.kick:
	default:
	}
	var tee, teeTimes *os.File
	if dir := os.Getenv("DIYPORT_RESTREAMER_TEE"); dir != "" {
		base := filepath.Join(dir, p.cfg.StreamName)
		tee, _ = os.Create(base + ".flv")
		teeTimes, _ = os.Create(base + ".times")
		defer func() {
			for _, f := range []*os.File{tee, teeTimes} {
				if f != nil {
					f.Close()
				}
			}
		}()
	}
	write := func(b []byte) error {
		if tee != nil && teeTimes != nil {
			tee.Write(b)
			fmt.Fprintf(teeTimes, "%d %d\n", time.Now().UnixNano(), len(b))
		}
		conn.SetWriteDeadline(time.Now().Add(30 * time.Second))
		n, err := conn.Write(b)
		p.bytes.Add(uint64(n))
		return err
	}
	dead := make(chan struct{})
	go p.readFeedback(conn, dead)
	if p.cfg.OnStart != nil {
		p.cfg.OnStart()
	}

	cfgSent, needKey := false, false
	p.mu.Lock()
	var head []byte
	if first != nil {
		head = p.headLocked(first.tl)
	} else {
		p.q, p.qBytes = nil, 0 // stale: the next key frame resumes
		needKey = true
		head = p.headLocked(p.tl)
		head = append(head, aiportTag(tagVideo, aiportVideoPayload(codecIDAVC, true, true, 0, p.avcc),
			p.tl.ms()-p.baseMs, p.tl.scale, 0, p.tl.ticks)...)
		head = append(head, p.audioConfigsLocked()...)
		cfgSent = true
	}
	p.mu.Unlock()
	if write(head) != nil {
		return
	}
	writeChunk := func(c chunk) bool {
		if c.video && !c.key && needKey {
			return true
		}
		b := c.pre
		if c.key {
			needKey = false
			if !cfgSent || c.cfgChanged {
				b = append(append([]byte(nil), b...), c.cfg...)
				cfgSent = true
			}
		}
		b = append(append([]byte(nil), b...), c.body...)
		if write(b) != nil {
			return false
		}
		if c.synced && p.cfg.OnSync != nil {
			p.cfg.OnSync(c.pts, c.wall)
		}
		if c.video {
			if p.cfg.OnWrote != nil {
				p.cfg.OnWrote(c.pts)
			}
			p.lastFrame.Store(time.Now().UnixNano())
			p.sentKey.CompareAndSwap(false, c.key)
		}
		return true
	}
	if first != nil {
		if !writeChunk(*first) {
			return
		}
		p.mu.Lock()
		tail := p.audioConfigsLocked() // after the key frame on the first connect
		p.mu.Unlock()
		if len(tail) > 0 && write(tail) != nil {
			return
		}
	}
	for {
		for {
			select {
			case b := <-p.rtt:
				if write(b) != nil {
					return
				}
				continue
			default:
			}
			c, ok := p.pop()
			if !ok {
				break
			}
			if !writeChunk(c) {
				return
			}
		}
		select {
		case <-p.ctx.Done():
			return
		case <-dead:
			return
		case <-p.kick:
			return
		case b := <-p.rtt:
			if write(b) != nil {
				return
			}
		case <-p.sig:
		}
	}
}

// headLocked is the first write of a connection: FLV header and onMetaData, stamped with the
// timeline at tl.
func (p *AIPortPusher) headLocked(tl timeline) []byte {
	if tl.scale == 0 {
		tl.scale = aiportVideoScale
	}
	b := aiportHeader(p.audioOn)
	return append(b, aiportTag(tagScript, ScriptPayload("onMetaData", p.metaLocked()),
		tl.ms()-p.baseMs, tl.scale, 0, tl.ticks)...)
}

// audioConfigsLocked is the AAC config tag, then the Opus one when Opus is on. Both are ts 0 with
// the trailer's pts at the base, so trailer ms - ts is the base here too (the firmware's bytes
// are 08 00 00 04 | 00 00 00 00 | 00 00 00 | AF 00 ASC | 00 00 00 0F, 4.4; the trailer after
// them is our reading of 4.3's "only when extendedFormat").
func (p *AIPortPusher) audioConfigsLocked() []byte {
	if !p.audioOn {
		return nil
	}
	b := aiportTag(tagAudio, aacConfigPayload(ascForRate(p.aacRate())), 0, uint32(p.aacRate()), 0,
		ticksFromMs(p.baseMs, uint32(p.aacRate())))
	if p.opusOn {
		r := uint32(p.cfg.OpusRate)
		b = append(b, aiportTag(tagOpus, opusConfigPayload(p.cfg.OpusRate), 0, r, 0, ticksFromMs(p.baseMs, r))...)
	}
	return b
}

// readFeedback drains the console's frames on the same socket (6), answering type 4 with onRtt.
// It closes dead when the connection ends.
func (p *AIPortPusher) readFeedback(conn net.Conn, dead chan struct{}) {
	defer close(dead)
	var buf, rest []byte
	tmp := make([]byte, 4096)
	for {
		n, err := conn.Read(tmp)
		if n > 0 {
			buf = append(rest, tmp[:n]...)
			var frames []feedback
			frames, rest = parseFeedback(buf)
			rest = append([]byte(nil), rest...)
			for _, f := range frames {
				p.onFeedback(f)
			}
		}
		if err != nil {
			if err != io.EOF && p.ctx.Err() == nil {
				p.log.Debug("flv: feedback read ended", "stream", p.cfg.StreamName, "err", err)
			}
			return
		}
	}
}

func (p *AIPortPusher) onFeedback(f feedback) {
	switch f.typ {
	case 4: // u32 token, u32 timescale, u64 ts
		if len(f.payload) < 16 {
			return
		}
		token := string(f.payload[:4])
		scale := binary.BigEndian.Uint32(f.payload[4:])
		ts := binary.BigEndian.Uint64(f.payload[8:])
		rtt := rttMillis(int64(p.cfg.Mono()), scale, ts)
		p.mu.Lock()
		tl := p.tl
		if tl.scale == 0 {
			tl.scale = aiportVideoScale
		}
		tag := aiportTag(tagScript, ScriptPayload("onRtt", aiportRtt(0, token, rtt)), tl.ms()-p.baseMs, tl.scale, 0, tl.ticks)
		p.mu.Unlock()
		select {
		case p.rtt <- tag:
		default:
		}
	case 3: // the console tells the streamer to stop this stream
		if !p.stopped.Swap(true) {
			p.log.Error("flv: console sent a stop feedback; not reconnecting this stream",
				"stream", p.cfg.StreamName)
		}
	case 5:
		if len(f.payload) >= 2 {
			p.log.Info("flv: console event notification", "stream", p.cfg.StreamName,
				"code", binary.BigEndian.Uint16(f.payload))
		}
	default: // 0 (bandwidth hint), 1, 2 (player hints): no encoder here to adapt; others invalid
		p.log.Debug("flv: feedback ignored", "stream", p.cfg.StreamName, "type", f.typ, "len", len(f.payload))
	}
}

// stripInbandPS: AIPORT_ONVIF_STRIP_PS=1 drops SPS/PPS/SEI/AUD (types 7, 8, 6, 9) from frame tags,
// leaving the parameter sets to the config tag. Live 2026-11-12 22:15: ms's fMP4 of our key
// frames carried an extra 30-byte sample with no slice at the key frame's own timestamp (5 in 6 s
// on _0, 10 in 10 s on _1; a native G4's _1: none). Chrome plays it, Safari's player spins.
var stripInbandPS = os.Getenv("AIPORT_ONVIF_STRIP_PS") == "1"

func frameNALUs(nalus [][]byte) [][]byte {
	if !stripInbandPS {
		return nalus
	}
	out := nalus[:0:0]
	for _, n := range nalus {
		if len(n) == 0 {
			continue
		}
		switch n[0] & 0x1f {
		case 6, 7, 8, 9:
			continue
		}
		out = append(out, n)
	}
	return out
}

func (p *AIPortPusher) aacRate() int {
	if p.cfg.AACRate > 0 {
		return p.cfg.AACRate
	}
	return aiportAACRate
}

// audioCounter is one track's counted clock (AudioConfig.AudioCounter).
type audioCounter struct {
	set   bool
	first time.Duration
	n     int64
}

// countedPTS replaces a frame's pts with first + n*duration (AAC: 1024 samples; Opus: 20 ms).
func (p *AIPortPusher) countedPTS(opus bool, src time.Duration) time.Duration {
	c, dur := &p.cntA, time.Duration(int64(time.Second)*1024/int64(p.aacRate()))
	if opus {
		c, dur = &p.cntO, 20*time.Millisecond
	}
	if c.set {
		got := c.first + time.Duration(c.n)*dur
		if d := got - src; d < 100*time.Millisecond && d > -100*time.Millisecond {
			c.n++
			return got
		}
	}
	c.set, c.first, c.n = true, src, 1
	return src
}
