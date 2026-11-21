// SPDX-License-Identifier: AGPL-3.0-or-later

package transcode

import (
	"bytes"
	"context"
	"fmt"
	"io"
	"log/slog"
	"os"
	"os/exec"
	"sync"
	"sync/atomic"
	"syscall"
	"time"

	"github.com/bluenviron/mediacommon/v2/pkg/codecs/h264"

	"diyport/go-restreamer/internal/media"
)

// Profile is one output: what a channel's encoder produces. Zero Width/Height keeps the
// source size; zero BitrateKbps means 2000; zero GOP means one second of frames.
type Profile struct {
	Name        string
	Width       int
	Height      int
	BitrateKbps int
	GOP         int
}

// Config for one camera's Transcoder.
type Config struct {
	Name     string // for logs only
	Caps     Caps   // from Probe; zero value means Probe("", "auto") is run
	Profiles []Profile
	Log      *slog.Logger
}

// Mode is what the transcoder is doing with the current source.
type Mode string

const (
	ModeIdle        Mode = "idle"        // no source yet
	ModeTranscode   Mode = "transcode"   // ffmpeg decodes once, encodes each profile
	ModeCopy        Mode = "copy"        // H.264 and no hardware encoder: passed through
	ModeUnsupported Mode = "unsupported" // H.265 and no hardware encoder
)

// Stats is a snapshot for get_stats.
type Stats struct {
	Mode     Mode
	Backend  string
	Restarts int // ffmpeg restarts after the first start
	Frames   map[string]int64
}

const (
	inQueue     = 90               // source AUs buffered ahead of ffmpeg: ~4 s at 20 fps, then drop to the next IDR
	stallAfter  = 20 * time.Second // input flowing, no output: ffmpeg is wedged
	okRunLength = 10 * time.Second // an ffmpeg that ran this long resets the restart backoff
	maxBackoff  = 10 * time.Second
	defaultFPS  = 20 // as onvif_push.py's `src["fps"] or 20`
)

// Transcoder is a media.Subscriber (of the rtsp pool) and a producer for each profile's
// downstream subscribers, attached with Output. Audio passes through untouched, on the source
// PTS. Safe for concurrent use; Source calls come from one goroutine as media.Subscriber says.
type Transcoder struct {
	cfg  Config
	log  *slog.Logger
	caps Caps

	mu   sync.Mutex // guards the fields below and serializes Source calls with Output/Close
	src  *media.Tracks
	mode Mode
	run  *run
	outs map[string]*output
	// restarts counts across runs.
	restarts atomic.Int64
	closed   bool
}

// New builds a Transcoder; ffmpeg isn't started until the source's tracks arrive.
func New(cfg Config) *Transcoder {
	if cfg.Log == nil {
		cfg.Log = slog.Default()
	}
	if cfg.Caps.FFmpeg == "" && cfg.Caps.Backend == "" && cfg.Caps.Reason == "" {
		cfg.Caps = Probe("", "auto")
	}
	t := &Transcoder{cfg: cfg, log: cfg.Log.With("camera", cfg.Name), caps: cfg.Caps, mode: ModeIdle,
		outs: map[string]*output{}}
	for _, p := range cfg.Profiles {
		t.outs[p.Name] = &output{profile: p}
	}
	return t
}

// Check says whether a source of this codec can be served: ErrUnsupportedHEVC for H.265
// without a hardware encoder. Use it to fail configure_camera early.
func (t *Transcoder) Check(c media.Codec) error {
	if c == media.H265 && !t.caps.OK() {
		return ErrUnsupportedHEVC
	}
	return nil
}

// Output subscribes sub to a profile's H.264. Unknown profile names are ignored with a log.
func (t *Transcoder) Output(profile string, sub media.Subscriber) {
	t.mu.Lock()
	defer t.mu.Unlock()
	o := t.outs[profile]
	if o == nil {
		t.log.Warn("transcode: no such profile", "profile", profile)
		return
	}
	o.mu.Lock()
	o.subs = append(o.subs, sub)
	o.mu.Unlock()
	if t.src != nil && t.mode == ModeCopy {
		o.sendTracks(*t.src, true)
	}
}

