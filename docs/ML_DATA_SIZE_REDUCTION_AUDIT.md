# Tasik ML / Flower Data-Size Reduction Audit

Last refreshed: 2026-07-30

Scope:

- TasikChiniResearch ML model, data preparation, anomaly thresholds, embeddings,
  clustering assets, and Flower exchange;
- Meshnet only where its current packet size and schedule determine transfer
  time;
- no radio-profile or RF-compliance changes.

This is a measurement and decision document. It does not treat a smaller file
as a success unless the resulting anomaly detection, embeddings, clustering,
and federated convergence remain useful.

## Executive Verdict

The `48 KB` figure is one full model movement, not the dataset and not a full
federated run:

```text
11,922 float32 parameters              47,688 bytes
8 Flower/NumPy .npy headers             1,024 bytes
one default Flower parameter movement  48,712 bytes
```

With two clients, three rounds, fit and federated evaluate, the current flow
makes 18 full model movements:

```text
48,712 * 18 = 876,816 model-tensor bytes
```

The safest first compression target is not GRU(16). A fresh local diagnostic
found that halving the hidden width to 16 increased validation reconstruction
MSE by about 16.7% relative to GRU(32), while keeping GRU(32) and transporting
groupwise int8 parameters did not regress validation MSE in a repeated
three-round diagnostic. The latter still needs real anomaly and cluster tests,
but it preserves the current architecture and 32-D embedding contract.

Recommended target:

```text
current GRU(32)
+ signed int8, group size 32
+ float32 dequantization and aggregation
+ evaluate/export only after the final round
```

Measured codec estimate:

```text
about 12,748 bytes per model movement
73.8% smaller than 48,712 bytes
```

Recommended schedule experiment:

```text
2 rounds * 3 local epochs
+ final-only distributed evaluation
= 10 model movements
= about 127,480 model bytes
= about 8.8 minutes current packet-schedule floor
```

That keeps the current total of six local epochs per client, but it performs
only two federated averages instead of three. It must be compared with the
current 3-round x 2-epoch result on genuinely different client datasets.

Stretch target:

```text
6-bit groupwise updates, about 9,768 bytes per movement
```

Six-bit exchange crossed the 10 KB target in the diagnostic, but its embedding
drift was materially larger than int8. It is a second-stage experiment, not the
first production choice.

Do not deploy any codec yet. The current asset and dataset baselines are not
valid enough to certify that compression preserved quality.

## Blocking Baseline Problems

### The current artifacts are not one compatible model bundle

The checkout currently combines:

- `assets/global_autoencoder_weights.npz`: old 15-feature weights;
- `assets/anomaly_thresholds.json`: old 15-feature thresholds, including raw
  `wind_direction_deg`;
- `assets/scaler_X.pkl`: current 18-feature scaler;
- current `model.py`: 18-feature GRU model;
- current clustering assets: fitted from the older embedding/model pipeline;
- root `global_autoencoder_weights.npz`: 18-feature shapes, but all 402 bias
  parameters are zero.

The last root artifact is an initialized model, not a trained global model.
`centralNode_weightExtraction.py` saves `initial_weights` after Flower exits; it
does not retrieve the aggregated result.

Consequences:

- the current edge monitor cannot load the 15-feature weights into the
  18-feature model;
- even after replacing the weights, the 15-feature threshold keys do not match
  the 18 model features;
- a new encoder requires regenerated PCA and clustering assets;
- a size/accuracy comparison against these mixed assets is invalid.

Required fix: publish one atomic, versioned asset bundle with:

```text
bundle version
ordered feature-schema hash
lookback and architecture
weight codec/version and tensor shapes
scaler hash
weights hash
threshold hash
encoder/embedding dimension
PCA and cluster-model hashes
training-data and split identifiers
```

Every process should refuse to mix incompatible bundle members.

### Current data preparation destroys one feature

The station database contains 5,148 non-null `evaporation_cm` values, ranging
from about 13.23 to 20.58. The sanitizer accepts only 0 to 5. Every observed
evaporation value is therefore converted to missing, then filled with zero.

After current preprocessing:

```text
evaporation_cm standard deviation = 0
unique values                     = 1
```

This is probably an aggregation, unit, or range-contract mismatch. The
preprocessing notebook sums evaporation and rainfall while resampling. It must
be checked against the original sensor meaning before changing the range.

Deleting evaporation would save only 129 parameters, or 516 float32 bytes at
GRU width 32. Fixing the signal is much more valuable than a roughly 1% payload
reduction.

### The current validation split leaks 71 hours

The current pipeline builds all overlapping 72-hour windows first and then
splits the window array 80/20.

