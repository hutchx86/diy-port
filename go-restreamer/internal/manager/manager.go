// SPDX-License-Identifier: AGPL-3.0-or-later

// Package manager owns the running cameras: the RTSP pool, one router per camera URL (copy
// or transcode), one flv.Pusher per channel, and the DetectServer for video1. It implements
// rpc.Handler, so configure_camera is the idempotent desired state of a camera: re-sent
// unchanged it does nothing, and a changed one restarts only the channels that differ.
package manager

import (
	"log/slog"
	"net"
	"strconv"
	"sync"
	"time"

	"diyport/go-restreamer/internal/audio"
	"diyport/go-restreamer/internal/detect"
	"diyport/go-restreamer/internal/flv"
	"diyport/go-restreamer/internal/rpc"
	"diyport/go-restreamer/internal/rtsp"
	"diyport/go-restreamer/internal/transcode"
)

// Options for New. Zero values are production.
type Options struct {
	Hwaccel string // as flag --hwaccel: auto, videotoolbox, none (default auto)
	// G711AAC re-encodes G.711 to AAC (flag --g711 aac, the default) instead of copying it.
	// Copied G.711 stuttered in Protect's browser audio under the Python push (user,
	// 2026-11-12); Python's AAC re-encode ran on wall-clock timestamps and made video stutter
	// (live 2026-10-28), but this encoder stamps AAC frames by sample count from the first
	// input PTS, so audio keeps the camera's own clock.
	G711AAC bool
	// Opus adds the type-10 Opus track Protect's browser live view plays (flag --opus, default on).
	Opus bool
	// OpusRate forces every channel's Opus sample rate (flag --opus-rate); 0 uses the rate
	// each channel's opus_sample_rate asks for. N > 0 also turns Opus on for a channel Protect
	// asked withOpus for, even with Opus off. A/B knob for live-view tests (2026-11-12).
	OpusRate int
	// NoAudioShare gives every pusher its own audio pipeline (flag --audio-share off), as
	// before the shared Encoder; the zero value shares one ffmpeg per camera URL.
	NoAudioShare bool
	Pace         time.Duration     // flv.Config.Pace for every push (flag --pace-ms)
	Caps         *transcode.Caps   // tests inject; nil probes ffmpeg
	Dialer       rtsp.Dialer       // nil: gortsplib over TCP
	Tune         func(*flv.Config) // tests: shorten retry timers
	Log          *slog.Logger
}

// Manager is the rpc.Handler.
type Manager struct {
	opts  Options
	log   *slog.Logger
	caps  transcode.Caps
	pool  *rtsp.Pool
	start time.Time

	mu   sync.Mutex
	cams map[string]*camera
}

var _ rpc.Handler = (*Manager)(nil)

func New(o Options) *Manager {
	if o.Log == nil {
		o.Log = slog.Default()
	}
	if o.Hwaccel == "" {
		o.Hwaccel = "auto"
	}
	m := &Manager{opts: o, log: o.Log, start: time.Now(), cams: map[string]*camera{}}
	if o.Caps != nil {
		m.caps = *o.Caps
	} else {
		m.caps = transcode.Probe("", o.Hwaccel)
	}
	if m.caps.OK() {
		m.log.Info("hardware H.264 encoder", "backend", m.caps.Backend, "encoder", m.caps.Encoder)
	} else {
		m.log.Warn("no hardware H.264 encoder: H.265 cameras cannot be served", "why", m.caps.Reason)
	}
	m.pool = rtsp.NewPool(o.Dialer, o.Log)
	return m
}

type camera struct {
	id     string
	mode   string
	groups map[string]*group // by rtsp url
	// encs holds one shared audio ffmpeg per rtsp url (CPU: 3 encoders per camera, one per
	// channel, 2026-11-12): channels on one URL share one camera session and PTS clock, so
	// one encoder serves all their pushers. Never shared across URLs. An entry lives while
	// the URL has a group; an idle Encoder runs no ffmpeg.
	encs    map[string]*audio.Encoder
	chans   map[string]*chanState
	order   []string // channels in configure order
	clock   *detect.Clock
	det     *detect.Server
	detSock string
}

type group struct {
	r *router
}

type chanState struct {
	cfg     rpc.Channel
	fan     *fan
	pusher  *flv.Pusher
	started time.Time
}