// Stats returns a snapshot.
func (t *Transcoder) Stats() Stats {
	t.mu.Lock()
	defer t.mu.Unlock()
	s := Stats{Mode: t.mode, Backend: t.caps.Backend, Restarts: int(t.restarts.Load()), Frames: map[string]int64{}}
	for n, o := range t.outs {
		s.Frames[n] = o.frames.Load()
	}
	return s
}

// Close stops ffmpeg. The transcoder is unusable afterwards.
func (t *Transcoder) Close() {
	t.mu.Lock()
	t.closed = true
	r := t.run
	t.run = nil
	t.mu.Unlock()
	if r != nil {
		r.stop()
	}
}

// OnTracks starts (or keeps, or restarts) the pipeline for the source's video.
func (t *Transcoder) OnTracks(tr media.Tracks) {
	t.mu.Lock()
	defer t.mu.Unlock()
	if t.closed {
		return
	}
	prev := t.src
	t.src = &tr
	switch {
	case tr.Video == media.H265 && !t.caps.OK(), tr.Video != media.H264 && tr.Video != media.H265:
		t.setMode(ModeUnsupported)
		t.stopRun()
		t.log.Error("transcode: unsupported source", "codec", tr.Video, "backend", t.caps.Backend, "why", t.caps.Reason)
		for _, o := range t.outs {
			o.disconnect(ErrUnsupportedHEVC)
		}
		return
	case !t.caps.OK():
		// H.264 and nothing to encode with: the camera's own stream goes out as is.
		t.setMode(ModeCopy)
		t.stopRun()
		for _, o := range t.outs {
			o.sendTracks(tr, true)
		}
		return
	}
	t.setMode(ModeTranscode)
	if t.run != nil && prev != nil && prev.Video == tr.Video && prev.Width == tr.Width && prev.Height == tr.Height {
		// Same picture: in-band parameter sets and the audio description may have changed, but
		// the decoder follows those on its own.
		t.run.setSource(tr)
		for _, o := range t.outs {
			o.audioChanged(tr)
		}
		return
	}
	t.stopRun()
	t.run = t.startRun(tr)
}

func (t *Transcoder) setMode(m Mode) { t.mode = m }

// stopRun must be called with t.mu held. It waits for the run's goroutines, which only take
// output locks, never t.mu.
func (t *Transcoder) stopRun() {
	if t.run != nil {
		r := t.run
		t.run = nil
		r.stop()
	}
}

// OnVideo queues an AU for ffmpeg (or forwards it in copy mode). Never blocks.
func (t *Transcoder) OnVideo(au media.VideoAU) {
	t.mu.Lock()
	r, mode := t.run, t.mode
	t.mu.Unlock()
	switch mode {
	case ModeTranscode:
		if r != nil {
			r.push(au)
		}
	case ModeCopy:
		for _, o := range t.outs {
			o.video(au)
		}
	}
}

// OnAudio passes audio to every output that has had its tracks.
func (t *Transcoder) OnAudio(f media.AudioFrame) {
	t.mu.Lock()
	mode := t.mode
	t.mu.Unlock()
	if mode != ModeTranscode && mode != ModeCopy {
		return
	}
	for _, o := range t.outs {
		o.audio(f)
	}
}

// OnDisconnect: the source is gone; ffmpeg has nothing to decode. Outputs hear it too, and
// get OnTracks again when the source comes back.
func (t *Transcoder) OnDisconnect(err error) {
	t.mu.Lock()
	t.stopRun()
	t.src = nil
	t.mode = ModeIdle
	t.mu.Unlock()
	for _, o := range t.outs {
		o.disconnect(err)
	}
}

// output is one profile and its downstream subscribers. Calls into subscribers are
// serialized by mu (video from the ffmpeg reader, audio from the source), each stream in order.
type output struct {
	profile Profile
	frames  atomic.Int64

	mu       sync.Mutex
	subs     []media.Subscriber
	sentKey  string // what the subscribers were last told; "" when they have no tracks
	sent     media.Tracks
	audioKey string
}

