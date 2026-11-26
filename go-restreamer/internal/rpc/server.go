// SPDX-License-Identifier: AGPL-3.0-or-later

package rpc

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"net"
	"os"
	"sync"
)

// Handler is what the server drives. Errors from Configure may be *Error to choose the code.
type Handler interface {
	Configure(ConfigureParams) (any, error)
	Remove(deviceID string) (any, error)
	Stats() any
}

const maxLine = 4 << 20

// Server accepts any number of clients on one unix socket. Each connection's requests are
// handled in order; connections run concurrently.
type Server struct {
	path string
	h    Handler
	log  *slog.Logger
	ln   net.Listener

	mu    sync.Mutex
	conns map[net.Conn]struct{}
	wg    sync.WaitGroup
}

// Listen removes a stale socket file left by a crashed predecessor, then listens.
func Listen(path string, h Handler, log *slog.Logger) (*Server, error) {
	if log == nil {
		log = slog.Default()
	}
	_ = os.Remove(path)
	ln, err := net.Listen("unix", path)
	if err != nil {
		return nil, err
	}
	s := &Server{path: path, h: h, log: log, ln: ln, conns: map[net.Conn]struct{}{}}
	s.wg.Add(1)
	go s.accept()
	return s, nil
}

func (s *Server) accept() {
	defer s.wg.Done()
	for {
		c, err := s.ln.Accept()
		if err != nil {
			return
		}
		s.mu.Lock()
		s.conns[c] = struct{}{}
		s.mu.Unlock()
		s.wg.Add(1)
		go s.serve(c)
	}
}

// Close stops accepting, hangs up on clients, waits for in-flight calls and removes the socket.
func (s *Server) Close() {
	s.ln.Close()
	s.mu.Lock()
	for c := range s.conns {
		c.Close()
	}
	s.mu.Unlock()
	s.wg.Wait()
	_ = os.Remove(s.path)
}

// Serve runs until ctx ends, then closes.
func (s *Server) Serve(ctx context.Context) {
	<-ctx.Done()
	s.Close()
}

func (s *Server) serve(c net.Conn) {
	defer s.wg.Done()
	defer func() {
		c.Close()
		s.mu.Lock()
		delete(s.conns, c)
		s.mu.Unlock()
	}()
	enc := json.NewEncoder(c) // Encode ends each value with the newline the framing wants
	r := bufio.NewReaderSize(c, 64<<10)
	for {
		line, err := readLine(r)
		if err != nil {
			if !errors.Is(err, io.EOF) && !errors.Is(err, net.ErrClosed) {
				s.log.Debug("rpc: read", "err", err)
			}
			return
		}
		if len(line) == 0 {
			continue
		}
		if resp := s.handle(line); resp != nil {
			if err := enc.Encode(resp); err != nil {
				return
			}
		}
	}
}

// readLine reads one line of any length up to maxLine.
func readLine(r *bufio.Reader) ([]byte, error) {
	var out []byte
	for {
		part, err := r.ReadSlice('\n')
		out = append(out, part...)
		if len(out) > maxLine {
			return nil, errors.New("line too long")
		}
		if err == bufio.ErrBufferFull {
			continue
		}
		if err != nil {
			if err == io.EOF && len(out) > 0 {
				return trim(out), nil
			}
			return nil, err
		}
		return trim(out), nil
	}
}

func trim(b []byte) []byte {
	for len(b) > 0 && (b[len(b)-1] == '\n' || b[len(b)-1] == '\r' || b[len(b)-1] == ' ') {
		b = b[:len(b)-1]
	}
	return b
}

func (s *Server) handle(line []byte) *Response {
	var req Request
	if err := json.Unmarshal(line, &req); err != nil {
		return errResp(json.RawMessage("null"), CodeParse, "parse error", nil)
	}
	notification := len(req.ID) == 0
	id := req.ID
	if notification {
		id = json.RawMessage("null")
	}
	if req.JSONRPC != "2.0" || req.Method == "" {
		return reply(notification, errResp(id, CodeInvalidRequest, "not a JSON-RPC 2.0 request", nil))
	}
	var result any
	var err error
	switch req.Method {
	case MethodConfigure:
		var p ConfigureParams
		if perr := decodeParams(req.Params, &p); perr != nil {
			return reply(notification, errResp(id, CodeInvalidParams, perr.Error(), nil))
		}
		if perr := p.Validate(); perr != nil {
			return reply(notification, errResp(id, CodeInvalidParams, perr.Error(), nil))
		}
		result, err = s.h.Configure(p)
	case MethodRemove:
		var p RemoveParams
		if perr := decodeParams(req.Params, &p); perr != nil || p.DeviceID == "" {
			return reply(notification, errResp(id, CodeInvalidParams, "device_id is required", nil))
		}
		result, err = s.h.Remove(p.DeviceID)
	case MethodStats:
		result = s.h.Stats()
	default:
		return reply(notification, errResp(id, CodeMethodNotFound, "method not found: "+req.Method, nil))
	}
	if err != nil {
		var re *Error
		if errors.As(err, &re) {
			return reply(notification, &Response{JSONRPC: "2.0", ID: id, Error: re})
		}
		return reply(notification, errResp(id, CodeServer, err.Error(), nil))
	}
	if result == nil {
		result = map[string]any{"ok": true}
	}
	return reply(notification, &Response{JSONRPC: "2.0", ID: id, Result: result})
}

func reply(notification bool, r *Response) *Response {
	if notification {
		return nil
	}
	return r
}

func errResp(id json.RawMessage, code int, msg string, data any) *Response {
	return &Response{JSONRPC: "2.0", ID: id, Error: &Error{Code: code, Message: msg, Data: data}}
}

func decodeParams(raw json.RawMessage, v any) error {
	if len(raw) == 0 {
		return errors.New("params are required")
	}
	return json.Unmarshal(raw, v)
}
