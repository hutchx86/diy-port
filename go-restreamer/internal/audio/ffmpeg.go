// SPDX-License-Identifier: AGPL-3.0-or-later

package audio

import (
	"bytes"
	"encoding/binary"
	"fmt"
	"io"
	"log/slog"
	"os"
	"os/exec"
	"strconv"
	"sync"
	"time"

	"diyport/go-restreamer/internal/media"
)

func ffmpegPath(p string) string {
	if p == "" {
		p = "ffmpeg"
	}
	if path, err := exec.LookPath(p); err == nil {
		return path
	}
	return ""
}

func ffmpegAvailable(p string) bool { return ffmpegPath(p) != "" }

// transcoder is one supervised ffmpeg: camera payloads on stdin, ADTS AAC on stdout (when out
// is set), split back into raw AAC frames, and Ogg Opus on fd 3 (when opus is set), split into
// raw 20 ms packets. A camera AAC feed (Opus-only use: AAC itself is passed through) gets ADTS
// headers added from its AudioSpecificConfig. A crash or exit restarts it with a 1..10 s backoff; frames that
// arrive while it is down are dropped (the Normalizer does not fall back to silence mid-stream,
// a gap is better than a second track clock).
type transcoder struct {
	cfg   Config
	codec media.Codec
	rate  int
	chans int
	asc   []byte                 // camera AAC AudioSpecificConfig, for ADTS headers
	out   func(media.AudioFrame) // AAC frames; nil: no AAC output
	opus  func(media.AudioFrame) // Opus packets; nil: no Opus output
	log   *slog.Logger

	in   chan media.AudioFrame
	done chan struct{}
	once sync.Once
	wg   sync.WaitGroup
}

func newTranscoder(cfg Config, t media.Tracks, out, opus func(media.AudioFrame), lg *slog.Logger) *transcoder {
	tc := &transcoder{cfg: cfg, codec: t.Audio, rate: t.AudioRate, chans: t.AudioChannels, asc: t.AudioConfig,
		out: out, opus: opus, log: lg, in: make(chan media.AudioFrame, 256), done: make(chan struct{})}
	if tc.codec == media.PCMU || tc.codec == media.PCMA {
		tc.rate, tc.chans = 8000, 1
	}
	if tc.codec == media.AAC && tc.rate == 0 {
		tc.rate = ASCRate(tc.asc)
	}
	if tc.rate == 0 {
		tc.rate = 48000
	}
	if tc.chans == 0 {
		tc.chans = 1
	}
	tc.wg.Add(1)
	go tc.supervise()
	return tc
}

func (tc *transcoder) write(f media.AudioFrame) {
	select {
	case tc.in <- media.AudioFrame{PTS: f.PTS, Data: append([]byte(nil), f.Data...)}:
	default: // ffmpeg is behind or down: drop rather than stall the video path
	}
}

func (tc *transcoder) close() {
	tc.once.Do(func() { close(tc.done) })
	tc.wg.Wait()
}

func (tc *transcoder) args() []string {
	a := []string{"-nostdin", "-loglevel", "error"}
	switch tc.codec {
	case media.PCMU:
		a = append(a, "-f", "mulaw", "-ar", "8000", "-ac", "1")
	case media.PCMA:
		a = append(a, "-f", "alaw", "-ar", "8000", "-ac", "1")
	case media.AAC:
		a = append(a, "-f", "aac") // ADTS, headers added in feed
	default: // Opus is fed as Ogg: ffmpeg has no raw Opus demuxer
		a = append(a, "-f", "ogg")
	}
	a = append(a, "-i", "pipe:0")
	// -flush_packets 1: each frame leaves at once, not in pipe-buffer bursts that reach
	// Protect as clumps of audio tags.
	if tc.out != nil {
		a = append(a, "-vn", "-flush_packets", "1", "-c:a", "aac", "-ar", strconv.Itoa(tc.cfg.aacRate()), "-ac", "1",
			"-b:a", strconv.Itoa(tc.cfg.aacBitrate()/1000)+"k", "-f", "adts", "pipe:1")
	}
	if tc.opus != nil {
		// yi-protect main.cpp: 48 kHz mono (cfg.OpusRate: the rate Protect asked for; packet durations and the Ogg granule stay on the 48 kHz clock), 20 ms frames, 64 kbps. -page_duration 20000 us:
		// one packet per Ogg page, so packets leave ffmpeg one by one.
		a = append(a, "-vn", "-flush_packets", "1", "-c:a", "libopus", "-ar", strconv.Itoa(tc.cfg.opusRate()), "-ac", "1",
			"-b:a", "64k", "-application", "audio", "-frame_duration", "20",
			"-f", "ogg", "-page_duration", "20000", "pipe:3")
	}
	return a
}