```text
last training window: raw rows 4,879..4,950
first validation window: raw rows 4,880..4,951
shared rows: 71 of 72
```

The scaler artifact was also fitted outside the current split code. A claimed
compression-quality result from this split is optimistic.

Correct order:

1. split chronologically by raw timestamp;
2. leave a 72-hour boundary gap or independently window each side;
3. fit the scaler on training data only, or use a declared fixed global scaler;
4. build training and validation windows independently.

### Most water-quality values are interpolated

The database has 6,172 continuous hourly rows from 2011-02-02 13:00 through
2011-10-17 16:00. Its time grid is complete, but source coverage is not:

| Signal group | Observed rows | Missing |
| --- | ---: | ---: |
| wind/weather core | 4,031 | 34.7% |
| solar radiation | 1,822 | 70.5% |
| rainfall and water level | 2,670 | 56.7% |
| water temperature/conductivity/turbidity/DO mg/L | 689 | 88.8% |
| pH | 595 | 90.4% |
| DO saturation | 463 | 92.5% |

The pipeline interpolates without a maximum gap and then forward/back fills.
The autoencoder therefore treats long synthetic runs as ground truth.

Use:

- bounded, source-aware interpolation;
- an observed-value mask in the loss, without necessarily adding mask features
  to the transmitted model;
- per-feature imputation provenance;
- evaluation on observed readings separately from imputed readings.

### Live acquisition does not match the model feature contract

The current `sensor_reading.py` obtains live dissolved oxygen and TDS voltages.
pH, turbidity, and temperature are placeholders. TDS is not a model feature.
Most of the 18 model features are not supplied by this path.

Before deciding which features to preserve, define which signals every deployed
station can actually measure, which arrive from another source, and which are
optional. A larger feature list filled mostly by old history or zeros is not
more informative.

## Four Different Size Problems

These sizes must not be combined:

| Object | Current size | Crosses LoRa in Flower? | Main fix |
| --- | ---: | :---: | --- |
| one Flower model movement | 48,712 B | yes | parameter codec and protocol |
| station SQLite DB | 1,163,264 B | no | retention/storage policy |
| fallback station CSV | 1,769,704 B | no | avoid duplicate deployment copy |
| materialized `X` and copied `y` windows | 60.3 MiB RAM | no | streaming windows/no target copy |
| runtime `assets/` | about 17 MiB | no | replace/compact cluster predictor |
| `cluster_model.pkl` alone | 17,011,393 B | no | surrogate or compressed asset |
| tracked `temp_extract/` | about 872 MiB | no | exclude from Pi release |
| full checkout including `.git` | about 1.3 GiB | no | build a deployment artifact |

The repository also contains a 102,477,314-byte source-data ZIP and about
275 MiB of Git metadata. None belongs in a Raspberry Pi runtime release.

## Exact Current Model Size

Current model:

```text
input                    72 x 18
encoder                  GRU(32)
bottleneck               RepeatVector(72)
decoder                  GRU(32)
output                    TimeDistributed(Dense(18))
parameters               11,922
```

Tensor layout:

| Tensor | Shape | Parameters | float32 bytes |
| --- | ---: | ---: | ---: |
| encoder kernel | 18 x 96 | 1,728 | 6,912 |
| encoder recurrent | 32 x 96 | 3,072 | 12,288 |
| encoder bias | 2 x 96 | 192 | 768 |
| decoder kernel | 32 x 96 | 3,072 | 12,288 |
| decoder recurrent | 32 x 96 | 3,072 | 12,288 |
| decoder bias | 2 x 96 | 192 | 768 |
| dense kernel | 32 x 18 | 576 | 2,304 |
| dense bias | 18 | 18 | 72 |
| total | | 11,922 | 47,688 |

Flower 1.31 `NumPyClient` converts each returned ndarray using `np.save` with
pickling disabled. The eight 128-byte `.npy` headers add 1,024 bytes.

The recurrent weights dominate. For this architecture, feature count `F` and
hidden width `U` produce:

```text
parameters = 9U^2 + 4FU + 12U + F
```

At `U=32`, removing one feature saves only:

```text
4U + 1 = 129 parameters = 516 float32 bytes
```

Reducing lookback from 72 does not change the parameter count.

## Measured Codec Results

Measurements used the checked-in 18-feature initialized tensor shapes and the
older trained 15-feature artifact as a distribution cross-check. A fresh
18-feature GRU was also trained locally for six epochs using a leakage-free
chronological split for preliminary output/embedding comparisons.

These are tensor/codec bytes, before protobuf, gRPC, and bridge control:

