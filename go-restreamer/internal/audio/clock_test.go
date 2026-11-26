// SPDX-License-Identifier: AGPL-3.0-or-later

package audio

import (
	"testing"
	"time"
)

// A lost packet (gap in input PTS) must move later output onto the camera's clock, which a
// pure sample count would not: that drift is what stuttered live 2026-11-12.
func TestInputClockFollowsGaps(t *testing.T) {
	c := &inputClock{rate: 8000}
	c.add(0, 160)                    // 20 ms
	c.add(20*time.Millisecond, 160)  // 20 ms
	c.add(100*time.Millisecond, 160) // 60 ms lost before this one
	for _, tc := range []struct {
		s    int64
		want time.Duration
	}{{0, 0}, {80, 10 * time.Millisecond}, {160, 20 * time.Millisecond},
		{320, 100 * time.Millisecond}, {400, 110 * time.Millisecond}} {
		if got, ok := c.at(tc.s); !ok || got != tc.want {
			t.Errorf("at(%d) = %v, %v; want %v", tc.s, got, ok, tc.want)
		}
	}
	if _, ok := (&inputClock{rate: 8000}).at(0); ok {
		t.Error("empty clock answered")
	}
}
