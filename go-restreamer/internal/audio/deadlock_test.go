// SPDX-License-Identifier: AGPL-3.0-or-later

package audio

import (
	"testing"
	"time"

	"diyport/go-restreamer/internal/media"
)

// SetTracks and Close while ffmpeg is emitting must not hang: they used to close the
// transcoder under n.mu while its reader waited in emit for n.mu (live 2026-11-12 12:02).
func TestNormalizerTeardownWhileEmitting(t *testing.T) {
	if !ffmpegAvailable("") {
		t.Skip("no ffmpeg")
	}
	tr := media.Tracks{Audio: media.PCMU, AudioRate: 8000, AudioChannels: 1}
	for round := 0; round < 5; round++ {
		n := New(Config{Enabled: true, TranscodeG711: true}, func(media.AudioFrame) {
			time.Sleep(time.Millisecond) // a slow consumer widens the race window
		})
		n.SetTracks(tr)
		n.Start(0)
		stop := make(chan struct{})
		go func() {
			pkt := make([]byte, 160)
			for i := 0; ; i++ {
				select {
				case <-stop:
					return
				default:
				}
				n.OnAudio(media.AudioFrame{PTS: time.Duration(i) * 20 * time.Millisecond, Data: pkt})
				time.Sleep(2 * time.Millisecond)
			}
		}()
		time.Sleep(300 * time.Millisecond)
		done := make(chan struct{})
		go func() {
			n.SetTracks(tr) // tracks change: replaces the transcoder
			time.Sleep(200 * time.Millisecond)
			n.Close()
			close(done)
		}()
		select {
		case <-done:
		case <-time.After(10 * time.Second):
			t.Fatalf("round %d: teardown hung", round)
		}
		close(stop)
	}
}
