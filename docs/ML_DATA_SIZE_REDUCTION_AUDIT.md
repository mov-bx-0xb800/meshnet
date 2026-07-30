# ML Data Size Reduction Audit

Last refreshed: 2026-07-30

Scope: `../TasikChiniResearch` ML codebase and its Flower traffic over the Meshnet LoRa bridge.

This is a decision document. It identifies where bytes come from, what can realistically be reduced, what is risky, and what should be tested before field use.

## Executive Verdict

The main traffic is not raw sensor data. The main traffic is repeated full-model exchange through Flower.

The current LoRa design sends model tensors many times:

- current 18-feature model: 48,712 Flower tensor bytes per full model movement;
- two clients, three rounds, current fit plus evaluate flow: 18 full model movements;
- payload-only minimum: 876,816 bytes before gRPC, bridge frames, ACK/POLL/POLL_DONE, retries, and local training time.

Best near-term path:

1. Fix the stale 15-feature vs 18-feature artifact mismatch.
2. Measure exact live Flower message sizes.
3. Disable per-round federated evaluation in LoRa mode unless it is truly needed.
4. Add `float16` Flower parameter exchange.
5. Test smaller GRU widths, starting with 16 units.
6. Only then attempt int8, sparse deltas, or custom FL protocol work.

Best strategic path:

- Use LoRa for telemetry, anomaly summaries, thresholds, embeddings, and occasional compact updates.
- Avoid frequent full Flower weight exchange over LoRa unless model size is reduced by at least 4x to 10x.

## Evidence From The Current Code

Current model path:

- `../TasikChiniResearch/model.py`
- `../TasikChiniResearch/centralNode.py`
- `../TasikChiniResearch/clientNode.py`
- `../TasikChiniResearch/preprocessing.py`

Current architecture:

```text
input: 72 time steps x 18 model features
encoder: GRU(32)
decoder: GRU(32)
output: TimeDistributed(Dense(18))
optimizer: Adam, lr 0.001, clipnorm 1.0
client local training: epochs=2, batch_size=16
Flower strategy: FedAvg
clients: default 2
rounds: default 3
```

Flower code path:

- `clientNode.get_parameters()` returns `model.get_weights()`.
- `clientNode.fit()` receives full global weights, trains locally, returns full local weights.
- `clientNode.evaluate()` receives full aggregated weights again and evaluates.
- `centralNode.py` uses Flower `FedAvg` with `fraction_fit=1.0` and `fraction_evaluate=1.0`.

Flower serialization source:

- Flower `ndarrays_to_parameters` converts each ndarray with `ndarray_to_bytes`.
- Flower `ndarray_to_bytes` uses `np.save(..., allow_pickle=False)`.
- Source: https://flower.ai/docs/framework/1.23/fr/_modules/flwr/common/parameter.html

That means the default payload is not compressed. It is `.npy` bytes per tensor.

## Artifact Mismatch Found

There are two weight files with different shapes:

```text
../TasikChiniResearch/assets/global_autoencoder_weights.npz
  old 15-feature model
  11,535 params
  46,140 raw float32 bytes
  47,164 Flower tensor bytes

../TasikChiniResearch/global_autoencoder_weights.npz
  current 18-feature model shape
  11,922 params
  47,688 raw float32 bytes
  48,712 Flower tensor bytes
```

This matters because `client_edge_anomalies_monitor.py` loads `./assets/global_autoencoder_weights.npz`, but current `model.py` builds an 18-feature model. That asset is likely stale and shape-incompatible with the current code.

Docs that still describe the old 15-feature model:

- `../TasikChiniResearch/LORA_TRANSPORT.md`
- `../TasikChiniResearch/agent.md`
- parts of `../TasikChiniResearch/progress.md`

## Current 18-Feature Tensor Layout

From the current 18-feature NPZ:

```text
arr_0 encoder kernel       (18, 96)  1,728 params  6,912 bytes
arr_1 encoder recurrent    (32, 96)  3,072 params 12,288 bytes
arr_2 encoder bias         (2, 96)     192 params    768 bytes
arr_3 decoder kernel       (32, 96)  3,072 params 12,288 bytes
arr_4 decoder recurrent    (32, 96)  3,072 params 12,288 bytes
arr_5 decoder bias         (2, 96)     192 params    768 bytes
arr_6 dense kernel         (32, 18)    576 params  2,304 bytes
arr_7 dense bias           (18,)        18 params     72 bytes
total                                  11,922 params 47,688 bytes
Flower `.npy` total                                  48,712 bytes
```

