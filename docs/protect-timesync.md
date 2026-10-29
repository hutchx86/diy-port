# Protect time sync (`ubnt_avclient_timeSync`): change required in pi-port

Status: proposed (2026-10-20), not implemented, and not needed while the pi-port host
is NTP-synced: two NTP clocks on a LAN are normally within a few to tens of ms, less
than one detection frame. Do this if the host can't run NTP or drifts from the console.
For what does improve accuracy, see [frame-timestamps.md](frame-timestamps.md) (change 3; change 4 was tested and doesn't help).

pi-port stamps every event with its host's clock. Real UniFi cameras and the AI Port
sync their clock with Protect over the avclient WebSocket before saying hello, and again
every 10 minutes. Protect places smart-detect boxes in playback by `clockWall`, so if the
pi-port host's clock drifts from the console's, every box moves by the difference.
`AIPORT_BOX_DELAY_MS` corrects a fixed decode or relay delay, not clock drift.

## What real devices do

Sources: WebSocket captures of a real camera (`02000000000C`, three connections) and a
second camera in `~/Desktop/protect/ds.json` and
`~/Desktop/protect/protect_camALL3_port7442_wss_public.ndjson`; the AI Port firmware
v5.1.12 in the aiport-fw lab (`ubnt_avclient`: `CONNECTED_TO_CONTROLLER_WS ->
TIME_SYNCING -> TIME_SYNCED -> HELLO_MSG_SENT`, `avclient_timesync.cpp`).

### Messages

Device to Protect (a request):

```json
{"from": "ubnt_avclient", "to": "UniFiVideo", "functionName": "ubnt_avclient_timeSync",
 "messageId": 81411085, "inResponseTo": 47760, "responseExpected": true,
 "timeStamp": "2026-07-01T23:03:37.023+00:00", "payload": {"timeDelta": 0}}
```

Protect to device (the reply):

```json
{"from": "UniFiVideo", "to": "ubnt_avclient", "functionName": "ubnt_avclient_timeSync",
 "messageId": 47762, "inResponseTo": 81411085, "responseExpected": false,
 "payload": {"t1": 1782947017066, "t2": 1782947017066}}
```

- `t1`, `t2`: Protect's clock in epoch ms when it received the request and when it
  replied. They were equal in all 81 captured replies.
- The first request of a connection has `inResponseTo: 0`. Each later request in a
  batch sets `inResponseTo` to the `messageId` of Protect's previous reply, so the
  rounds form a chain.
- `timeDelta`: the correction in ms the device applied after its previous batch,
  reported in the next batch: `0` until the first correction, then for example `10`,
  or `-1`, `-81`, `12` on later batches. The device's measured offset moves by that
  amount at the batch boundary (from about +50 ms to about -40 ms when it reported -81).

### Timing

- Straight after the WebSocket connects, before `ubnt_avclient_hello`, the device sends
  a batch of 10 requests, one at a time, each after the previous reply arrives. A batch
  took 0.6 to 1.3 s on the LAN.
- The hello follows immediately, already stamped with the corrected time.
- The batch repeats every 600 s for as long as the connection is up (seen at +601 s,
  +1202 s and +1803 s).
- A device with no clock (stamped `2000-01-01T00:00:25`) kept that time for all 10
  rounds, then stamped its hello `2025-12-24T02:36:43`: it corrects once the batch
  is complete. Another connection started 20 s behind.
- Protect doesn't require the sync. Camera emulators that skip it (unifi-cam-proxy,
  pi-port) are adopted and their events accepted. They're just not aligned to Protect's
  clock.

### The firmware's own clock use

On the AI Port, `ubnt_avclient` sets the system clock from this exchange (and stops
`systemd-timesyncd` after adoption). Frames are stamped with `CLOCK_MONOTONIC` at decode
and converted to wall time on that synced clock. pi-port shouldn't set the host's clock.
It should keep the measured offset and apply it to the wall times it sends.

## What pi-port does now (`piport/avclient.py`)

- `run()` (around line 2560) connects and sends `ubnt_avclient_hello` straight away;
  there's no time sync.
- The receive loop (around line 2600) logs and drops every message with a non-zero
  `inResponseTo` before `handle_function()` sees it. Protect's timeSync replies have
  one, so they'd be dropped there.
- `_now_ms()` (around line 1943) returns `time.time()` and `time.monotonic()` in ms. It
  is the single source of wall time for events:
  - the ffmpeg reader stamps each frame with it (`_latest_frame_time`, around line 489);
  - `_analysed_frame_time` copies that stamp, falling back to `_now_ms()` (around
    line 597);
  - `_frame_wall_ms()` gives that minus `AIPORT_BOX_DELAY_MS`, and becomes `clockWall`,
    the tracker's `now_ms`, `first_shown` and the snapshot `wall`, and through those
    `clockBestWall`.
- `send_msg()` (around line 287) stamps `timeStamp` with
  `datetime.now(timezone.utc)`, separately from `_now_ms()`.
- `clockMonotonic` and `clockStream` (`_stream_clocks()`) are monotonic ms since the
  RTSP pull started. They're unaffected and shouldn't change.

## Change required

### 1. Clock offset state

Add a module-level offset, in ms, that is added to every wall-clock time pi-port sends:

```python
_protect_offset_ms = 0       # Protect's clock minus ours, from the last timeSync batch
_protect_last_delta_ms = 0   # change applied after the last batch, sent as timeDelta
```

- In `_now_ms()`, return `int(time.time() * 1000) + _protect_offset_ms` as the wall
  part. The monotonic part stays as it is. This covers frame stamps, `clockWall`,
  `clockBestWall` and tracker times in one place.
- In `send_msg()`, build `timeStamp` from the same corrected time, e.g.
  `datetime.fromtimestamp(_now_ms()[0] / 1000, timezone.utc)`, keeping the RFC 3339
  format with milliseconds and `+00:00`.

### 2. One round, and the offset maths

For each request, record the local wall time just before sending (`T0`) and when the
reply arrives (`T3`). Use the raw `time.time()`, not the corrected time. Then:

```text
offset = ((t1 - T0) + (t2 - T3)) / 2      # Protect minus local, ms
rtt    = (T3 - T0) - (t2 - t1)
```

After 10 rounds, take the offset from the round with the smallest `rtt` (or the median
of the 10). Then:

```python
new = round(best_offset)
_protect_last_delta_ms = new - _protect_offset_ms
_protect_offset_ms = new
```

Send `_protect_last_delta_ms` as `timeDelta` in the next batch's requests (0 on the
first batch of the process). Log the offset and RTT of each batch at INFO.

### 3. The batch at connect, before the hello

In `run()`, between `connect(...)` and the hello:

- Send a request with `from_="ubnt_avclient"`, `to="UniFiVideo"`,
  `response_expected=True`, `in_response_to=0` for the first and the previous reply's
  `messageId` after that, and `payload={"timeDelta": _protect_last_delta_ms}`.
  `send_msg()`'s defaults are the other way round
  (`from_="UniFiVideo"`, `to="ubnt_avclient"`). Match the captured direction here.
- Read the reply with `ws.recv(timeout=...)` (about 2 s per round). Skip any other
  message that arrives meanwhile, or queue it for `handle_function()`.
- If a round times out or the reply has no `t1`/`t2`, stop the batch, keep the current
  offset and go on to the hello. Never hold up the hello for more than a few seconds.
- Then compute the offset (section 2) and send the hello as now.

### 4. Resync every 600 s

The receive loop blocks on `for raw in ws`, so the periodic batch needs its own trigger:

- A daemon timer thread (`threading.Timer`, rescheduled each time) starts a batch every
  600 s while the connection is up. It sends the first request of the batch. The
  websockets sync client allows `send` from another thread.
- In the receive loop, before the `inResponseTo` early `continue`, handle
  `functionName == "ubnt_avclient_timeSync"` replies: record `T3`, store the round, and
  send the next request of the batch (chained `inResponseTo`) until 10 are done, then
  update the offset. Guard the batch state with a lock.
- Cancel the timer when the connection closes. A new connection runs its own batch
  before its hello.

### 5. Things to watch

- **Tracker times are wall times.** `_motion_state` (`started_ms`, confirm and miss
  windows) and the snapshot buffers use `_frame_wall_ms()`. When the offset changes, a
  live track's next frame jumps by that delta. Corrections after the first batch were a
  few to ~80 ms, which is harmless. A first batch can move the clock by seconds, for
  example after a reconnect with a host clock that has drifted. Either apply only small
  changes while tracks are open, or move the tracker's durations to monotonic time.
- **Frame stamps taken before a change keep the old offset.** That's fine: the
  correction is at most one frame's worth after the first batch.
- **Pre-adopt connection:** a real device syncs before every hello. Whether Protect
  answers before adoption isn't in the captures: all of them are of adopted cameras. So
  try the batch there too, and rely on the timeout path if it isn't answered.
- **ucp4 (`ucp4_client.py`):** stamps `int(time.time() * 1000)` on its own messages.
  It isn't part of event placement. Leave it unless Protect turns out to compare it.

### 6. Tests (`piport/tests/test_avclient.py`)

- Offset maths: fixed `T0`, `T3`, `t1`, `t2`, including a reply with a large offset and
  one with a high RTT; the smallest-RTT round wins.
- `timeDelta`: 0 on the first batch, then the previous batch's change.
- Chain: the first request has `inResponseTo: 0`, later ones the previous reply's
  `messageId`; `from`/`to` are `ubnt_avclient` / `UniFiVideo`; `responseExpected` true.
- At connect, the hello is sent after the batch, and is still sent when Protect doesn't
  reply (timeout).
- `_now_ms()`, `clockWall`/`clockBestWall` in an `EventSmartDetect`, and the
  `timeStamp` of `send_msg()` all include the offset. `clockMonotonic`/`clockStream`
  don't.
- The receive loop routes a timeSync reply to the sync handler and not to the generic
  "response to our msg" skip.

### 7. Docs

Add `ubnt_avclient_timeSync` to [aiport-interface.md](aiport-interface.md) (connection
sequence and message list), and note there that `clockWall`/`clockBestWall` are on
Protect's clock.

## Checking it live

- Log each batch's offset and smallest RTT at INFO. On a LAN the RTT should be a few ms
  to a few tens of ms.
- Skew the pi-port host's clock on purpose (for example 2 s ahead in a test VM). After
  the first batch, events' `clockWall` should match Protect's time to within the RTT,
  and boxes should line up in playback with `AIPORT_BOX_DELAY_MS` unchanged.
