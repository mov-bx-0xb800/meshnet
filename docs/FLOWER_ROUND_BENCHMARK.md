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
- `254` DATA frames at `192` bytes per stream payload.
- `32` bridge windows at `8` DATA frames per window.
- About `382` LoRa packets per model transfer before retries:
  DATA + ACK + POLL + two repeated POLL_DONE control frames.
- With `2` logical clients and evaluate enabled: `6` full model transfers,
  `292,272` model bytes per round, `1,524` DATA frames, about `2,292`
  LoRa packets before retries.

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

On the central node, make sure the central bridge forwards to the same host and
port used below. Then run:

```bash
python3 scripts/flower_round_benchmark.py server \
  --host 127.0.0.1 \
  --port 8081 \
  --rounds 1 \
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