// pushKey is what a pusher is built from; anything else about a channel is routing.
func pushKey(c rpc.Channel) [4]string {
	return [4]string{c.StreamName, net.JoinHostPort(c.DestHost, strconv.Itoa(c.DestPort)),
		strconv.FormatBool(c.AudioEnabled), opusKey(c)}
}

// opusRate is the rate a channel's Opus is encoded at, 0 when it has none (the Options.Opus
// flag is off, or Protect did not ask for Opus). An unsupported request falls back to 48000.
func (m *Manager) opusRate(cam *camera, c rpc.Channel) int {
	if !c.Opus || (!m.opts.Opus && m.opts.OpusRate <= 0) {
		return 0
	}
	if m.opts.OpusRate > 0 {
		return m.opts.OpusRate
	}
	rate, ok := audio.OpusRateFor(c.OpusSampleRate)
	if !ok {
		m.log.Warn("unsupported opus_sample_rate, using 48000", "device", cam.id, "channel", c.Channel,
			"requested", c.OpusSampleRate)
	}
	return rate
}

// opusKey is the Opus part of a pusher's identity: a change restarts the push, since the
// encode rate and the trailer timescale are fixed per connection.
func opusKey(c rpc.Channel) string {
	if !c.Opus {
		return ""
	}
	rate, _ := audio.OpusRateFor(c.OpusSampleRate)
	return strconv.Itoa(rate)
}

func profile(c rpc.Channel) transcode.Profile {
	return transcode.Profile{Name: c.Channel, Width: c.Width, Height: c.Height, BitrateKbps: c.BitrateKbps}
}

func (m *Manager) newPusher(cam *camera, c rpc.Channel) *flv.Pusher {
	rate := m.opusRate(cam, c)
	cfg := flv.Config{
		Addr:          net.JoinHostPort(c.DestHost, strconv.Itoa(c.DestPort)),
		StreamName:    c.StreamName,
		AudioEnabled:  c.AudioEnabled,
		FFmpegPath:    m.caps.FFmpeg,
		TranscodeG711: m.opts.G711AAC,
		Opus:          rate > 0,
		OpusRate:      rate,
		AudioEncoder:  m.encoder(cam, c.RTSPURL),
		Pace:          m.opts.Pace,
		Log:           m.log.With("device", cam.id, "channel", c.Channel),
	}
	if c.Channel == "video1" {
		// The detection readers' wall times are the ones this push gives Protect, plus the pace
		// hold. Live 2026-11-13 10:37 on Driveway: with Pace 1000 ms boxes led the vehicle by
		// ~1.1 s; with --pace-ms 0 they lined up. Protect places the video a Pace later than the
		// paced wallClock suggests, so detection frames are stamped Pace later to match.
		pace := m.opts.Pace
		cfg.OnStart, cfg.OnWrote = cam.clock.Reset, cam.clock.Wrote
		cfg.OnSync = func(pts time.Duration, wall time.Time) { cam.clock.Synced(pts, wall.Add(pace)) }
	}
	if m.opts.Tune != nil {
		m.opts.Tune(&cfg)
	}
	return flv.NewPusher(cfg)
}

// encoder returns the camera's shared audio encoder for a URL, making it on first use.
func (m *Manager) encoder(cam *camera, url string) *audio.Encoder {
	if m.opts.NoAudioShare {
		return nil // each Normalizer runs its own ffmpeg
	}
	e := cam.encs[url]
	if e == nil {
		e = audio.NewEncoder(m.caps.FFmpeg, m.log.With("device", cam.id))
		cam.encs[url] = e
	}
	return e
}

// Configure applies the desired state of one camera.
func (m *Manager) Configure(p rpc.ConfigureParams) (any, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	mode := p.Mode
	if mode == "" {
		mode = "copy"
	}
	cam := m.cams[p.DeviceID]
	if cam == nil {
		cam = &camera{id: p.DeviceID, mode: mode, groups: map[string]*group{}, encs: map[string]*audio.Encoder{}, chans: map[string]*chanState{},
			clock: detect.NewClock()}
		m.cams[p.DeviceID] = cam
	}
	changed := 0
	if cam.mode != mode {
		cam.mode = mode
		for _, g := range cam.groups {
			g.r.setMode(mode)
		}
		changed++
	}

	want := map[string]rpc.Channel{}
	for _, c := range p.Channels {
		want[c.Channel] = c
	}
	// Gone channels first, so their RTSP sessions are released before new ones are opened (a
	// camera has few to give).
	for _, name := range append([]string(nil), cam.order...) {
		if _, ok := want[name]; !ok {
			m.removeChannel(cam, name)
			changed++
		}
	}
	for _, c := range p.Channels {
		if m.applyChannel(cam, c) {
			changed++
		}
	}
	if m.applyDetect(cam, p.SharedDecode) {
		changed++
	}
	m.log.Info("configure_camera", "device", cam.id, "mode", mode, "channels", len(cam.order), "changed", changed)
	return map[string]any{"ok": true, "changed": changed}, nil
}