// inRate is the sample rate that input positions (inputClock) are counted in.
func (tc *transcoder) inRate() int {
	switch tc.codec {
	case media.PCMU, media.PCMA:
		return 8000
	case media.AAC:
		return tc.rate
	}
	return OpusRate
}

// inSamples is how many samples one camera frame carries: G.711 one byte per sample, AAC
// 1024 per frame, Opus by its TOC byte (48 kHz).
func (tc *transcoder) inSamples(data []byte) int {
	switch tc.codec {
	case media.PCMU, media.PCMA:
		return len(data)
	case media.AAC:
		return aacFrameLen
	}
	return opusSamples(data)
}

func (tc *transcoder) supervise() {
	defer tc.wg.Done()
	delay := time.Second
	for {
		started := time.Now()
		err := tc.runOnce()
		select {
		case <-tc.done:
			return
		default:
		}
		if time.Since(started) > time.Minute {
			delay = time.Second
		}
		tc.log.Warn("audio: ffmpeg ended, restarting", "err", err, "in", delay)
		select {
		case <-tc.done:
			return
		case <-time.After(delay):
		}
		delay = min(delay*2, 10*time.Second)
	}
}

func (tc *transcoder) runOnce() error {
	aacRate := int64(tc.cfg.aacRate())
	cmd := exec.Command(ffmpegPath(tc.cfg.FFmpegPath), tc.args()...)
	stdin, err := cmd.StdinPipe()
	if err != nil {
		return err
	}
	var stdout io.ReadCloser
	if tc.out != nil {
		if stdout, err = cmd.StdoutPipe(); err != nil {
			return err
		}
	}
	var opusR *os.File
	if tc.opus != nil {
		var opusW *os.File
		if opusR, opusW, err = os.Pipe(); err != nil {
			return err
		}
		cmd.ExtraFiles = []*os.File{opusW} // fd 3 in the child
		defer opusR.Close()
		defer opusW.Close() // the parent's copy; closed after Start so EOF reaches the reader
	}
	var stderr bytes.Buffer
	cmd.Stderr = &limitWriter{w: &stderr, n: 2048}
	if err := cmd.Start(); err != nil {
		return err
	}
	if tc.opus != nil {
		cmd.ExtraFiles[0].Close()
	}
	stopped := make(chan struct{})
	var base atomicDuration                   // PTS of the first input; output PTS counts samples from it
	anchors := &inputClock{rate: tc.inRate()} // input sample position -> camera PTS
	go func() {
		select {
		case <-tc.done:
			cmd.Process.Kill()
		case <-stopped:
		}
	}()
	var readers sync.WaitGroup
	// Reader: ADTS -> raw frames, PTS = first input PTS + samples emitted.
	if stdout != nil {
		readers.Add(1)
		go func() {
			defer readers.Done()
			var samples int64
			r := &adtsReader{r: stdout}
			for {
				frame, err := r.next()
				if err != nil {
					return
				}
				pts := base.get() + time.Duration(samples)*time.Second/time.Duration(aacRate)
				if tc.codec == media.PCMU || tc.codec == media.PCMA {
					// G.711 is one byte per 8 kHz sample, so output sample n came from input
					// sample n*8000/AACRate: stamp it with that input's camera PTS. A pure sample
					// count drifts from video by every lost RTP packet, every frame dropped while
					// ffmpeg was behind, and the camera clock's skew; Protect's browser audio
					// stuttered on the counted version (user, live 2026-11-12 on a Hikvision camera).
					if at, ok := anchors.at(samples * 8000 / aacRate); ok {
						pts = at
					}
				}
				samples += aacFrameLen
				tc.out(media.AudioFrame{PTS: pts, Data: frame})
			}
		}()
	}
	// Reader: Ogg -> raw Opus packets. Packet k covers 48 kHz samples [k*960, (k+1)*960), so
	// it is stamped with the camera PTS of the input sample at that position: a perfectly
	// regular grid wherever the camera's clock is, and gaps only where input really was
	// lost (the same rule as the G.711 AAC stamps above; Protect's web live audio garbles on
	// bursty stamps, yi-protect FlvPush.cpp "Fallback Opus PTS: a steady 20 ms grid").
	if opusR != nil {
		readers.Add(1)
		go func() {
			defer readers.Done()
			var pos int64 // 48 kHz samples emitted
			r := &oggPackets{r: opusR}
			for {
				pkt, err := r.next()
				if err != nil {
					return
				}
				pts := base.get() + time.Duration(pos)*time.Second/OpusRate
				if at, ok := anchors.at(pos * int64(tc.inRate()) / OpusRate); ok {
					pts = at
				}
				pos += int64(opusSamples(pkt))
				tc.opus(media.AudioFrame{PTS: pts, Data: pkt})
			}
		}()
	}
	err = tc.feed(stdin, &base, anchors)
	stdin.Close()
	if err != nil {
		cmd.Process.Kill()
	}
	werr := cmd.Wait()
	close(stopped)
	readers.Wait()
	if err == nil {
		err = werr
	}
	if s := bytes.TrimSpace(stderr.Bytes()); len(s) > 0 {
		err = fmt.Errorf("%v: %s", err, s)
	}
	return err
}