| Codec | Estimated bytes | Reduction | DATA frames | One-movement schedule floor | 18-movement floor |
| --- | ---: | ---: | ---: | ---: | ---: |
| current Flower float32 | 48,712 | baseline | 305 | 203.9 s | 61.2 min |
| zstd-19 on default `.npy` stream | 42,925 | 11.9% | 269 | about 179 s | 53.7 min |
| Flower float16 arrays | 24,868 | 48.9% | 156 | 104.4 s | 31.3 min |
| int8, per-tensor scale | about 12,018 | 75.3% | 76 | about 51.4 s | 15.4 min |
| int8, group 32 | about 12,748 | 73.8% | 80 | 53.0 s | 15.9 min |
| 6-bit, group 32 | about 9,768 | 79.9% | 62 | 41.6 s | 12.5 min |
| 4-bit, group 32 | about 6,787 | 86.1% | 43 | 29.8 s | 8.9 min |

The schedule uses current Meshnet settings:

```text
160-byte DATA payload
8-frame window
400 ms packet interval
500 ms central poll interval
ACK + POLL + two POLL_DONE packets per completed window
```

It excludes open/close packets, gRPC overhead, retries, radio queue delays,
disconnects, and local ML work.

### Lossless compression is useful only as a second layer

Measured on the 18-feature payload:

```text
default Flower tensors       48,712 bytes
zlib/gzip/zstd               about 42.9 KB
lzma                         about 42.7 KB
np.savez_compressed          44,315 bytes
```

Float32 weights and deltas remain high-entropy. Raw deltas from three
two-epoch intervals compressed losslessly to 42.3-43.5 KB. Delta encoding alone
does not solve the radio problem.

After int8 quantization, zstd reduced three measured update byte streams from
11,922 bytes to approximately 10,379, 9,638, and 9,288 bytes. Entropy coding is
therefore worthwhile after quantization, not instead of it.

### Float16 is the lowest-risk first control

Exact current size:

```text
float16 tensor data     23,844 bytes
.npy headers             1,024 bytes
total                   24,868 bytes
```

The measured float16 weight reconstruction had:

```text
relative L2 error       about 0.021%
weight cosine           0.99999998
```

In the repeated three-round local diagnostic, float16 transport changed final
validation MSE by approximately -0.0001% and mean embedding cosine was
0.9999996 relative to float32.

This is a diagnostic, not proof of anomaly recall. Float16 transport is also
different from enabling Keras mixed-precision compute.

### Int8 groupwise transport is the current sweet spot

Group size 32 uses one float16 scale per group and keeps server aggregation in
float32.

Measured estimate:

```text
quantized values         11,922 bytes
373 float16 scales          746 bytes
version/shape metadata       80 bytes
total                    12,748 bytes
```

On the 18-feature initialized tensors:

```text
relative weight L2 error  0.444%
weight cosine             0.999990
```

On one freshly trained GRU(32), applying int8 groupwise weights after training
changed validation MSE by about +0.73% and mean embedding cosine was 0.999988.
When the same quantization was applied at every up/down exchange across three
local rounds, validation MSE did not regress, while embedding cosine relative
to float32 was 0.999692.

MSE moving slightly down is not evidence that quantization improved anomaly
detection. Cluster-label agreement and anomaly recall are the deciding gates.

### Six-bit is the sub-10 KB stretch candidate

Measured estimate:

```text
packed 6-bit values       8,942 bytes
373 float16 scales          746 bytes
version/shape metadata       80 bytes
total                     9,768 bytes
```

The initialized-tensor relative L2 error was about 1.83%. In the repeated
three-round diagnostic, validation MSE did not regress but mean embedding
cosine fell to about 0.99727. That can be enough to move points across a cluster
boundary.

Use 6-bit only after:

- int8 passes;
- per-layer sensitivity is measured;
- important tensors can remain int8/float16;
- cluster-label and anomaly decisions are compared, not only average MSE.

### Four-bit and ternary exchange are not first-line choices

Four-bit groupwise weights showed roughly 8-9% relative L2 distortion and
visible output/embedding drift. Average MSE sometimes improved because the
distortion acted like regularization, while individual feature errors changed
substantially.

Two-bit/sign/ternary schemes were much worse in direct tensor reconstruction.
They require quantization-aware or error-feedback training and are research
options, not safe drop-in codecs.

## Reduction Options

### 1. Evaluate and export only at the end

Current two-client round:

```text
2 fit downlinks
2 fit-result uplinks
2 evaluate downlinks
= 6 full model movements
```

Three rounds with final-only evaluate:

```text
12 fit movements + 2 final evaluate movements = 14
18 -> 14 movements
22.2% less model traffic
```

No distributed evaluation would use 12 movements, a 33.3% reduction, but the
current evaluate method also:

- computes thresholds;
- saves global weights;
- exports embeddings;
- logs the local round.

Those actions must move to an explicit finalization phase. Simply setting
`fraction_evaluate=0` would leave stale artifacts.

If the central node has an acceptable reference validation set, centralized
evaluation avoids model downlinks, but it cannot replace station-specific
validation.

### 2. Use fewer communication rounds with more local epochs

Current client work is:

```text
3 rounds x 2 local epochs = 6 local epochs
```

Candidate comparisons:

```text
2 rounds x 3 local epochs = 6 local epochs
1 round  x 6 local epochs = 6 local epochs
```

This reduces communication without reducing local compute. It can increase
client drift when stations have non-IID data, so compare convergence and
station-level quality. With only two clients, this is one of the highest-value
experiments.

### 3. Cache parameter versions and send references

After a client evaluates global model `G(r)`, the next round's fit also starts
from `G(r)`. The current protocol sends those bytes again.

A stateful codec can send:

```text
model bundle ID
global version
base SHA-256
"reuse cached base" marker
```

For three evaluate-every-round rounds, this can eliminate the two duplicated
fit downlinks at the start of rounds 2 and 3: four of 18 model movements.

Also pre-provision the exact initial model on every station. Round 1 can use a
hash reference if all nodes prove they have the same bytes.

Cache design needs:

- atomic persistence;
- missed-round recovery;
- full-model fallback;
- base-hash verification;
- bounded old-version retention.

### 4. Send quantized deltas, not absolute weights

Raw:

```text
delta = local weights - received global weights
```

is still 47,688 float32 bytes. The benefit comes from quantization and
sparsification.

Use:

- int8 or 6-bit delta quantization;
- stochastic rounding for an unbiased update where appropriate;
- server reconstruction and float32 weighted aggregation;
- client residual/error feedback across rounds.

Do not repeatedly aggregate integer arrays with stock FedAvg. Flower's default
in-place aggregation operates on the received ndarray dtype; custom
dequantization is required.

### 5. Sparse top-k deltas with error feedback

Measured top-k energy retention on the GRU(32) update:

| Kept values | Bitmap + int8 values | Round 1 energy retained | Round 3 energy retained |
| ---: | ---: | ---: | ---: |
| 5% | about 2,104 B | 34.7% | 55.6% |
| 10% | about 2,700 B | 50.5% | 70.7% |
| 20% | about 3,892 B | 69.5% | 84.8% |
| 25% | about 4,488 B | 75.8% | 88.8% |
| 50% | about 7,468 B | 93.3% | 97.7% |

Aggressive top-k is very small but discards too much of a three-round update
unless the residual is carried forward. With only three rounds, delayed error
may never be transmitted.

Sparse updates are a later phase after dense int8. Required state:

- residual per client and tensor;
- model/base version;
- deterministic sparse index encoding;
- reset behavior after reconnect or full resync;
- maximum residual age.

### 6. Remove redundant serialization overhead

Packing all tensors into one versioned buffer removes most of the eight `.npy`
headers, saving about 1 KB or 2.1%. It is worthwhile inside a custom codec but
not a solution by itself.

Use explicit little-endian encoding and declared shapes. Never infer shapes from
untrusted payload bytes.

### 7. Adapt precision by tensor sensitivity

Not every tensor needs the same precision:

- keep biases and small/output tensors in float16;
- use int8 on sensitive recurrent matrices;
- try 6-bit only on tensors whose perturbation does not change decisions;
- use per-tensor, per-row, or group scales according to measured error;
- use a codec table in the model manifest.

This can approach the 6-bit size with int8-like quality.

### 8. Width reduction

Exact Flower-style sizes for the current two-GRU shape:

| GRU units | Parameters | float32 | float16 |
| ---: | ---: | ---: | ---: |
| 32 | 11,922 | 48,712 B | 24,868 B |
| 24 | 7,218 | 29,896 B | 15,460 B |
| 20 | 5,298 | 22,216 B | 11,620 B |
| 16 | 3,666 | 15,688 B | 8,356 B |
| 12 | 2,322 | 10,312 B | 5,668 B |
| 8 | 1,266 | 6,088 B | 3,556 B |

One-seed leakage-free diagnostic:

| Width | Validation MSE | Change vs width 32 |
| ---: | ---: | ---: |
| 32 | 0.35194 | baseline |
| 24 | 0.34209 | -2.8% |
| 16 | 0.41060 | +16.7% |
| 12 | 0.41732 | +18.6% |

This does not prove width 24 is better; it shows width 16 is not currently a
safe default recommendation. Run at least five seeds after fixing the data.