The hidden recurrent weights dominate. Reducing input feature count alone barely moves the needle.

## Current Traffic Model

One current 18-feature model movement:

```text
payload size       48,712 bytes
bridge DATA frames ceil(48,712 / 192) = 254
windows            ceil(254 / 8) = 32
approx packets     DATA 254 + ACK 32 + POLL 32 + POLL_DONE 64 = 382
```

Two clients, three rounds, current fit plus evaluate flow:

```text
full movements 18
model payload   876,816 bytes
DATA frames     4,572
approx packets  6,876
time at 700 B/s 20.9 minutes payload-only
```

This excludes gRPC framing, bridge open/close/control, retries, failed windows, reconnects, and local training time.

## Why Generic Compression Is Weak

Measured on the current 18-feature NPZ payload without NumPy installed, by parsing the `.npy` members and compressing the raw bytes:

```text
Flower `.npy` tensor bytes      48,712
zlib/gzip/lzma compressed       about 42,900 bytes
reduction                       about 12 percent
```

Reason: trained or initialized float32 tensors are close to high-entropy binary data. ZIP/gzip-style compression helps a little with headers, zeros, and repeated structure, but it will not turn 49 KB into 5 KB.

Conclusion: stream compression in Meshnet is not enough by itself.

## Reduction Options

### Option A - Disable Federated Evaluate In LoRa Mode

Current Flower round with two clients has six model movements:

```text
server -> client 1 for fit
server -> client 2 for fit
client 1 -> server updated model
client 2 -> server updated model
server -> client 1 for evaluate
server -> client 2 for evaluate
```

If we skip per-round federated evaluate, each round becomes four full model movements instead of six.

Impact:

```text
18 movements -> 12 movements for three rounds
876,816 bytes -> 584,544 bytes
about 33 percent reduction
```

Risk:

- Lose per-round validation metrics from clients.
- Current `clientNode.evaluate()` also saves thresholds and global weights; that logic must move elsewhere.

Recommendation: do this for LoRa mode unless per-round client evaluation is required.

### Option B - Send Float16 Parameters

Convert Flower tensors to float16 for transport, then cast back to float32 for Keras training/aggregation.

Impact for current 18-feature model:

```text
float32 raw data 47,688 bytes
float16 raw data 23,844 bytes
estimated Flower-style transfer about 24-25 KB
reduction about 49 percent
```

Risk:

- Small numerical loss.
- Need custom Flower client/strategy or a serialization layer that dequantizes before aggregation.
- Must test anomaly MSE, thresholds, and cluster stability.

Recommendation: highest value first implementation after fixing stale artifacts.

### Option C - Send Int8 Quantized Parameters Or Updates

Use per-tensor scale metadata and int8 payloads.

Impact for current 18-feature model:

```text
int8 data roughly 11,922 bytes
with scales/metadata roughly 12-13 KB
reduction about 4x
```

Risk:

- More accuracy loss than float16.
- Server aggregation must dequantize before FedAvg or implement quantized averaging carefully.
- Repeated quantization across rounds can drift unless tested.
- Better if combined with error feedback/residuals.

Recommendation: second-stage experiment after float16 baseline.

Relevant research:

- QSGD studies quantized gradient/update communication with convergence guarantees: https://arxiv.org/abs/1610.02132
- FedPAQ combines periodic averaging and quantized message passing for federated learning: https://arxiv.org/abs/1909.13014

### Option D - Smaller GRU Width

Current hidden width is 32. This drives most of the bytes.

Estimated current 18-feature Flower-style sizes:

| GRU units | Params | Float32 transfer | Float16 data | Int8 data |
| ---: | ---: | ---: | ---: | ---: |
| 32 | 11,922 | 48.7 KB | 23.8 KB | 11.9 KB |
| 24 | 7,218 | 29.9 KB | 14.4 KB | 7.2 KB |
| 16 | 3,666 | 15.7 KB | 7.3 KB | 3.7 KB |
| 12 | 2,322 | 10.3 KB | 4.6 KB | 2.3 KB |
| 8 | 1,266 | 6.1 KB | 2.5 KB | 1.3 KB |

Risk:

- Reduced model capacity.
- May hurt anomaly detection for complex temporal patterns.
- Must validate on station holdout and event/anomaly cases.

