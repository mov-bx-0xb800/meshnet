# Flower Round Benchmark

This is the two-node test suite for measuring the planned Flower-over-Meshnet
round over the existing bridge path.

It sends deterministic binary payloads with the same byte size as the current
Flower model tensors. It does not run TensorFlow training unless you add
artificial sleeps, but it does replicate the network choreography:

1. Central sends global model for fit.
2. Client sends fitted model back.
3. Repeat for each logical client slot.
4. Central optionally sleeps for aggregation.
5. Central sends aggregated model for evaluate.
6. Client returns small evaluation metrics.

Default current model payload:

- `48,712` bytes per full model transfer.
- `305` DATA frames at `160` bytes per stream payload.
- `39` bridge windows at `8` DATA frames per window.
- About `461` LoRa packets per model transfer before retries:
  DATA + ACK + POLL + two repeated POLL_DONE control frames.
- With `2` logical clients and evaluate enabled: `6` full model transfers,
  `292,272` model bytes per round, `1,830` DATA frames, about `2,766`
  LoRa packets before retries.
- The default runner performs `3` rounds, so the complete simulation moves
  `876,816` model bytes in `18` full model movements.

## Local Plan

From `meshnet/`:

```bash
python3 scripts/flower_round_benchmark.py plan
```

Use the real current weights artifact when available:

```bash
python3 scripts/flower_round_benchmark.py plan \
  --weights-npz /Users/wojake/Desktop/TasikChiniResearch/global_autoencoder_weights.npz
```

## Two-Node Run

The normal runner now defaults to the complete intended profile:

```text
MODEL_BYTES=48712
ROUNDS=3
LOGICAL_CLIENTS=2
EVALUATE=1
BRIDGE_PAYLOAD_BYTES=160
BRIDGE_WINDOW_SIZE=8
BRIDGE_ACK_TIMEOUT_SECONDS=5
BRIDGE_CONTROL_TIMEOUT_SECONDS=10
BRIDGE_MAX_RETRIES=8
BRIDGE_FRAME_INTERVAL_MS=400
BRIDGE_POLL_INTERVAL_MS=500
```

Start central first and then client:

```bash
./scripts/run-flower-round-test.sh
```

The runner is offline-only: application TCP is loopback, transport is the
attached LoRa radio, Meshtastic's PyPI version check is disabled, and logs are
archived locally without any automatic Git/GitHub publish.

The runner enforces that profile even when a node's local `config.flower.yaml`
still contains older bridge timing or retry values. It also restores only the
systemd services that were active before the test.

The two nodes should use the same git commit and the same Meshtastic firmware
line. The full run has a packet-and-poll schedule floor of about `61.2 minutes`
and will take longer with startup, scheduler guards, retransmissions, or poor
RF. Status rows show acknowledged queue progress, current TX/RX rate, and an
ETA for the current queued application message.

The benchmark hello carries the local Git commit and detected radio firmware.
A mismatch is logged as a warning by default so it cannot terminate the
central runner immediately after the client connects. For a deliberately
strict acceptance run, set `ALLOW_PEER_MISMATCH=0`; a mismatch will then be
rejected before model DATA starts. The benchmark command itself follows the
same policy: use `--strict-peer-match` to opt into rejection.

For a short transport smoke test:

```bash
MODEL_BYTES=512 ROUNDS=1 LOGICAL_CLIENTS=1 EVALUATE=0 \
  ./scripts/run-flower-round-test.sh
```

On the central node, make sure the central bridge forwards to the same host and
port used below. Then run:

```bash
python3 scripts/flower_round_benchmark.py server \
  --host 127.0.0.1 \
  --port 8081 \
  --rounds 3 \
  --logical-clients 2 \
  --jsonl central-round-benchmark.jsonl
```

On the client node, connect to the local client bridge listener:

```bash
python3 scripts/flower_round_benchmark.py client \
  --host 127.0.0.1 \
  --port 8081 \
  --jsonl client-round-benchmark.jsonl
```

For a fit-only round without evaluation downlink:

```bash
python3 scripts/flower_round_benchmark.py server --no-evaluate
```

Runtime logs stay ignored by Git. Publishing is manual and explicit:

```bash
PUBLISH_FLOWER_LOGS=1 ./scripts/push-latest-flower-logs.sh central
```

That command uses a log branch by default. Direct-to-base publication requires
the additional explicit `AUTO_LOG_DIRECT_PUSH=1`.

To serve the newest archive from the central hotspot address:

```bash
SERVE=1 ./scripts/export-latest-flower-logs.sh central
```

The default download root is `http://172.20.10.2:8765/` and the HTTP server
binds to all local interfaces. Override those independently with `SERVE_HOST`
and `SERVE_BIND` if the network changes.

## Output

Each full model movement prints one `[transfer]` row per side:

- `role`: `central` or `client`.
- `r`: Flower round number.
- `c`: logical client slot.
- `phase`: `fit_down`, `fit_up`, or `eval_down`.
- `dir`: `central_to_client` or `client_to_central`.
- `bytes`: model bytes moved.
- `elapsed`: local measured transfer time.
- `goodput`: payload bytes per second for that movement.
- `peer_elapsed`: remote receive time when known.
- `frames`: estimated bridge DATA frames for that payload.
- `windows`: estimated reliable-stream windows.
- `approx_packets`: estimated LoRa packet count before retries.
- `sha`: first 16 hex chars of the payload hash.

The final `[summary]` line reports total model bytes, total elapsed time,
mean/median/p90 transfer goodput, and total estimated frame/window/packet counts.

## Important Limits

With only two physical radios, `--logical-clients 2` means one client process
plays two planned client slots serially. That measures the byte choreography for
the full two-client round, but it does not measure contention or fairness across
two separate client radios.

This benchmark does not prove RF compliance. It reports payload size, elapsed
time, goodput, hashes, and estimated bridge packet counts. Compliance still
needs EIRP calculation, occupied-band validation, override-frequency checks,
airtime ledgering, and actual RF measurements.
