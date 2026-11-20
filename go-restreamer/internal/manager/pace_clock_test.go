// SPDX-License-Identifier: AGPL-3.0-or-later
package manager

import (
	"testing"
	"time"

	"diyport/go-restreamer/internal/audio"
	"diyport/go-restreamer/internal/detect"
	"diyport/go-restreamer/internal/flv"
	"diyport/go-restreamer/internal/rpc"
)

// Live 2026-11-13 10:37 on Driveway: with Pace 1000 ms boxes led the vehicle by ~1.1 s and lined
// up with --pace-ms 0. video1's syncs reach the detection clock a Pace later.
func TestVideo1SyncShiftedByPace(t *testing.T) {
	var cfg *flv.Config
	m := New(Options{Hwaccel: "none", Pace: time.Second, Log: testLog(&syncBuf{}),
		Tune: func(c *flv.Config) { cfg = c }})
	cam := &camera{id: "D", groups: map[string]*group{}, encs: map[string]*audio.Encoder{},
		chans: map[string]*chanState{}, clock: detect.NewClock()}
	m.newPusher(cam, rpc.Channel{Channel: "video1"})
	if cfg == nil || cfg.OnSync == nil {
		t.Fatal("video1 pusher has no OnSync")
	}
	wall := time.UnixMilli(1_000_000)
	cfg.OnSync(0, wall)
	cam.clock.Wrote(500 * time.Millisecond)
	got, ok := cam.clock.Wall(500*time.Millisecond, time.Second, nil)
	if want := float64(1_000_000 + 1000 + 500); !ok || got != want {
		t.Fatalf("Wall = %v, %v; want %v", got, ok, want)
	}
}
