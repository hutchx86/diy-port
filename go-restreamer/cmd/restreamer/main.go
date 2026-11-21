// SPDX-License-Identifier: AGPL-3.0-or-later

// Command restreamer is the diy-port Go sidecar: it pulls each ONVIF camera's RTSP once and
// pushes the streams to Protect, driven over JSON-RPC on a unix socket by run_all.py.
package main

import (
	"context"
	"flag"
	"fmt"
	"log/slog"
	"os"
	"os/signal"
	"syscall"
	"time"

	"diyport/go-restreamer/internal/audio"
	"diyport/go-restreamer/internal/manager"
	"diyport/go-restreamer/internal/rpc"
)

func main() {
	sock := flag.String("sock", "", "unix socket path for JSON-RPC (required; one per instance)")
	hw := flag.String("hwaccel", "auto", "hardware encoder: auto, videotoolbox or none")
	g711 := flag.String("g711", "aac", "G.711 camera audio: aac (re-encode, default) or copy")
	opus := flag.String("opus", "on", "also push an Opus track (FLV tag type 10) for Protect's browser live view: on or off")
	share := flag.String("audio-share", "on", "share one audio ffmpeg between channels on a camera URL: on or off (off: one per channel)")
	opusRate := flag.Int("opus-rate", 0, "force every channel's Opus sample rate (8000, 12000, 16000, 24000, 48000); 0: as Protect asks per channel")
	pace := flag.Int("pace-ms", 1000, "hold tags this long and send them on their own clock (0: as they arrive)")
	level := flag.String("log-level", "info", "debug, info, warn or error")
	flag.Parse()
	if *sock == "" {
		fmt.Fprintln(os.Stderr, "restreamer: --sock is required")
		os.Exit(2)
	}
	switch *hw {
	case "auto", "videotoolbox", "none":
	default:
		fmt.Fprintf(os.Stderr, "restreamer: --hwaccel %q: want auto, videotoolbox or none\n", *hw)
		os.Exit(2)
	}
	switch *opus {
	case "on", "off":
	default:
		fmt.Fprintf(os.Stderr, "restreamer: --opus %q: want on or off\n", *opus)
		os.Exit(2)
	}
	switch *share {
	case "on", "off":
	default:
		fmt.Fprintf(os.Stderr, "restreamer: --audio-share %q: want on or off\n", *share)
		os.Exit(2)
	}
	if *opusRate != 0 {
		if _, ok := audio.OpusRateFor(*opusRate); !ok {
			fmt.Fprintf(os.Stderr, "restreamer: --opus-rate %d: want 0, 8000, 12000, 16000, 24000 or 48000\n", *opusRate)
			os.Exit(2)
		}
	}
	var lv slog.Level
	if err := lv.UnmarshalText([]byte(*level)); err != nil {
		fmt.Fprintf(os.Stderr, "restreamer: --log-level %q: %v\n", *level, err)
		os.Exit(2)
	}
	log := slog.New(slog.NewTextHandler(os.Stderr, &slog.HandlerOptions{Level: lv}))
	slog.SetDefault(log)

	mgr := manager.New(manager.Options{Hwaccel: *hw, G711AAC: *g711 != "copy", Opus: *opus == "on", OpusRate: *opusRate, NoAudioShare: *share == "off", Pace: time.Duration(*pace) * time.Millisecond, Log: log})
	srv, err := rpc.Listen(*sock, mgr, log)
	if err != nil {
		log.Error("cannot listen", "sock", *sock, "err", err)
		os.Exit(1)
	}
	log.Info("restreamer ready", "sock", *sock)

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGTERM, syscall.SIGINT)
	defer stop()
	<-ctx.Done()
	log.Info("shutting down")
	srv.Close()
	mgr.Close()
}
