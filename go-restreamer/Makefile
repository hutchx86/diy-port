# SPDX-License-Identifier: AGPL-3.0-or-later
# `make` builds the native macOS binary; `make linux-arm64` the Pi / RK3588 one. No CGo.

BIN := bin/diyport-restreamer

.PHONY: all linux-arm64 test vet clean
all:
	CGO_ENABLED=0 GOOS=darwin GOARCH=arm64 go build -trimpath -o $(BIN) ./cmd/restreamer

linux-arm64:
	CGO_ENABLED=0 GOOS=linux GOARCH=arm64 go build -trimpath -o $(BIN)-linux-arm64 ./cmd/restreamer

test:
	go vet ./...
	go test -race ./...

clean:
	rm -rf bin