// applyChannel makes one channel match c and reports whether anything restarted.
func (m *Manager) applyChannel(cam *camera, c rpc.Channel) bool {
	cs := cam.chans[c.Channel]
	if cs != nil && cs.cfg == c {
		return false
	}
	if cs == nil {
		cs = &chanState{fan: &fan{}, started: time.Now()}
		cs.pusher = m.newPusher(cam, c)
		cs.fan.add(cs.pusher)
		cam.chans[c.Channel] = cs
		cam.order = append(cam.order, c.Channel)
		m.route(cam, cs, c, "")
		cs.cfg = c
		m.log.Info("channel added", "device", cam.id, "channel", c.Channel, "url", rtsp.Redact(c.RTSPURL),
			"dest", net.JoinHostPort(c.DestHost, strconv.Itoa(c.DestPort)))
		return true
	}
	old := cs.cfg
	if old.Request != c.Request {
		cs.pusher.Unpark() // Protect asked for this channel again: try a parked push anew
	}
	if pushKey(old) != pushKey(c) {
		np := m.newPusher(cam, c)
		cs.fan.add(np) // gets the live tracks at once, then waits for its IDR
		cs.fan.remove(cs.pusher)
		cs.pusher.Close()
		cs.pusher = np
		cs.started = time.Now()
		m.log.Info("channel push restarted", "device", cam.id, "channel", c.Channel)
	}
	if old.RTSPURL != c.RTSPURL || profile(old) != profile(c) {
		m.route(cam, cs, c, old.RTSPURL)
	}
	cs.cfg = c
	return true
}

// route puts a channel into the router for its URL, taking it out of oldURL's, and keeps the
// pool subscriptions in step.
func (m *Manager) route(cam *camera, cs *chanState, c rpc.Channel, oldURL string) {
	if oldURL != "" && oldURL != c.RTSPURL {
		m.leave(cam, c.Channel, oldURL)
	}
	g := cam.groups[c.RTSPURL]
	if g == nil {
		g = &group{r: newRouter(cam.id, c.RTSPURL, cam.mode, m.caps, m.log)}
		g.r.enc = m.encoder(cam, c.RTSPURL)
		cam.groups[c.RTSPURL] = g
		cs.pusher.SetAudioEncoder(g.r.enc)
		g.r.set(c.Channel, member{fan: cs.fan, profile: profile(c)})
		m.pool.Acquire(c.RTSPURL, g.r)
		return
	}
	cs.pusher.SetAudioEncoder(g.r.enc) // no-op unless the channel moved URL
	g.r.set(c.Channel, member{fan: cs.fan, profile: profile(c)})
}

func (m *Manager) leave(cam *camera, channel, url string) {
	g := cam.groups[url]
	if g == nil {
		return
	}
	if g.r.del(channel) == 0 {
		m.pool.Release(url, g.r)
		g.r.close()
		g.r.enc.Close()
		delete(cam.groups, url)
		delete(cam.encs, url)
	}
}

func (m *Manager) removeChannel(cam *camera, name string) {
	cs := cam.chans[name]
	if cs == nil {
		return
	}
	m.leave(cam, name, cs.cfg.RTSPURL)
	if name == "video1" && cam.det != nil {
		cs.fan.remove(cam.det)
	}
	cs.fan.remove(cs.pusher)
	cs.pusher.Close()
	delete(cam.chans, name)
	for i, n := range cam.order {
		if n == name {
			cam.order = append(cam.order[:i:i], cam.order[i+1:]...)
			break
		}
	}
	m.log.Info("channel removed", "device", cam.id, "channel", name)
}