Changing width also changes embedding dimension. The PCA and HDBSCAN pipeline
must be retrained, and health labels must be revalidated.

### 9. Keep encoder width, shrink the decoder

The 32-D encoder is consumed by clustering. An asymmetric model can preserve a
32-D embedding while using a 16-unit decoder:

```text
encoder GRU(32)
decoder GRU(16)
Dense(18)
about 7,698 parameters
about 31,816 Flower float32 bytes
about 16,420 Flower float16 bytes
```

It still changes embedding semantics after retraining, but it avoids changing
the embedding dimension and removes capacity from the reconstruction side.

### 10. Align the objective with the actual anomaly decision

The monitor uses only the final reconstructed point in the 72-hour window. The
model trains a full 72 x 18 reconstruction.

Alternative:

```text
past window -> GRU(32) encoder -> Dense(18) current/next-point prediction
```

This removes the decoder:

```text
about 5,586 parameters
about 22,984 Flower float32 bytes
about 11,812 Flower float16 bytes
```

It is no longer the same sequence autoencoder. It may be a better anomaly
objective, but thresholds and event tests must be rebuilt.

### 11. Lightweight recurrent or temporal architectures

Candidates:

- SimpleRNN autoencoder: about 4,306 parameters, but more vulnerable to long
  temporal dependencies;
- temporal convolution/depthwise-separable convolution;
- compact temporal convolutional network;
- linear/PCA autoencoder as a strong baseline;
- fixed reservoir/echo-state encoder with a small trainable output layer.

Compare accuracy and Pi processing time. Do not choose a smaller architecture
from parameter count alone.

### 12. Structured pruning and low-rank factorization

Unstructured zero pruning only saves wire bytes when accompanied by sparse
index/bitmap encoding. It often does not speed dense GRU execution.

Prefer:

- structured hidden-unit pruning;
- gate-aware pruning;
- low-rank factorization of the four large GRU matrices;
- fine-tuning after pruning;
- knowledge distillation from the current GRU(32).

TensorFlow Model Optimization's standard quantization-aware tooling does not
currently provide general drop-in RNN/GRU coverage. Transport quantization can
be implemented independently; quantization-aware GRU training needs a custom,
tested path.

### 13. Federate small adapters

After a trustworthy GRU(32) base is pretrained and identically installed,
freeze it and train low-rank adapters on the large matrices.

Approximate rank-2 adapter budget across the four GRU matrices, dense matrix,
and biases:

```text
about 1,498 trainable parameters
about 3 KB in float16 before compact metadata
```

This is the best strategic path to sub-5 KB updates while preserving a capable
base. It changes the training design and requires:

- adapter-enabled GRU layers;
- base model/version immutability during a federation;
- adapter aggregation tests on non-IID station data;
- periodic full-base refresh outside routine LoRa operation.

### 14. Federate only selected layers

Current encoder only:

```text
4,992 parameters
20,352 Flower float32 bytes
10,368 Flower float16 bytes
```

Dense layer only:

```text
594 parameters
2,632 Flower float32 bytes
```

Bias-only, decoder-only, encoder-only, and personalized-head schemes can be
small, but they change what is global. In this autoencoder, a shared encoder
with local decoders can make reconstruction errors incomparable between
stations. Use only with an explicit personalization design.

### 15. Model broadcast instead of duplicate downlinks

The central node sends the same global model separately through two Flower TCP
sessions. A radio-native model dissemination layer could transmit one global
payload and collect per-client acknowledgements.

Potential current flow:

```text
one fit broadcast + two client uploads + one evaluate broadcast
= four model-sized movements per round instead of six
```

This cannot be done transparently by copying gRPC bytes: messages and sessions
are client-specific. It needs a Tasik/Flower parameter dissemination protocol,
reliable per-client ACK state, authentication, retransmission, and fallback to
unicast.

### 16. Send anomaly intelligence routinely, full models occasionally

Routine LoRa payloads can contain:

```text
32-D embedding in float16        64 bytes
18 feature errors in float16     36 bytes
threshold/health/version metadata
anomaly flags and timestamp
```

Raw data stays local. Full federated updates occur on a schedule, when a model
drift trigger fires, or over a higher-capacity maintenance link.

This is the most field-appropriate architecture when airtime matters more than
continuous global retraining.

### 17. Other advanced options considered

These are valid research directions, but none is a safer first change than
dense int8:

- **Vector/codebook quantization:** cluster weights or updates and transmit
  short centroid indexes plus a codebook. It can combine with Huffman or rANS
  coding, but a new/adaptive codebook adds bytes and aggregation complexity.
- **Weight clustering:** useful for a relatively stable inference model; less
  convenient when every client changes the codebook every round.
