// SPDX-License-Identifier: AGPL-3.0-or-later

package manager

import (
	"context"
	"testing"
	"time"

	"diyport/go-restreamer/internal/rpc"
	"diyport/go-restreamer/internal/rtsp"
	"diyport/go-restreamer/internal/transcode"
)

// get_stats must not queue behind a remove whose RTSP session is slow to close (an offline
// camera mid-handshake): the manager lock is dropped while the session is released.
func TestStatsDoesNotWaitOnSlowRelease(t *testing.T) {
	slow := func(ctx context.Context, _ string, _ rtsp.Sink) error {
		<-ctx.Done()
		time.Sleep(2 * time.Second) // a dialer slow to notice
		return nil
	}
	m := New(Options{Caps: &transcode.Caps{Reason: "test"}, Dialer: slow, Log: testLog(&syncBuf{}), Tune: fastTune})
	defer m.Close()
	d := newSink(t)
	if _, err := m.Configure(rpc.ConfigureParams{DeviceID: "CAM1", Mode: "copy", Channels: []rpc.Channel{
		{Channel: "video1", StreamName: "video1", RTSPURL: "rtsp://192.0.2.1/x", DestHost: d.host(), DestPort: d.port()}}}); err != nil {
		t.Fatal(err)
	}
	removed := make(chan struct{})
	go func() { m.Remove("CAM1"); close(removed) }()
	time.Sleep(200 * time.Millisecond) // Remove is now waiting on the release
	start := time.Now()
	m.Stats()
	if d := time.Since(start); d > 500*time.Millisecond {
		t.Fatalf("get_stats waited %v on a remove", d)
	}
	select {
	case <-removed:
	case <-time.After(5 * time.Second):
		t.Fatal("remove never finished")
	}
}
