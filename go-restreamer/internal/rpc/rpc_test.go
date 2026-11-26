// SPDX-License-Identifier: AGPL-3.0-or-later

package rpc

import (
	"bufio"
	"encoding/json"
	"net"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"
)

type fakeHandler struct {
	mu      sync.Mutex
	configs []ConfigureParams
	removed []string
}

func (h *fakeHandler) Configure(p ConfigureParams) (any, error) {
	h.mu.Lock()
	defer h.mu.Unlock()
	if p.DeviceID == "REFUSED" {
		return nil, &Error{Code: CodeServer, Message: "no", Data: "ERR_UNSUPPORTED_CODEC_HEVC"}
	}
	h.configs = append(h.configs, p)
	return map[string]any{"ok": true, "n": len(h.configs)}, nil
}
func (h *fakeHandler) Remove(id string) (any, error) {
	h.mu.Lock()
	defer h.mu.Unlock()
	h.removed = append(h.removed, id)
	return nil, nil
}
func (h *fakeHandler) Stats() any { return map[string]any{"cameras": map[string]any{}} }

func sockPath(t *testing.T) string {
	// Unix socket paths are short on macOS: keep under /tmp's short dirs, not t.TempDir()'s.
	d, err := os.MkdirTemp("", "rpc")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { os.RemoveAll(d) })
	return filepath.Join(d, "s")
}

type client struct {
	c net.Conn
	r *bufio.Reader
}

func dial(t *testing.T, path string) *client {
	c, err := net.Dial("unix", path)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { c.Close() })
	return &client{c, bufio.NewReader(c)}
}

func (c *client) call(t *testing.T, line string) map[string]any {
	t.Helper()
	if _, err := c.c.Write([]byte(line + "\n")); err != nil {
		t.Fatal(err)
	}
	c.c.SetReadDeadline(time.Now().Add(5 * time.Second))
	l, err := c.r.ReadString('\n')
	if err != nil {
		t.Fatalf("reading reply to %s: %v", line, err)
	}
	var m map[string]any
	if err := json.Unmarshal([]byte(l), &m); err != nil {
		t.Fatalf("reply %q: %v", l, err)
	}
	return m
}

const camJSON = `{"device_id":"AABBCCDDEEFF","mode":"copy","shared_decode":{"enabled":true,"detect_fps":10,"socket":"/tmp/x.sock"},` +
	`"channels":[{"channel":"video1","stream_name":"video1","rtsp_url":"rtsp://u:p@h/1","dest_host":"192.0.2.1","dest_port":7550,` +
	`"audio_enabled":true,"width":0,"height":0,"bitrate_kbps":0}]}`

func TestRoundTrip(t *testing.T) {
	path := sockPath(t)
	// A stale socket file from a crashed predecessor must not block the start.
	if err := os.WriteFile(path, nil, 0o600); err != nil {
		t.Fatal(err)
	}
	h := &fakeHandler{}
	s, err := Listen(path, h, nil)
	if err != nil {
		t.Fatalf("stale socket file blocked Listen: %v", err)
	}
	a, b := dial(t, path), dial(t, path) // several clients at once

	// What Python sends: the same configure_camera twice, ids as ints.
	r := a.call(t, `{"jsonrpc":"2.0","id":1,"method":"configure_camera","params":`+camJSON+`}`)
	if r["id"].(float64) != 1 || r["error"] != nil || r["result"].(map[string]any)["ok"] != true {
		t.Fatalf("configure reply = %v", r)
	}
	r = b.call(t, `{"jsonrpc":"2.0","id":"x","method":"configure_camera","params":`+camJSON+`}`)
	if r["id"] != "x" || r["error"] != nil {
		t.Fatalf("second client reply = %v", r)
	}
	if len(h.configs) != 2 || h.configs[0].Channels[0].RTSPURL != "rtsp://u:p@h/1" || h.configs[0].SharedDecode.Socket != "/tmp/x.sock" ||
		h.configs[0].SharedDecode.DetectFPS != 10 || !h.configs[0].Channels[0].AudioEnabled || h.configs[0].Channels[0].DestPort != 7550 {
		t.Fatalf("decoded = %+v", h.configs)
	}
	if r = a.call(t, `{"jsonrpc":"2.0","id":2,"method":"get_stats"}`); r["result"] == nil {
		t.Fatalf("stats = %v", r)
	}
	if r = a.call(t, `{"jsonrpc":"2.0","id":3,"method":"remove_camera","params":{"device_id":"AABBCCDDEEFF"}}`); r["error"] != nil || h.removed[0] != "AABBCCDDEEFF" {
		t.Fatalf("remove = %v %v", r, h.removed)
	}

	// Errors.
	for _, tc := range []struct {
		line string
		code float64
	}{
		{`not json`, CodeParse},
		{`{"jsonrpc":"2.0","id":4,"method":"nope"}`, CodeMethodNotFound},
		{`{"jsonrpc":"2.0","id":5,"method":"configure_camera","params":{"device_id":"A","mode":"fast"}}`, CodeInvalidParams},
		{`{"jsonrpc":"2.0","id":6,"method":"configure_camera","params":{"device_id":"A","channels":[{"channel":"video1"}]}}`, CodeInvalidParams},
		{`{"jsonrpc":"2.0","id":7,"method":"remove_camera","params":{}}`, CodeInvalidParams},
		{`{"jsonrpc":"1.0","id":8,"method":"get_stats"}`, CodeInvalidRequest},
		{`{"jsonrpc":"2.0","id":9,"method":"configure_camera","params":{"device_id":"REFUSED","channels":[]}}`, CodeServer},
	} {
		r := a.call(t, tc.line)
		e, _ := r["error"].(map[string]any)
		if e == nil || e["code"].(float64) != tc.code {
			t.Errorf("%s -> %v, want code %v", tc.line, r, tc.code)
		}
	}
	// A notification gets no reply: the next call's reply is its own.
	a.c.Write([]byte(`{"jsonrpc":"2.0","method":"get_stats"}` + "\n"))
	if r = a.call(t, `{"jsonrpc":"2.0","id":10,"method":"get_stats"}`); r["id"].(float64) != 10 {
		t.Fatalf("got %v after a notification", r)
	}

	// Close hangs up on clients and removes the socket.
	s.Close()
	if _, err := os.Stat(path); err == nil {
		t.Error("socket file left behind")
	}
	a.c.SetReadDeadline(time.Now().Add(2 * time.Second))
	if _, err := a.r.ReadString('\n'); err == nil || strings.Contains(err.Error(), "timeout") {
		t.Errorf("client not hung up: %v", err)
	}
}

func TestValidateRejects(t *testing.T) {
	for name, p := range map[string]ConfigureParams{
		"no device":   {Channels: nil},
		"dup channel": {DeviceID: "A", Channels: []Channel{{Channel: "v", RTSPURL: "r", DestHost: "h", DestPort: 1}, {Channel: "v", RTSPURL: "r", DestHost: "h", DestPort: 1}}},
		"bad port":    {DeviceID: "A", Channels: []Channel{{Channel: "v", RTSPURL: "r", DestHost: "h", DestPort: 70000}}},
		"missing url": {DeviceID: "A", Channels: []Channel{{Channel: "v", DestHost: "h", DestPort: 1}}},
	} {
		if p.Validate() == nil {
			t.Errorf("%s accepted", name)
		}
	}
}