- **Random projection, sketches, and compressed sensing:** send a lower
  dimensional update and reconstruct/aggregate at the server. Recovery error
  and metadata are difficult to justify for only 11,922 parameters.
- **Federated distillation:** exchange outputs, prototypes, reconstruction
  statistics, or cluster summaries instead of weights. This can be far smaller,
  but it requires a shared reference input or a redesigned unsupervised
  objective.
- **Event-triggered communication:** upload only when update norm, model drift,
  or anomaly-distribution change exceeds a threshold. It reduces average
  traffic, not worst-case payload, and can bias participation.
- **Alternating client participation:** one of two stations trains per round.
  This halves some traffic but weakens every-round representation and should be
  compared with both-client periodic averaging.
- **Asynchronous aggregation:** avoids waiting for both clients but creates
  stale updates and does not make an individual update smaller.
- **Knowledge distillation:** train a small student centrally/offline and then
  federate the student or its adapters. This is preferable to shrinking width
  without a teacher.
- **TFLite post-training quantization:** valuable for inference storage and
  possibly Pi CPU time, but it does not automatically change Flower's training
  arrays or communication codec.
- **bfloat16/fixed int16:** both use about two bytes per parameter. Float16 is
  the simpler control for the current small weight range; bfloat16 trades
  mantissa precision for range that this model does not presently need.
- **Secure aggregation:** arbitrary sparse or variable-scale payloads are not
  automatically compatible with secure aggregation. Define the privacy and
  aggregation protocol before combining them.
- **Analog over-the-air aggregation:** a wireless-research technique, but not
  compatible with the current Meshtastic packet radio and TCP bridge.
- **Transparent gRPC or bridge compression:** possible, but measured float32
  lossless gain is only about 12%. Parameter-aware compression is both smaller
  and easier to validate.

## Flower Integration Boundary

Current `NumPyClient` is convenient but it automatically:

1. converts incoming `Parameters.tensors` using `np.load`;
2. expects a list of ndarrays;
3. serializes returned ndarrays using `np.save`.

Flower's `arrays_size_mod` only logs sizes. It does not compress arrays.

Implementation options:

### Minimal float16 experiment

- strategy sends float16 ndarrays;
- client casts to float32 before `model.set_weights`;
- client returns float16 transport arrays;
- custom strategy casts every client result to float32 before weighted average;
- strategy casts the outbound aggregate back to float16.

Do not use stock in-place FedAvg on float16 if the acceptance criterion assumes
float32 aggregation.

### Recommended int8/6-bit implementation

Use either:

- legacy Flower `Client` and a custom `Strategy` with custom
  `Parameters(tensors=[...], tensor_type="tasik.int8.v1")`; or
- migrate to Flower's Message API and construct custom `Array`/byte records.

The codec belongs at the Tasik/Flower boundary. Meshnet should continue moving
an opaque, authenticated byte stream.

Required wire fields:

```text
magic and codec version
model bundle ID and global round
base-model version/hash
tensor schema hash
per-tensor bit width and scale layout
packed payload length
uncompressed logical length
SHA-256 or equivalent end-to-end digest
```

Reject:

- unknown codec versions;
- wrong tensor count/shape;
- non-finite scales;
- payload length mismatches;
- stale or unknown base hashes;
- digest mismatch.

Decode to float32 before FedAvg. Aggregate in float32. Quantize once for the
next outbound transfer.

## Local Dataset and Pi Processing Improvements

These reduce compute and RAM, not Flower model bytes.

### Stop materializing duplicate targets

Current windows:

```text
X shape        6,101 x 72 x 18 float32
X RAM          31,627,584 bytes
y = X.copy()   31,627,584 bytes
combined       60.3 MiB
```

An autoencoder target can be the input batch itself. Use a streaming
`tf.data.Dataset`, a safe strided view, or a generator and avoid the full `y`
copy.

### Use a training stride

Adjacent windows share 71 of 72 hours. Approximate current two-epoch batch
counts:

| Window stride | Windows | X+y RAM | Train batches |
| ---: | ---: | ---: | ---: |
| 1 | 6,101 | 60.3 MiB | 610 |
| 2 | 3,051 | 30.2 MiB | 306 |
| 3 | 2,034 | 20.1 MiB | 204 |
| 6 | 1,017 | 10.1 MiB | 102 |
| 12 | 509 | 5.0 MiB | 52 |

Start with stride 2 or 3 and compare rare-event coverage and convergence.
Validation should use a declared non-overlapping stride and independent raw
time range.

### Cap or sample the local history

Training over the entire growing database makes Pi work increase forever.
Use:

- a rolling recent horizon;
- a fixed-size replay sample of older normal periods;
- guaranteed retention of real anomaly/event windows;
- station-balanced and observed-value-aware sampling.

### Avoid repeated evaluation exports

Each current evaluate call performs:

- validation evaluation;
- a second prediction pass for thresholds;
- an encoder pass across all 6,101 windows;
- JSON, NPZ, NPY, and SQLite writes.

With approximate current batch defaults this is roughly 269 inference batches
per client per round in addition to 610 fit batches. Final-only evaluation
removes two-thirds of this repeated work.

### Do not infer Raspberry Pi time from laptop time

The local Mac diagnostic trained six epochs in roughly 23-30 seconds depending
on width. That is not a Pi benchmark. Use the existing per-phase timers on the
actual Pis and record:

- data load/preprocessing;
- each local epoch;
- validation;
- threshold prediction;
- embedding extraction;
- codec encode/decode;
- peak RSS/RAM and CPU temperature.

## Runtime and Repository Footprint

### Build a Pi release instead of cloning the research tree

The Pi release should exclude:

- `.git`;
- `temp_extract/`;
- source ZIPs;
- notebooks;
- raw intermediate exports;
- local caches and logs;
- redundant CSV after verified DB import.

Keep a research/data archive separately. Do not rewrite repository history as
part of the ML codec change.

### Replace the 17 MB HDBSCAN predictor if local footprint matters

`cluster_model.pkl` is an HDBSCAN object with prediction data and is about
17 MB. Measured lossless sizes:

```text
zlib/gzip   about 7.5 MB
lzma        about 4.8 MB
```

Compression saves storage but adds load time/RAM. Better candidates:

- validate K-Means or GMM if their health-label quality is acceptable;
- train a compact classifier to imitate the accepted HDBSCAN assignments;
- export condensed prototypes plus a small decision rule;
- retain HDBSCAN only on the training machine.

The current `requirements.txt` omits `hdbscan` even though the selected pickle
references `hdbscan.hdbscan_.HDBSCAN`. The runtime requirements need to include
the package required to unpickle the selected model, or the monitor will fail
before prediction.

## End-to-End Scenario Table

Current Mesh schedule, no RF loss or retry:

| Scenario | Model movements | Tensor bytes | Schedule floor |
| --- | ---: | ---: | ---: |
| current float32, 3 rounds, evaluate each | 18 | 876,816 | 61.2 min |
| current float32, final evaluate only | 14 | 681,968 | 47.6 min |
| float16, 3 rounds, evaluate each | 18 | 447,624 | 31.3 min |
| int8 group32, 3 rounds, evaluate each | 18 | 229,464 | 15.9 min |
| int8 group32, 3 rounds, final evaluate | 14 | 178,472 | 12.4 min |
| int8 group32, 2 x 3 epochs, final evaluate | 10 | 127,480 | 8.8 min |
| int8 group32, 1 x 6 epochs, final evaluate | 6 | 76,488 | 5.3 min |
| 6-bit group32, 2 x 3 epochs, final evaluate | 10 | 97,680 | 6.9 min |

These numbers do not include:

- protobuf/gRPC bytes;
- session open/close;
- local ML time;
- scheduler guard time;
- retries and failed windows;
- reconnect/restart;
- other radio users or regulatory airtime constraints.

Even the recommended case is an occasional maintenance/training operation, not
low-impact routine telemetry.

## Proposed Acceptance Targets

No explicit current model-byte target was found in the repository beyond the
old 700 B/s benchmark target. Use these engineering targets unless the project
adopts a different contract:

```text
first accepted codec       <= 13,000 bytes per model movement
stretch codec              <= 10,000 bytes per model movement
two-round complete run     <= 130,000 tensor bytes
routine telemetry          one or a few Mesh packets
```

Quality gates should be agreed before tuning. Proposed starting gates:

- five fixed-seed runs plus confidence intervals;
- no validation MSE regression above 1%;
- per-feature MSE and threshold drift reported, not hidden by an average;
- no material loss of recall on pH, turbidity, and dissolved-oxygen events;
- false-positive rate within the accepted operational band;
- at least 99% health-cluster label agreement against float32;
- embedding cosine and nearest-cluster margin distributions reported;
- same or better convergence after the chosen round/local-epoch schedule;
- station-by-station results on non-identical client partitions;
- exact serialized and end-to-end wire bytes;
- actual Pi processing/RAM/thermal measurements;
- full-model fallback after missed round, corrupt cache, and reconnect;
- real three-radio benchmark with hashes, RSSI, SNR, retries, and elapsed time.

## Implementation Order

### Phase 0 - make the baseline trustworthy