Recommendation: test `GRU(16)` first. It is the best balanced target: about 3.1x smaller before quantization and likely still expressive enough for 18 environmental features.

### Option E - Reduce Feature Count

This is less powerful than it sounds because recurrent hidden weights dominate.

Example with 32 GRU units:

```text
18 features -> about 48.7 KB
15 features -> about 47.2 KB
10 features -> about 44.6 KB
```

Risk:

- Dropping features can weaken anomaly attribution and health classification.
- Current 18 features include useful cyclical encodings.

Recommendation: do not lead with feature removal. Use feature ablation for model quality, not as the main byte-saving tactic.

### Option F - Reduce Lookback Window

Reducing `LOOKBACK_WINDOW` does not reduce model parameter size for a GRU. The same GRU weights are reused at every time step.

Impact:

- Less local compute.
- Less RAM.
- Faster local training.
- No meaningful reduction in Flower model bytes.

Recommendation: do not use lookback reduction as a LoRa byte-size fix.

### Option G - Send Deltas Instead Of Full Weights

After a client receives global weights, it can train locally and send only:

```text
delta = local_weights - global_weights
```

Raw float32 delta is still the same size. The win comes when deltas are sparse or quantized.

Possible design:

- server and clients keep previous global model;
- client sends sparse top-k quantized delta plus residual error feedback;
- server averages reconstructed deltas;
- server sends quantized global delta back, not full model.

Risk:

- This is no longer stock Flower `NumPyClient`/`FedAvg`.
- Needs versioning, model hash checks, missed-round recovery, and residual handling.
- More engineering complexity.

Recommendation: high-value but later. Do not start here unless we accept a custom FL protocol.

### Option H - Skip Full FL For Routine LoRa Operation

If the operational goal is anomaly detection and lake state reporting, we can avoid full model exchange most of the time.

Send instead:

- current anomaly flags,
- per-feature reconstruction errors,
- dynamic thresholds,
- 32D embeddings,
- health-state label,
- small model/version hashes,
- occasional compressed model updates.

Approximate payload:

```text
32D embedding float16 = 64 bytes before metadata
18 feature errors float16 = 36 bytes before metadata
health label + timestamp + status = one small packet
```

This is the LoRa-native design.

Risk:

- Less pure federated learning.
- Global model improves less often.
- Requires accepting local models as primary at each station.

Recommendation: best production architecture if radio airtime and compliance matter more than frequent global retraining.

## Recommended Implementation Order

1. Fix stale 15/18-feature docs and artifact paths.
2. Add a small size audit script that reports:
   - tensor shapes,
   - raw bytes,
   - Flower `.npy` bytes,
   - estimated LoRa frames/windows/packets,
   - compressed/float16/int8 estimates.
3. Add exact Flower message-size logging. Flower has `arrays_size_mod` and `message_size_mod` in newer APIs, but current scripts use legacy `start_numpy_client`; use either a migration or a local wrapper.
4. Add a LoRa mode that disables per-round federated evaluate.
5. Implement float16 parameter exchange and validate against baseline.
6. Train and compare GRU widths 32, 24, 16, 12.
7. Combine `GRU(16)` with float16 if accuracy holds.
8. Only then evaluate int8 and sparse delta protocols.
9. Decide whether production uses full FL over LoRa or sends compact anomaly/embedding summaries.

## Test Gates

Before accepting any reduction:

- Same train/validation split.
- Same random seeds.
- Compare final validation MSE.
- Compare per-feature thresholds.
- Compare anomaly detection on injected pH, turbidity, and dissolved oxygen events.
- Compare embeddings and health cluster labels.
- Measure exact wire bytes, not only NPZ file size.
- Run through Meshnet benchmark with the new payload size.
- Record retries, RSSI, SNR, and elapsed time.

## Current Recommendation

The practical target should be:

```text
GRU(16) + float16 transport + skip per-round evaluate in LoRa mode
```

Expected order-of-magnitude:

```text
current:        about 48.7 KB per full model movement
GRU(16):        about 15.7 KB float32
GRU(16)+f16:    about 8 KB
skip evaluate:  one-third fewer full model movements
```

That gets the system from "barely demo-feasible" toward "field-testable", while keeping the ML concept recognizable.

For production, prefer compact anomaly/embedding telemetry and occasional model refreshes over frequent full Flower training rounds on LoRa.
