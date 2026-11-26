// SPDX-License-Identifier: AGPL-3.0-or-later

package detect

import (
	"sync"
	"time"
)

// Clock is the onClockSyncs the video1 push actually sent Protect, in the source's PTS, and
// the last video frame it wrote (Python ProtectClock). Live 2026-11-07 on Driveway (shared
// decode, delay 0): boxes ran a little ahead of the vehicle when detection read the wall
// clock as a tag left the pull, because the push reads it when the tag leaves its queue and
// any backlog put Protect's time later than ours. A frame's wall time is therefore Protect's
// own: its sync's wallClock + (pts - sync pts).
type Clock struct {
	mu      sync.Mutex
	syncs   []sync1       // oldest first, at most 16
	written time.Duration // PTS of the last video frame sent
	have    bool
	changed chan struct{} // closed and replaced whenever written moves
}

type sync1 struct {
	pts  time.Duration
	wall float64 // ms
}

func NewClock() *Clock { return &Clock{changed: make(chan struct{})} }

// Reset starts a new stream (a new push connection): its timestamps are unrelated to the old.
func (c *Clock) Reset() {
	c.mu.Lock()
	c.syncs, c.have = nil, false
	c.mu.Unlock()
}

// Synced records an onClockSync the push sent.
func (c *Clock) Synced(pts time.Duration, wall time.Time) {
	c.mu.Lock()
	c.syncs = append(c.syncs, sync1{pts, float64(wall.UnixMilli())})
	if len(c.syncs) > 16 {
		c.syncs = c.syncs[1:]
	}
	c.mu.Unlock()
}

// Wrote records the frame the push just sent.
func (c *Clock) Wrote(pts time.Duration) {
	c.mu.Lock()
	c.written, c.have = pts, true
	close(c.changed)
	c.changed = make(chan struct{})
	c.mu.Unlock()
}

// Wall returns Protect's wall ms for the frame at pts once the push has sent it, by the sync
// in force when it went (the latest at or before it); ok is false after timeout, or when done
// closes first.
func (c *Clock) Wall(pts time.Duration, timeout time.Duration, done <-chan struct{}) (float64, bool) {
	deadline := time.NewTimer(timeout)
	defer deadline.Stop()
	for {
		c.mu.Lock()
		if c.have && c.written >= pts {
			defer c.mu.Unlock()
			for i := len(c.syncs) - 1; i >= 0; i-- {
				if s := c.syncs[i]; s.pts <= pts {
					return s.wall + float64((pts - s.pts).Milliseconds()), true
				}
			}
			return 0, false
		}
		ch := c.changed
		c.mu.Unlock()
		select {
		case <-ch:
		case <-deadline.C:
			return 0, false
		case <-done:
			return 0, false
		}
	}
}