// feed copies queued frames to ffmpeg's stdin until it fails or the transcoder closes.
func (tc *transcoder) feed(w io.Writer, base *atomicDuration, anchors *inputClock) error {
	var ogg *oggOpus
	first := true
	for {
		select {
		case <-tc.done:
			return nil
		case f := <-tc.in:
			if first {
				base.set(f.PTS)
				first = false
				if tc.codec == media.Opus {
					ogg = newOggOpus(tc.chans, tc.rate)
					if _, err := w.Write(ogg.headers()); err != nil {
						return err
					}
				}
			}
			data := f.Data
			anchors.add(f.PTS, tc.inSamples(data))
			if ogg != nil {
				data = ogg.packet(f.Data)
			} else if tc.codec == media.AAC {
				h, ok := adtsHeader(tc.asc, len(data))
				if !ok {
					continue
				}
				data = append(h[:], data...)
			}
			if _, err := w.Write(data); err != nil {
				return err
			}
		}
	}
}

type limitWriter struct {
	w io.Writer
	n int
}

func (l *limitWriter) Write(p []byte) (int, error) {
	if l.n > 0 {
		m := min(len(p), l.n)
		l.w.Write(p[:m])
		l.n -= m
	}
	return len(p), nil
}

type atomicDuration struct {
	mu sync.Mutex
	d  time.Duration
}

func (a *atomicDuration) set(d time.Duration) { a.mu.Lock(); a.d = d; a.mu.Unlock() }
func (a *atomicDuration) get() time.Duration  { a.mu.Lock(); defer a.mu.Unlock(); return a.d }

// adtsReader splits an ADTS stream into raw AAC frames (header stripped).
type adtsReader struct{ r io.Reader }

func (a *adtsReader) next() ([]byte, error) {
	var h [7]byte
	if _, err := io.ReadFull(a.r, h[:]); err != nil {
		return nil, err
	}
	if h[0] != 0xFF || h[1]&0xF0 != 0xF0 {
		return nil, fmt.Errorf("lost ADTS sync")
	}
	n := int(h[3]&3)<<11 | int(h[4])<<3 | int(h[5]>>5)
	hdr := 7
	if h[1]&1 == 0 { // CRC present
		hdr = 9
		var crc [2]byte
		if _, err := io.ReadFull(a.r, crc[:]); err != nil {
			return nil, err
		}
	}
	if n < hdr {
		return nil, fmt.Errorf("bad ADTS length %d", n)
	}
	buf := make([]byte, n-hdr)
	_, err := io.ReadFull(a.r, buf)
	return buf, err
}

// --- Ogg Opus muxing for ffmpeg's stdin ---------------------------------------------------

type oggOpus struct {
	chans, rate int
	serial      uint32
	seq         uint32
	granule     uint64
}

func newOggOpus(chans, rate int) *oggOpus {
	return &oggOpus{chans: chans, rate: rate, serial: 0x44495950}
}