func audioKey(t media.Tracks) string {
	return fmt.Sprintf("%v/%d/%d/%x", t.Audio, t.AudioRate, t.AudioChannels, t.AudioConfig)
}

// sendTracks tells subscribers about the stream. In copy mode that's the source's tracks
// verbatim; in transcode mode h264 supplies the output's own parameter sets.
func (o *output) sendTracks(t media.Tracks, force bool) {
	o.mu.Lock()
	defer o.mu.Unlock()
	key := fmt.Sprintf("%x/%x/%x/", t.VPS, t.SPS, t.PPS) + audioKey(t)
	if !force && key == o.sentKey {
		return
	}
	o.sentKey, o.sent, o.audioKey = key, t, audioKey(t)
	for _, s := range o.subs {
		s.OnTracks(t)
	}
}

// audioChanged resends tracks (same video, new audio description) once the output has some.
func (o *output) audioChanged(src media.Tracks) {
	o.mu.Lock()
	if o.sentKey == "" || audioKey(src) == o.audioKey {
		o.mu.Unlock()
		return
	}
	nt := o.sent
	nt.Audio, nt.AudioRate, nt.AudioChannels, nt.AudioConfig = src.Audio, src.AudioRate, src.AudioChannels, src.AudioConfig
	o.mu.Unlock()
	o.sendTracks(nt, true)
}

func (o *output) video(au media.VideoAU) {
	o.mu.Lock()
	defer o.mu.Unlock()
	if o.sentKey == "" {
		return
	}
	o.frames.Add(1)
	for _, s := range o.subs {
		s.OnVideo(au)
	}
}

func (o *output) audio(f media.AudioFrame) {
	o.mu.Lock()
	defer o.mu.Unlock()
	if o.sentKey == "" {
		return
	}
	for _, s := range o.subs {
		s.OnAudio(f)
	}
}

func (o *output) disconnect(err error) {
	o.mu.Lock()
	defer o.mu.Unlock()
	o.sentKey = ""
	for _, s := range o.subs {
		s.OnDisconnect(err)
	}
}

// run is one supervised ffmpeg (restarted on exit) for one source picture.
type run struct {
	t      *Transcoder
	cancel context.CancelFunc
	done   chan struct{}

	in      chan media.VideoAU
	needIDR atomic.Bool // set when a drop left ffmpeg's input with a hole
	srcMu   sync.Mutex
	src     media.Tracks
	ptsMu   sync.Mutex
	ptsQ    [][]time.Duration // per profile: source PTS of AUs written, popped as that profile's AUs come out (no B-frames, no drops: 1:1)
	lastPTS []time.Duration
	fps     float64
}

func (t *Transcoder) startRun(src media.Tracks) *run {
	ctx, cancel := context.WithCancel(context.Background())
	r := &run{t: t, cancel: cancel, done: make(chan struct{}), in: make(chan media.VideoAU, inQueue), src: src}
	r.fps = src.FPS
	if r.fps < 1 || r.fps > 120 {
		r.fps = defaultFPS
	}
	r.ptsQ = make([][]time.Duration, len(t.cfg.Profiles))
	r.lastPTS = make([]time.Duration, len(t.cfg.Profiles))
	r.needIDR.Store(true)
	go r.supervise(ctx)
	return r
}

func (r *run) setSource(tr media.Tracks) { r.srcMu.Lock(); r.src = tr; r.srcMu.Unlock() }
func (r *run) source() media.Tracks      { r.srcMu.Lock(); defer r.srcMu.Unlock(); return r.src }

func (r *run) stop() { r.cancel(); <-r.done }

func (r *run) push(au media.VideoAU) {
	if r.needIDR.Load() {
		if !au.IDR {
			return
		}
		r.needIDR.Store(false)
	}
	select {
	case r.in <- au:
	default:
		// ffmpeg is behind: drop, and don't feed it P frames that reference the hole.
		r.needIDR.Store(true)
	}
}