// applyDetect keeps the DetectServer matching shared_decode: serving video1 on its socket
// while enabled and video1 exists.
func (m *Manager) applyDetect(cam *camera, sd *rpc.SharedDecode) bool {
	sock := ""
	if sd != nil && sd.Enabled && sd.Socket != "" {
		sock = sd.Socket
	}
	v1 := cam.chans["video1"]
	if v1 == nil {
		sock = ""
	}
	if cam.detSock == sock {
		return false // includes "none wanted, none running"
	}
	if cam.det != nil {
		// video1's fan may be a different one by now; removing from a fan that never held it
		// is a no-op.
		for _, cs := range cam.chans {
			cs.fan.remove(cam.det)
		}
		cam.det.Close()
		cam.det, cam.detSock = nil, ""
	}
	if sock == "" {
		return true
	}
	srv, err := detect.New(sock, cam.clock, m.log.With("device", cam.id))
	if err != nil {
		// Detection falls back to its own RTSP pull; the push goes on.
		m.log.Warn("detection socket failed", "device", cam.id, "err", err)
		return false
	}
	cam.det, cam.detSock = srv, sock
	v1.fan.add(srv)
	return true
}

// Remove drops a camera and everything it runs.
func (m *Manager) Remove(deviceID string) (any, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	cam := m.cams[deviceID]
	if cam == nil {
		return map[string]any{"ok": true, "removed": false}, nil
	}
	m.removeCamera(cam)
	delete(m.cams, deviceID)
	m.log.Info("camera removed", "device", deviceID)
	return map[string]any{"ok": true, "removed": true}, nil
}

func (m *Manager) removeCamera(cam *camera) {
	if cam.det != nil {
		for _, cs := range cam.chans {
			cs.fan.remove(cam.det)
		}
		cam.det.Close()
		cam.det = nil
	}
	for _, name := range append([]string(nil), cam.order...) {
		m.removeChannel(cam, name)
	}
}

// Close stops every camera.
func (m *Manager) Close() {
	m.mu.Lock()
	defer m.mu.Unlock()
	for id, cam := range m.cams {
		m.removeCamera(cam)
		delete(m.cams, id)
	}
}

func ageMs(t time.Time) *int64 {
	if t.IsZero() {
		return nil
	}
	v := time.Since(t).Milliseconds()
	return &v
}

// Stats is the get_stats result. URLs are redacted.
func (m *Manager) Stats() any {
	m.mu.Lock()
	defer m.mu.Unlock()
	st := rpc.Stats{Cameras: map[string]rpc.CameraStats{}, UptimeMs: time.Since(m.start).Milliseconds(),
		Hwaccel: rpc.HwStats{Requested: m.opts.Hwaccel, Backend: m.caps.Backend, Encoder: m.caps.Encoder,
			FFmpeg: m.caps.FFmpeg, Reason: m.caps.Reason}}
	for id, cam := range m.cams {
		cst := rpc.CameraStats{Mode: cam.mode, Channels: []rpc.ChannelStats{}}
		for _, name := range cam.order {
			cs := cam.chans[name]
			ps := cs.pusher.Stats()
			c := rpc.ChannelStats{Channel: name, StreamName: cs.cfg.StreamName, URL: rtsp.Redact(cs.cfg.RTSPURL),
				BytesSent: ps.BytesSent, LastFrameAgeMs: ageMs(ps.LastFrameTime), Reconnects: ps.Reconnects,
				Connected: ps.Connected, UptimeMs: time.Since(cs.started).Milliseconds(), Error: cs.fan.reason()}
			if !ps.LastFrameTime.IsZero() {
				v := ps.LastFrameTime.UnixMilli()
				c.LastSentUnixMs = &v
			}
			cst.Channels = append(cst.Channels, c)
		}
		st.Cameras[id] = cst
		for url, g := range cam.groups {
			if ts, ok := g.r.tcStats(); ok {
				st.Transcode = append(st.Transcode, rpc.TCStats{DeviceID: id, URL: rtsp.Redact(url),
					Mode: string(ts.Mode), Restarts: ts.Restarts, Frames: ts.Frames})
			}
		}
	}
	st.RTSP = []rpc.RTSPStats{}
	for _, s := range m.pool.Stats() {
		st.RTSP = append(st.RTSP, rpc.RTSPStats{URL: s.URL, Subscribers: s.Subscribers, Sessions: s.Sessions,
			Reconnects: s.Reconnects, Connected: s.Connected, LastFrameAgeMs: ageMs(s.LastFrame)})
	}
	return st
}
