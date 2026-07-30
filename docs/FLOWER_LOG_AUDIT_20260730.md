# Flower Log Audit - 2026-07-30

Inputs inspected:

- `flower-logs-central-20260730T113835Z-meshpi-3938.tar.gz`
- `flower-logs-client-20260730T113915Z-node1-3459.tar.gz`

## Finding

The failed run did not reach Flower DATA transfer.

Central heard the client:

- central final `frames_received=2`
- central final `control_frames_received=2`
- central logged `rx OPEN peer=client-001 mesh=!53532122`

Client did not hear central:

- client final `frames_received=0`
- client final `control_frames_received=0`
- client never logged `rx OPEN_OK`

So the live failure was central-to-client LoRa delivery during the bridge
handshake. It was not yet a 47 KB model-size problem or a TCP/Flower payload
segmentation problem.

## Configuration Evidence

The peer IDs were correct in the run:

- central local radio: `!536b1922`
- client local radio: `!53532122`
- central config peer: `!53532122`
- client config peer: `!536b1922`

Both radios were on the intended RF settings:

- `MY_919`
- `SHORT_FAST`
- slot `8`
- hop limit `1`
- override frequency `0.0`
- frequency offset `0.0`

The firmware versions differed:

- central: `2.7.15.567b8ea`
- client: `2.5.20.4c97351`

That mismatch remains a real field-risk until both devices are updated to the
same firmware line.

## Runner Problem

The run accidentally used the heavy benchmark defaults:

- `evaluate=1`
- `model_bytes=48712`
- `transfers_per_round=3`
- `bytes_per_round=146136`

That commit temporarily changed the runner to a small smoke test:

- `EVALUATE=0`
- `MODEL_BYTES=512`

After the later `12:01Z` run isolated the stream scheduling and frame-size
failures, the runner moved to the complete intended profile. See
`FLOWER_LOG_AUDIT_20260730_1201.md`.

## Code Fixes

The bridge now avoids the direct-unicast failure mode seen in these logs:

- one configured peer uses LoRa broadcast addressing by default
- received frames are still accepted only if the sender matches the pinned peer
- multi-peer setups stay direct to avoid cross-talk
- `MESHNET_BRIDGE_RADIO_DESTINATION_MODE=direct` can force direct mode

The bridge also now:

- waits a full configured frame interval after RX before replying
- paces all LoRa frames with the configured frame interval
- repeats `OPEN_OK` five times
- records radio broadcast and radio ACK counters in metrics

The runner now redacts:

- channel PSK values
- primary channel URLs
- private keys
- public keys
- fixed PINs
- MQTT passwords
- Telegram secrets

Do not share the old uploaded archives further. They contain sensitive radio
configuration material.

## Next Run Interpretation

If the next run opens a tunnel and shows `data_tx`/`data_rx` moving, the root
blocker was direct-unicast/control-plane delivery.

If the next run still shows central receiving client frames but client receiving
zero central frames, the remaining blocker is RF/firmware/hardware, not Flower
payload code. Update both radios to the same firmware and then swap antennas or
radios to see whether the one-way failure follows the hardware.