func (o *oggOpus) headers() []byte {
	head := []byte("OpusHead")
	head = append(head, 1, byte(o.chans))
	head = binary.LittleEndian.AppendUint16(head, 0) // pre-skip
	head = binary.LittleEndian.AppendUint32(head, uint32(o.rate))
	head = append(head, 0, 0, 0) // gain, mapping family 0
	tags := []byte("OpusTags")
	tags = binary.LittleEndian.AppendUint32(tags, 0)
	tags = binary.LittleEndian.AppendUint32(tags, 0)
	return append(o.page(head, 0, 0x02), o.page(tags, 0, 0)...)
}

// packet wraps one Opus packet in its own Ogg page; granule position counts 48 kHz samples.
func (o *oggOpus) packet(p []byte) []byte {
	o.granule += uint64(opusSamples(p))
	return o.page(p, o.granule, 0)
}

func (o *oggOpus) page(data []byte, granule uint64, flags byte) []byte {
	var segs []byte
	for n := len(data); ; n -= 255 {
		if n >= 255 {
			segs = append(segs, 255)
			continue
		}
		segs = append(segs, byte(n))
		break
	}
	b := []byte("OggS")
	b = append(b, 0, flags)
	b = binary.LittleEndian.AppendUint64(b, granule)
	b = binary.LittleEndian.AppendUint32(b, o.serial)
	b = binary.LittleEndian.AppendUint32(b, o.seq)
	o.seq++
	b = append(b, 0, 0, 0, 0, byte(len(segs)))
	b = append(b, segs...)
	b = append(b, data...)
	binary.LittleEndian.PutUint32(b[22:], oggCRC(b))
	return b
}

var oggTable = func() (t [256]uint32) {
	for i := range t {
		r := uint32(i) << 24
		for j := 0; j < 8; j++ {
			if r&0x80000000 != 0 {
				r = r<<1 ^ 0x04c11db7
			} else {
				r <<= 1
			}
		}
		t[i] = r
	}
	return
}()

func oggCRC(b []byte) (crc uint32) {
	for _, c := range b {
		crc = crc<<8 ^ oggTable[byte(crc>>24)^c]
	}
	return
}

// opusSamples is a packet's duration in 48 kHz samples, from its TOC byte (RFC 6716 3.1).
func opusSamples(p []byte) int {
	if len(p) == 0 {
		return 0
	}
	cfg := p[0] >> 3
	var frame int // samples per frame at 48 kHz
	switch {
	case cfg < 12: // SILK: 10, 20, 40, 60 ms
		frame = [...]int{480, 960, 1920, 2880}[cfg&3]
	case cfg < 16: // hybrid: 10, 20 ms
		frame = [...]int{480, 960}[cfg&1]
	default: // CELT: 2.5, 5, 10, 20 ms
		frame = [...]int{120, 240, 480, 960}[cfg&3]
	}
	switch p[0] & 3 {
	case 0:
		return frame
	case 1, 2:
		return 2 * frame
	}
	if len(p) < 2 {
		return frame
	}
	return int(p[1]&0x3F) * frame
}

// inputClock maps an input sample position (samples fed so far, at rate: G.711 one byte per
// 8 kHz sample, AAC 1024 per frame, Opus by packet duration) to the PTS of the frame holding
// it, so output frames keep the camera's RTP clock across gaps.
type inputClock struct {
	rate int // samples per second of the input
	mu   sync.Mutex
	pos  int64 // samples fed so far
	runs []clockRun
}

type clockRun struct {
	start int64
	pts   time.Duration
}

func (c *inputClock) add(pts time.Duration, n int) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.runs = append(c.runs, clockRun{c.pos, pts})
	c.pos += int64(n)
	if len(c.runs) > 512 { // ~10 s of 20 ms packets; the encoder lags far less
		c.runs = append(c.runs[:0], c.runs[len(c.runs)-256:]...)
	}
}

// at is the PTS of input sample s, interpolated within its frame.
func (c *inputClock) at(s int64) (time.Duration, bool) {
	c.mu.Lock()
	defer c.mu.Unlock()
	for i := len(c.runs) - 1; i >= 0; i-- {
		if r := c.runs[i]; r.start <= s {
			return r.pts + time.Duration(s-r.start)*time.Second/time.Duration(c.rate), true
		}
	}
	return 0, false
}