1. Fix evaporation/rainfall units and aggregation semantics.
2. Bound imputation and add observed-value-aware evaluation.
3. Split raw time before window construction and eliminate overlap leakage.
4. define genuine station/client partitions instead of cloning one historical
   dataset everywhere.
5. Fix final global weight export.
6. Regenerate one versioned 18-feature scaler/weights/threshold/cluster bundle.
7. Establish float32 anomaly, embedding, cluster, and Pi-time baselines.

### Phase 1 - low-risk communication controls

1. Add exact parameter and gRPC-message size logging.
2. Add final-only evaluate/export mode.
3. Compare 3x2, 2x3, and 1x6 round/epoch schedules.
4. Add float16 transport with float32 aggregation.
5. Run the complete quality and three-radio test gates.

### Phase 2 - recommended codec

1. Implement versioned dense int8 group32 encode/decode.
2. Dequantize and aggregate in float32.
3. Add bundle/base hashes, corruption rejection, and full fallback.
4. Combine with 2 rounds x 3 epochs if its non-IID quality holds.
5. Verify the approximately 12.7 KB movement and approximately 8.8-minute
   schedule floor on the real link.

### Phase 3 - stretch reductions

1. Test mixed int8/6-bit sensitivity by tensor.
2. Test zstd after quantization.
3. Add cached-base references.
4. Test dense quantized deltas.
5. Add sparse top-k only with persistent error feedback.
6. Prototype reliable model broadcast if downlink airtime remains dominant.

### Phase 4 - strategic ML redesign

1. Test width 24 and asymmetric 32/16 encoder/decoder.
2. Compare sequence-to-one prediction with full reconstruction.
3. Distill a compact model.
4. Prototype frozen-base rank-2/rank-4 federated adapters.
5. Move routine operation to anomaly/embedding telemetry with occasional
   global refreshes.

## Research Basis

- Flower's parameter conversion serializes every ndarray with `np.save`:
  https://flower.ai/docs/framework/1.23/fr/_modules/flwr/common/parameter.html
- Flower `Parameters` contains arbitrary tensor byte strings plus a
  `tensor_type`:
  https://flower.ai/docs/framework/1.20/en/ref-api/flwr.common.Parameters.html
- Flower documents custom client serialization, including sparse parameters:
  https://flower.ai/docs/framework/1.20/en/tutorial-series-customize-the-client-pytorch.html
- Flower `arrays_size_mod` logs array sizes; it is not a compression codec:
  https://flower.ai/docs/framework/ref-api/flwr.clientapp.mod.arrays_size_mod.html
- Flower's Message API provides custom `Array` byte payloads and strategy
  control:
  https://flower.ai/docs/framework/main/en/ref-api/flwr.common.Array.html
- TensorFlow Model Optimization recommends post-training quantization as the
  first experiment but does not offer general drop-in RNN/LSTM QAT coverage:
  https://www.tensorflow.org/model_optimization/guide/quantization/training.md
- QSGD provides the central communication/variance tradeoff for quantized
  updates:
  https://proceedings.neurips.cc/paper_files/paper/2017/hash/6c340f25839e6acdc73414517203f5f0-Abstract.html
- FedPAQ combines periodic averaging and quantized federated messages:
  https://proceedings.mlr.press/v108/reisizadeh20a
- Error feedback addresses the convergence defects of biased compression:
  https://proceedings.mlr.press/v97/karimireddy19a.html
- Deep Gradient Compression shows that extreme sparsity requires more than
  top-k alone:
  https://research.google/pubs/deep-gradient-compression-reducing-the-communication-bandwidth-for-distributed-training/
- Sparse Ternary Compression was designed for non-IID federated data:
  https://arxiv.org/abs/1903.02891
- LoRA establishes frozen-base low-rank adaptation:
  https://arxiv.org/abs/2106.09685
- CE-FedAvg specifically combines deterministic initialization, sparsification,
  quantization, and lightweight compression for LoRaWAN-constrained FL:
  https://eprints.cs.univie.ac.at/8732/

## Final Decision

The practical sweet spot is:

```text
fix the 18-feature data/artifact baseline
keep GRU(32)
use dense int8 group32 transport
aggregate in float32
run 2 rounds x 3 local epochs if non-IID testing accepts it
evaluate/export only at the end
target about 12.7 KB per movement and 127.5 KB per complete run
```

This reduces tensor traffic by about 85.5% relative to the current complete
run without first deleting environmental features or shrinking the embedding
model.

Six-bit, sparse deltas, model broadcast, and low-rank adapters are the next
levels. GRU(16), four-bit weights, and feature deletion should not be adopted
until task-level tests show that the lost information is acceptable.