// supervise restarts ffmpeg with backoff until stopped.
func (r *run) supervise(ctx context.Context) {
	defer close(r.done)
	backoff := time.Second
	for first := true; ctx.Err() == nil; first = false {
		if !first {
			r.t.restarts.Add(1)
		}
		start := time.Now()
		err := r.once(ctx)
		if ctx.Err() != nil {
			return
		}
		r.t.log.Warn("transcode: ffmpeg exited, restarting", "err", err, "in", backoff)
		if time.Since(start) > okRunLength {
			backoff = time.Second
		}
		select {
		case <-ctx.Done():
			return
		case <-time.After(backoff):
		}
		if backoff *= 2; backoff > maxBackoff {
			backoff = maxBackoff
		}
		// Whatever queued while ffmpeg was down is stale: start again from a fresh IDR.
		r.needIDR.Store(true)
		r.drain()
	}
}

func (r *run) drain() {
	for {
		select {
		case <-r.in:
		default:
			return
		}
	}
}

// capBuf keeps the tail of ffmpeg's stderr for the exit log.
type capBuf struct {
	mu sync.Mutex
	b  []byte
}

func (c *capBuf) Write(p []byte) (int, error) {
	c.mu.Lock()
	c.b = append(c.b, p...)
	if len(c.b) > 2048 {
		c.b = c.b[len(c.b)-2048:]
	}
	c.mu.Unlock()
	return len(p), nil
}
func (c *capBuf) String() string {
	c.mu.Lock()
	defer c.mu.Unlock()
	return string(bytes.TrimSpace(c.b))
}

// once runs one ffmpeg until it exits, the stall watchdog fires or ctx ends.
func (r *run) once(ctx context.Context) error {
	t := r.t
	src := r.source()
	r.ptsMu.Lock()
	for i := range r.ptsQ {
		r.ptsQ[i] = nil // a new ffmpeg's first output is its first input
	}
	r.ptsMu.Unlock()
	args := buildArgs(t.caps, src, r.fps, t.cfg.Profiles)
	cmd := exec.Command(t.caps.FFmpeg, args...)
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	stderr := &capBuf{}
	cmd.Stderr = stderr
	stdin, err := cmd.StdinPipe()
	if err != nil {
		return err
	}
	type pipe struct{ r, w *os.File }
	pipes := make([]pipe, len(t.cfg.Profiles))
	for i := range pipes {
		pr, pw, err := os.Pipe()
		if err != nil {
			return err
		}
		pipes[i] = pipe{pr, pw}
		cmd.ExtraFiles = append(cmd.ExtraFiles, pw)
	}
	closePipes := func() {
		for _, p := range pipes {
			p.r.Close()
			p.w.Close()
		}
	}
	if err := cmd.Start(); err != nil {
		closePipes()
		return err
	}
	for _, p := range pipes {
		p.w.Close() // the child has its copy; ours must go so EOF arrives
	}
	t.log.Info("transcode: ffmpeg started", "backend", t.caps.Backend, "pid", cmd.Process.Pid,
		"codec", src.Video, "w", src.Width, "h", src.Height, "profiles", len(pipes))

	var lastOut atomic.Int64
	lastOut.Store(time.Now().UnixNano())
	var wg sync.WaitGroup
	for i, p := range pipes {
		wg.Add(1)
		go func(i int, o *output, rd *os.File) {
			defer wg.Done()
			r.readOutput(i, o, rd, &lastOut)
		}(i, t.outs[t.cfg.Profiles[i].Name], p.r)
	}
	waitCh := make(chan error, 1)
	go func() { wg.Wait(); waitCh <- cmd.Wait() }()

	kill := func() { _ = syscall.Kill(-cmd.Process.Pid, syscall.SIGKILL) }
	var needIDR = true
	var lastIn time.Time
	tick := time.NewTicker(2 * time.Second)
	defer tick.Stop()
	var buf []byte
	for {
		select {
		case <-ctx.Done():
			kill()
			stdin.Close()
			<-waitCh
			return nil
		case err := <-waitCh:
			stdin.Close()
			if err == nil {
				err = io.EOF
			}
			return fmt.Errorf("%v: %s", err, stderr.String())
		case <-tick.C:
			if !lastIn.IsZero() && time.Since(lastIn) < 5*time.Second &&
				time.Since(time.Unix(0, lastOut.Load())) > stallAfter {
				t.log.Warn("transcode: ffmpeg produced no output, killing it")
				kill()
			}
		case au := <-r.in:
			if needIDR && !au.IDR {
				continue
			}
			needIDR = false
			s := r.source()
			buf = buf[:0]
			if au.IDR && !hasParamSets(au, s.Video) {
				buf = annexB(buf, s.VPS, s.SPS, s.PPS)
			}
			buf = annexB(buf, au.NALUs...)
			r.ptsMu.Lock()
			for i := range r.ptsQ {
				r.ptsQ[i] = append(r.ptsQ[i], au.PTS)
				if len(r.ptsQ[i]) > 512 {
					r.ptsQ[i] = r.ptsQ[i][len(r.ptsQ[i])-256:]
				}
			}
			r.ptsMu.Unlock()
			lastIn = time.Now()
			if _, err := stdin.Write(buf); err != nil {
				// ffmpeg is gone; the waitCh case reports why.
				kill()
				<-waitCh
				stdin.Close()
				return fmt.Errorf("stdin: %v: %s", err, stderr.String())
			}
		}
	}
}

