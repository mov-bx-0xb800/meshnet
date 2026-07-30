# Flower Log Audit - 2026-07-30 12:22Z

Inputs:

- `flower-logs-central-20260730T122228Z-meshpi-4443.tar.gz`
- `flower-logs-client-20260730T122252Z-node1-3932.tar.gz`

Both nodes ran commit `9f663d3`. The archives began before `90475d6`, which
removed automatic Git/GitHub log publication.

## Result

The bridge was not stalled and did not crash. The operator interrupted both
runners during the first 48,712-byte model downlink:

- central finished with `exit_status=130`;
- client finished with `exit_status=130`;
- neither side recorded a session reset, invalid frame, duplicate,
  out-of-order frame, retransmission, radio NAK, or radio ACK timeout.

Session `1908403967` opened in both directions. The client hello reached
central, the server configuration reached the client, and the first model
movement was progressing in complete acknowledged windows:

```text
central stream_window_bytes_sent = 21,760
client  data_bytes_received       = 21,760
client  local_bytes_sent          = 21,760
central acknowledgements_received = 17
client  acknowledgements_sent     = 17
```

The remaining central queue was `27,388` bytes. The run was stopped at about
45 percent of the first application message, before the benchmark could emit
its first transfer result.

## What Looked Wrong But Was Not

Central repeatedly printed:

```text
diagnosis=radio_send_blocked_or_failed
```

Every such sample was taken while one 160-byte DATA frame was inside the
synchronous Meshtastic send call. `data_bytes_attempted` is incremented before
that call and `data_bytes_sent` immediately after it, so the temporary
160-byte difference meant "send in progress," not "send failed."

The client initially printed `data_tx_no_peer_rx` even though it had already
received the protocol ACK for its 79-byte hello. That diagnosis was also
false.

## Actual Runner Problems

The deployed local YAML files were stale:

```text
payload_bytes       = 192
poll_interval_ms    = 1000
central retries     = 15
client retries      = 12
central control     = 30 seconds
client control      = 20 seconds
```

The runner overrode payload size to 160 bytes but did not override the other
profile values. The 1,000 ms poll interval added avoidable delay to every
window.

The plan printed only the obsolete 700 B/s payload-only estimate. It did not
print the packet schedule. With this run's 8,298 estimated packets and 702
windows, the old active profile had a floor of:

```text
8,298 * 0.400 seconds + 702 * 1.000 seconds = 4,021.2 seconds
                                                  = about 67 minutes
```

That excludes USB send-call time, startup, retries, and RF loss. The
implementation also started its 400 ms delay after the roughly 100 ms USB
send call, making the live run slower again.

The runner stopped systemd services but did not restore the services that had
been active before the test. SIGTERM also ended the bridge without a guaranteed
final metrics publication.

The manual log publisher still defaulted to pushing and could push directly to
the base branch. Runtime logs were unignored, and a successful direct push
deleted the local source log directory. The optional archive HTTP server bound
to all interfaces without authentication.

## Fixes

- Enforce the complete current runner profile through environment overrides:
  160-byte payload, window 8, 5-second ACK timeout, 10-second control timeout,
  8 retries, 400 ms frame interval, and 500 ms poll interval.
- Treat `frame_interval_ms` as start-to-start pacing instead of adding USB
  acceptance time on top.
- Report `transfer_progress`, acknowledged queue progress, TX/RX rate, and ETA.
  A fault diagnosis now requires repeated no-progress samples.
- Print the packet-schedule floor. The current full profile is at least 61.2
  minutes before startup, retries, or RF loss.
- Never send TCP `HALF_CLOSE` while acknowledged radio DATA is still pending.
- Handle SIGTERM gracefully so final metrics are published.
- Record and restore only the systemd services that were active before the
  benchmark.
- Exchange the Git commit and detected Meshtastic firmware in the benchmark
  hello. A follow-up keeps mismatches visible but warns and continues by
  default because rejecting them made the central runner exit immediately
  after the client connected; strict acceptance remains opt-in.
- Ignore runtime logs by default. Publishing now requires
  `PUBLISH_FLOWER_LOGS=1`, direct-to-base push is off by default, and local logs
  are never deleted after a push.
- Serve the optional archive on the established central hotspot endpoint,
  `http://172.20.10.2:8765/`, while retaining explicit host/bind overrides.

## Remaining Field Action

The radios still run different firmware:

- central: `2.7.15.567b8ea`
- client: `2.5.20.4c97351`

They also use different configured conducted powers, 23 dBm and 24 dBm.
Neither mismatch caused packet loss in this capture. The runner warns and
continues for diagnostics; use `ALLOW_PEER_MISMATCH=0` to reject mismatches for
a strict acceptance run. Both radios should still be put on the same supported
firmware line before acceptance. Transmit power must be chosen from measured
antenna gain and cable loss; the repo cannot prove EIRP compliance from the
YAML number alone.
