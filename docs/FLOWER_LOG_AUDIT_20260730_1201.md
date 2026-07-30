# Flower Log Audit - 2026-07-30 12:01Z

Inputs inspected:

- `flower-logs-central-20260730T120137Z-meshpi-4196.tar.gz`
- `flower-logs-client-20260730T120153Z-node1-3689.tar.gz`

Both nodes ran git commit `65ce1f3`.

## Result

The broadcast destination fix solved the original handshake failure:

- both nodes opened session `3982261024`;
- central received the client's `OPEN` and 79 application bytes;
- client received `OPEN_OK`, an ACK, POLL frames, and 4 application bytes;
- both directions transmitted and received authenticated bridge DATA.

The 512-byte smoke round still failed before its first model movement.

## Core Failure 1: The Poll Turn Was Violated

Central sent `POLL` sequence 2, which grants the client the half-duplex LoRa
channel. While that poll was active, central's local TCP reader queued 939
bytes and immediately started a downlink window.

At the same time, the client returned two `POLL_DONE` frames. Central therefore
transmitted DATA while the client held the scheduled turn.

The code caused this directly: `_on_local_data()` called `send_window()` on
both roles, bypassing `_central_scheduler()` and `_serve_client_poll()`.
Central metrics confirmed two concurrent send-window calls:

```text
stream_send_window_active=2
```

Fix:

- local TCP readers only queue bytes;
- central downlinks are sent only by the central scheduler;
- client uplinks are sent only while serving a central POLL;
- the scheduler ignores a new central connection until the full OPEN_OK burst
  has completed.

## Core Failure 2: Near-Maximum DATA Frames Did Not Arrive

Central's first downlink window contained:

```text
seq 1-4: 192-byte stream payload, 232 bytes encoded
seq 5:   171-byte stream payload, 211 bytes encoded
```

The client received `seq=5`, the 211-byte encoded frame, and returned SACK
`0x00000010`. It received none of the 232-byte encoded frames, including the
retries. Central attempted 67 DATA frames, retransmitted 61, and finally reset
the session. The client's final 104-byte RESET did arrive.

This proves that local `tx accepted` logging is not proof of over-air delivery.
It also makes 232-byte bridge frames unsafe to use for the full benchmark on
this live radio/firmware pair.

Fix:

- default stream payload reduced from 192 to 160 bytes;
- maximum normal encoded DATA frame reduced from 232 to 200 bytes;
- the runner enforces 160 bytes through `MESHNET_BRIDGE_PAYLOAD_BYTES`, so an
  older local YAML file does not need manual editing;
- benchmark frame/window estimates use the same enforced payload size.

Cost for one current 48,712-byte model movement:

```text
old 192-byte chunks: 254 DATA, 32 windows, about 382 packets
new 160-byte chunks: 305 DATA, 39 windows, about 461 packets
```

That is about 20.7 percent more estimated packets in exchange for avoiding the
live near-maximum-frame failure.

## Intended Full Profile

The current 18-feature model artifact contains 48,712 Flower tensor bytes. The
47,164-byte artifact under `assets/` is an older 15-feature model and must not
be used as the current production size.

The complete planned benchmark is:

```text
3 rounds
2 logical clients
evaluation enabled
6 full model movements per round
18 full model movements total
876,816 model bytes total
8,298 estimated LoRa packets before retries and open/close overhead
```

At 400 ms pacing, packet pacing alone is at least 55.3 minutes. Real elapsed
time will be longer.

## Remaining Hardware Risk

The radio firmware still differs:

- central RAK11310: `2.7.15.567b8ea`
- client RAK11310: `2.5.20.4c97351`

Both radios should be put on the same firmware line before treating a full-run
failure as a bridge-only software failure.