func hasParamSets(au media.VideoAU, c media.Codec) bool {
	for _, n := range au.NALUs {
		if len(n) == 0 {
			continue
		}
		if c == media.H265 {
			if ty := (n[0] >> 1) & 0x3f; ty == 33 {
				return true
			}
		} else if n[0]&0x1f == 7 {
			return true
		}
	}
	return false
}

// readOutput splits one profile's output pipe into AUs and delivers them.
func (r *run) readOutput(idx int, o *output, rd *os.File, lastOut *atomic.Int64) {
	t := r.t
	spl := &auSplitter{}
	var sps, pps []byte
	spl.emit = func(nalus [][]byte) {
		lastOut.Store(time.Now().UnixNano())
		au := media.VideoAU{NALUs: nalus}
		for _, n := range nalus {
			switch n[0] & 0x1f {
			case 5:
				au.IDR = true
			case 7:
				sps = n
			case 8:
				pps = n
			}
		}
		au.PTS = r.nextPTS(idx)
		if au.IDR && sps != nil && pps != nil {
			s := r.source()
			tr := media.Tracks{Video: media.H264, SPS: sps, PPS: pps, FPS: r.fps,
				Audio: s.Audio, AudioRate: s.AudioRate, AudioChannels: s.AudioChannels, AudioConfig: s.AudioConfig}
			var parsed h264.SPS
			if parsed.Unmarshal(sps) == nil {
				tr.Width, tr.Height = parsed.Width(), parsed.Height()
			}
			o.sendTracks(tr, false)
		}
		o.video(au)
	}
	buf := make([]byte, 64<<10)
	for {
		n, err := rd.Read(buf)
		if n > 0 {
			spl.write(buf[:n])
		}
		if err != nil {
			rd.Close()
			t.log.Debug("transcode: output closed", "profile", o.profile.Name)
			return
		}
	}
}

// nextPTS pairs an output AU with the source AU it came from: h264_videotoolbox with -bf 0
// and -fps_mode passthrough emits one AU per input frame, in order.
func (r *run) nextPTS(idx int) time.Duration {
	r.ptsMu.Lock()
	defer r.ptsMu.Unlock()
	if len(r.ptsQ[idx]) == 0 {
		r.lastPTS[idx] += time.Duration(float64(time.Second) / r.fps)
		return r.lastPTS[idx]
	}
	p := r.ptsQ[idx][0]
	r.ptsQ[idx] = r.ptsQ[idx][1:]
	r.lastPTS[idx] = p
	return p
}
