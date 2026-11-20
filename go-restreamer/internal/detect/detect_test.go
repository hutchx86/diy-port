// SPDX-License-Identifier: AGPL-3.0-or-later

package detect

import (
	"testing"
	"time"
)

func TestPackUnpack(t *testing.T) {
	// The Python struct ">BdI": 1 + 8 + 4 bytes, big endian.
	b := Pack(KindFrame, 1700000000123.5, []byte("abc"))
	if len(b) != 13+3 || b[0] != 1 || b[9] != 0 || b[12] != 3 {
		t.Fatalf("% x", b)
	}
	k, w, n := Unpack(b)
	if k != KindFrame || w != 1700000000123.5 || n != 3 {
		t.Fatal(k, w, n)
	}
}

// The clock maps a frame to the sync in force when it was sent, once it has been sent.
func TestClockWall(t *testing.T) {
	c := NewClock()
	c.Reset()
	c.Synced(0, time.UnixMilli(1000))
	c.Synced(10*time.Second, time.UnixMilli(11050)) // the wall clock drifted 50 ms
	if _, ok := c.Wall(time.Second, 30*time.Millisecond, nil); ok {
		t.Fatal("answered before the frame was sent")
	}
	go func() { time.Sleep(20 * time.Millisecond); c.Wrote(12 * time.Second) }()
	w, ok := c.Wall(12*time.Second, time.Second, nil)
	if !ok || w != 13050 {
		t.Fatalf("wall = %v %v", w, ok)
	}
	if w, ok = c.Wall(4*time.Second, time.Second, nil); !ok || w != 5000 {
		t.Fatalf("earlier frame wall = %v %v", w, ok)
	}
	c.Reset()
	if _, ok = c.Wall(time.Second, 20*time.Millisecond, nil); ok {
		t.Fatal("a reset clock still answers")
	}
}
