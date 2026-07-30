# Tasik Flower over Meshnet/LoRa Full Transport Audit

Last verified: 2026-07-31, Asia/Kuala_Lumpur

## Purpose

This is the consolidated Meshnet-side source of truth for carrying the Tasik
Chini Flower workload through Meshtastic/LoRa.

It records:

- exact current model and Flower message sizes;
- Meshnet stream framing and reliable-window behavior;
- packet, window, schedule, and measured-time calculations;
- every 2026-07-30 physical benchmark finding and the resulting fixes;
- what the benchmark proves and does not prove;
- Tasik clustering, dataset, embedding, and summary transport implications;
- compression and scheduling options;
- service, log, and operational safeguards;
- Malaysia RF/compliance limits and remaining evidence;
- field acceptance requirements.

Companion ML/data document:

```text
../../../TasikChiniResearch/research-docs/FLOWER_ML_MESHNET_FULL_AUDIT_20260731.md
```

## Audit Snapshot

Meshnet:

```text
repository: /Users/wojake/Desktop/MESHTASTIC/meshnet
branch:     main
HEAD:       cde9e96c2e71afae31172f511577546763880ebd
state:      additional uncommitted reliability, safety, API, test, and doc work
```

Tasik Chini:

```text
repository: /Users/wojake/Desktop/TasikChiniResearch
branch:     codex/lora-flower-bridge
HEAD:       1dc713c35fb7ab746e1ec6a5e469baffa7705ed8
state:      ahead of feature upstream with additional local Flower fixes
```

Physical archives:

```text
flower-logs-central-20260730T132522Z-meshpi-4952.tar.gz
flower-logs-client-20260730T132528Z-node1-4366.tar.gz
```

Earlier failure/progress evidence:

```text
flower-logs-central-20260730T113835Z-meshpi-3938.tar.gz
flower-logs-client-20260730T113915Z-node1-3459.tar.gz

flower-logs-central-20260730T120137Z-meshpi-4196.tar.gz
flower-logs-client-20260730T120153Z-node1-3689.tar.gz

flower-logs-central-20260730T122228Z-meshpi-4443.tar.gz
flower-logs-client-20260730T122252Z-node1-3932.tar.gz
```

## Executive Verdict

1. The bridge now successfully transfers complete current-model-sized binary
   messages in both directions with exact hashes.
2. The latest archive proves **one complete synthetic two-client round**, not
   three rounds and not real TensorFlow/Flower training.
3. The completed round transferred **292,272 model bytes** in
   **1,662.635 seconds**, or **175.8 B/s**.
4. The current real Flower model has **11,922 float32 parameters** and
   **48,712 Flower NumPy tensor bytes** per full model.
5. A model-bearing Flower 1.31 protobuf is approximately **48,824 bytes**.
6. Two clients, three fit/evaluate rounds are approximately **879,852
   application-protobuf bytes** before gRPC/HTTP2, connection, and bridge
   control overhead.
7. At measured link goodput, the application transfer alone is approximately
   **83.3 minutes**.
8. The heavier Tasik clustering search does not change Flower radio bytes or
   rate. It changes offline CPU, RAM, repository size, and deployment.
9. Do not send embeddings, datasets, or the 49 MB HDBSCAN predictor over this
   link.
10. The bridge is controlled-demo feasible, but not yet proven for unattended
    production, compliance, or repeated full Flower sessions.

## Unit Rule

Keep radio rate and data size separate:

```text
kbps = kilobits per second
KB/s = kilobytes per second
B/s  = application payload bytes per second
```

Current reference values:

```text
SHORT_FAST nominal raw rate: approximately 10.94 kbps
SHORT_FAST nominal raw bytes: approximately 1.37 KB/s before overhead
Meshtastic application cap: 233 bytes
Meshnet maximum stream data: 193 bytes after its header and HMAC
current chosen stream data: 160 bytes
measured application goodput: approximately 176 B/s
```

Changing `payload_bytes` to 160, `window_size` to 8, or `poll_interval_ms` to
500 does not mean 160 B/s, 8 B/s, or 500 B/s. These are framing/scheduling
controls. Goodput is the delivered application bytes divided by wall-clock
time.

## System Boundary

```text
client Flower
  -> local TCP 127.0.0.1:8081
  -> client Meshnet bridge
  -> authenticated reliable-stream packets
  -> Meshtastic/LoRa
  -> central Meshnet bridge
  -> local TCP 127.0.0.1:8081
  -> central Flower
```

Flower and Meshnet are separate repositories and processes. Neither imports
the other.

Meshnet:

- transports opaque bytes;
- preserves byte order;
- verifies its own stream frames;
- schedules half-duplex radio turns;
- acknowledges/retries windows;
- closes a failed local TCP session instead of replaying a partial gRPC
  stream.

Meshnet does not:

- parse Flower protobufs;
- know tensor shapes;
- quantize or compress weights;
- aggregate models;
- transmit Tasik files that no application has written to its TCP socket.

## Current Flower and Bridge Configuration

Flower:

```text
client endpoint:              127.0.0.1:8081
central bind:                 127.0.0.1:8081
required clients:             2
rounds:                       3
client/phase timeout:         7,200 seconds
fit fraction:                 1.0
evaluate fraction:            1.0
required complete responses:  yes
```

Bridge:

```text
payload_bytes:           160
window_size:             8
ACK timeout:             5 seconds
control timeout:         10 seconds
maximum retries:         8
frame start interval:    400 ms
central poll interval:   500 ms
maximum TCP buffer:      65,536 bytes
metrics interval:        normally 60 seconds, 10 during benchmark
destination mode:        single_peer_broadcast
```

Radio:

```text
region:                  MY_919
preset:                  SHORT_FAST
frequency slot:          8
hop limit:               1
role:                    CLIENT_MUTE
custom frequency:        0
frequency offset:        0
MQTT LoRa ingress:       disabled
MQTT LoRa egress:        disabled
power saving:            disabled
```

Flower-specific examples use:

```text
network.allow_broadcast: false
channel:                 TASIKFL
central tx_power:        23 dBm conducted
client tx_power:         24 dBm conducted
```

The generic `config.master.yaml` is not automatically a production Flower
configuration. Use and validate the Flower-specific examples.

## Stream Frame Format

Current limits:

```text
Meshtastic data cap: 233 bytes
Meshnet header:        24 bytes
HMAC-SHA256 tag:       16 bytes, truncated
maximum data:         193 bytes
current data:         160 bytes
current encoded DATA: 200 bytes
```

Header fields include:

```text
magic
protocol version
frame type
flags
session ID
sequence
cumulative ACK
selective ACK bitmap
payload length
```

Frame types:

```text
OPEN
OPEN_OK
DATA
ACK
POLL
POLL_DONE
HALF_CLOSE
CLOSE
RESET
PING
```

The HMAC key is derived from:

```text
protocol version
network ID
network password
```

The bridge validates:

- magic/version;
- session ID;
- frame length;
- frame type;
- payload limit;
- HMAC;
- pinned sender identity;
- radio source versus declared sender where applicable.

This provides authenticated stream frames, but it is not a replacement for
physical access control, secret management, or end-to-end model-bundle
versioning.

## Reliable Stream Behavior

### Windowing

The sender:

1. takes at most eight 160-byte DATA chunks;
2. numbers them;
3. requests an ACK on the final missing DATA frame;
4. waits up to five seconds;
5. retransmits only frames not covered by cumulative/selective ACK state;
6. permits nine total attempts: the first plus eight retries;
7. fails the session when the window cannot be acknowledged.

The receiver:

- buffers out-of-order frames;
- delivers only ordered bytes to local TCP;
- discards duplicate delivery;
- reports cumulative and selective ACK state.

### Flow control

Local TCP readers only queue bytes. They do not decide when the radio may
transmit.

The 65,536-byte buffer provides backpressure. Large TCP messages can stream
through a bounded queue; they do not need to fit entirely in memory. That does
not make multi-megabyte LoRa transfers practical.

### Half-duplex scheduling

Central controls radio turns:

- central sends its downlink windows;
- central sends `POLL` to grant the client an uplink turn;
- client sends only during that grant;
- client sends `POLL_DONE` twice;
- central does not begin conflicting downlink DATA while the poll is active.

`POLL_DONE` repetition and guards are deliberate because short control frames
were accepted by the local firmware without always arriving at the peer.

### Connection behavior

- `OPEN_OK` is repeated five times.
- A central connection is not considered ready until the OPEN_OK burst
  completes.
- A failed stream closes the corresponding local TCP connection.
- Partial gRPC bytes are not replayed.
- `HALF_CLOSE` is not sent while acknowledged DATA remains pending.
- SIGTERM triggers graceful stop/final metrics publication.

### Destination mode

The first physical logs showed direct-unicast control delivery failing in one
direction. The current one-peer bridge uses Meshtastic broadcast addressing
for radio delivery while still:

- requiring the configured peer;
- authenticating the stream HMAC;
- rejecting frames from an unexpected source.

Multi-peer operation stays direct to avoid cross-talk. Direct mode can be
forced for diagnostics.

This use of broadcast destination is a delivery workaround, not an open
application broadcast. `network.allow_broadcast` for the normal Meshnet
application remains disabled in the Flower examples.

## Current Tasik Model Size

Model:

```text
lookback:       72
features:       18
encoder:        GRU(32)
embedding:      32
decoder:        GRU(32)
output:         Dense(18)
parameters:     11,922
raw float32:    47,688 bytes
Flower tensors: 48,712 bytes
```

Exact tensors:

| Tensor | Shape | Parameters | Raw bytes | `.npy` bytes |
| --- | ---: | ---: | ---: | ---: |
| encoder kernel | 18 x 96 | 1,728 | 6,912 | 7,040 |
| encoder recurrent | 32 x 96 | 3,072 | 12,288 | 12,416 |
| encoder bias | 2 x 96 | 192 | 768 | 896 |
| decoder kernel | 32 x 96 | 3,072 | 12,288 | 12,416 |
| decoder recurrent | 32 x 96 | 3,072 | 12,288 | 12,416 |
| decoder bias | 2 x 96 | 192 | 768 | 896 |
| dense kernel | 32 x 18 | 576 | 2,304 | 2,432 |
| dense bias | 18 | 18 | 72 | 200 |
| **total** | | **11,922** | **47,688** | **48,712** |

The older 15-feature values:

```text
11,535 parameters
47,164 Flower tensor bytes
```

are stale for current code.

## One Model Transfer

### Tensor-only benchmark size

```text
bytes:       48,712
DATA frames: ceil(48,712 / 160) = 305
windows:     ceil(305 / 8)      = 39
```

Approximate bridge packet count:

```text
DATA       305
ACK         39
POLL        39
POLL_DONE   78
total      461
```

Schedule floor:

```text
461 * 0.400 + 39 * 0.500
= 203.9 seconds
= approximately 239 B/s
```

This excludes open/close, local serial calls, guards, retries, and loss.

### Model-bearing Flower protobuf

Local Flower 1.31 serialization audit:

```text
model-bearing protobuf: approximately 48,824 bytes
DATA frames:            306
windows:                39
approximate packets:    462
schedule floor:         204.3 seconds
```

A typical measured evaluate response was approximately 170 bytes:

```text
DATA frames:         2
windows:             1
approximate packets: 6
schedule floor:      2.9 seconds
```

Real gRPC/HTTP2/TCP bytes are higher and connection-dependent.

## Full Round and Three-Round Math

Two clients with fit and federated evaluation:

```text
2 fit downlinks
2 fit uploads
2 evaluate downlinks
2 small evaluate responses
```

Tensor-only benchmark:

```text
one round:
6 * 48,712 = 292,272 model bytes

three rounds:
18 * 48,712 = 876,816 model bytes
5,490 DATA frames
702 planned model windows
8,298 approximate model packets
```

Schedule floor:

```text
8,298 * 0.400 = 3,319.2 seconds
702 * 0.500   =   351.0 seconds
total         = 3,670.2 seconds = 61.2 minutes
```

Application-protobuf estimate:

```text
one round:
6 * 48,824 + 2 * 170 = 293,284 bytes

three rounds:
879,852 bytes
approximately 8,352 DATA/control packets by the same simplified accounting
approximately 61.6-minute packet/poll schedule floor
```

At measured 176 B/s:

```text
one model protobuf: 277.4 seconds = 4m37s
one round:          1,666.4 seconds = 27m46s
three rounds:       4,999.2 seconds = 83m19s
```

The measured link, not the old 700 B/s target, should drive planning.

## Physical Benchmark Chronology

### 11:38Z: bridge handshake failed one way

Evidence:

- central received the client's OPEN;
- client received no central frame and no OPEN_OK;
- peer IDs and radio settings matched;
- firmware differed:
  - central `2.7.15.567b8ea`;
  - client `2.5.20.4c97351`.

Conclusion:

```text
central-to-client control delivery failed before Flower DATA
```

This was not yet a 48 KB payload problem.

Resulting fixes:

- one-peer bridge defaults to radio broadcast destination;
- application sender remains pinned and authenticated;
- OPEN_OK repetition;
- reply pacing;
- extra radio/broadcast metrics.

Security note:

The old archives contained sensitive radio material. Do not redistribute them.
Current runner redaction covers network passwords, channel PSKs, primary
channel URLs, private/public keys, fixed PINs, MQTT passwords, and Telegram
secrets.

### 12:01Z: handshake fixed, scheduling and frame-size failures exposed

Success:

- session opened in both directions;
- control and DATA moved both ways.

Failure 1:

```text
central queued local TCP while a client POLL turn was active
central and client attempted conflicting half-duplex transmission
stream_send_window_active reached 2
```

Fix:

- TCP readers only queue;
- central scheduler alone sends downlinks;
- client sends only during a poll grant;
- connection readiness waits for the OPEN_OK burst.

Failure 2:

```text
192-byte stream data -> 232-byte encoded frame
171-byte stream data -> 211-byte encoded frame
```

The client received the 211-byte frame but none of the 232-byte frames,
including retries.

Conclusion:

```text
local firmware “accepted” is not proof of over-air delivery
near-cap 232-byte frames were unsafe on this live firmware/radio pair
```

Fix:

```text
payload reduced 192 -> 160 bytes
encoded normal DATA reduced 232 -> 200 bytes
runner enforces the safe profile even with stale local YAML
```

Cost:

| Payload | DATA frames | Windows | Approx packets |
| --- | ---: | ---: | ---: |
| 192 B | 254 | 32 | 382 |
| 160 B | 305 | 39 | 461 |

The safe setting costs approximately 20.7% more estimated packets.

### 12:22Z: transfer was healthy but operator stopped it

The first 48,712-byte downlink reached:

```text
21,760 acknowledged bytes
17 acknowledged windows
approximately 45% complete
0 retransmissions
0 invalid frames
0 session resets
```

Both runners ended with status 130 because they were interrupted.

False diagnostics found:

- a 160-byte attempted-versus-sent difference meant a synchronous send was in
  progress, not failed;
- the client said the peer had not received DATA even after its hello ACK.

Actual runner problems:

- local YAML values differed;
- only payload was overridden;
- poll interval remained 1,000 ms;
- pacing delay was added after USB acceptance time;
- systemd services were not restored;
- final metrics were not guaranteed on SIGTERM;
- manual log publication was too easy and could delete source logs;
- optional HTTP log server had no authentication.

Resulting fixes:

- enforce all profile values through environment overrides;
- start-to-start frame pacing;
- progress requires multiple no-progress samples before fault diagnosis;
- show acknowledged queue progress, rate, ETA, packets, retries, and resets;
- print the schedule floor;
- defer HALF_CLOSE until radio DATA is acknowledged;
- graceful SIGTERM/final metrics;
- record and restore only previously active services;
- opt-in log publication;
- no local log deletion after publish;
- direct-to-base publishing requires a second explicit opt-in;
- optional export defaults to `http://172.20.10.2:8765/`.

### 13:25Z: first complete synthetic round

Central result:

```text
round:                   1
transfers:               6
model bytes:             292,272
elapsed:                 1,662.635 seconds
round goodput:           175.8 B/s
mean transfer goodput:   175.9 B/s
median transfer goodput: 176.9 B/s
p90 transfer goodput:    178.5 B/s
transfer range:          271.770-288.361 seconds
goodput range:           168.9-179.2 B/s
```

Client result:

```text
elapsed:                 1,661.915 seconds
round goodput:           175.9 B/s
mean transfer goodput:   179.1 B/s
transfer receive range:  264.134-281.585 seconds
goodput range:           173.0-184.4 B/s
```

Hashes matched for all completed transfer events.

Then:

- round-2 client-1 fit downlink completed;
- 7 transfers and 340,984 model bytes existed in each benchmark record;
- the operator interrupted at approximately 35 minutes;
- exit status was 130;
- no session reset occurred.

Final central bridge metrics at interruption:

```text
data sent:             246,901 bytes
data received:         127,869 bytes
retransmitted frames:  4
invalid frames:        0
duplicates:            0
out-of-order frames:   0
session resets:        0
pending bytes:         0
```

Final client metrics:

```text
data sent:             128,029 bytes
data received:         246,261 bytes
retransmitted frames:  0
invalid frames:        0
duplicates:            0
out-of-order frames:   16
session resets:        0
pending bytes:         20,758
send window active:    1
```

The pending/out-of-order state is consistent with interruption during the next
uplink. It is not a completed-run summary.

## What Is Proven

- the bridge can open a session both ways;
- 160-byte DATA / 200-byte encoded frames work on the tested pair;
- selective ACK/retry can move full 48,712-byte payloads;
- exact deterministic hashes match;
- central and client directions both work;
- one complete six-movement synthetic round works;
- measured application goodput is approximately 176 B/s;
- the benchmark runner captures useful progress and final bridge metrics;
- the export endpoint default is the hotspot address
  `http://172.20.10.2:8765/`, not `12.0.0.1`.

## What Is Not Proven

- a completed three-round synthetic run;
- real Flower protobuf/gRPC traffic over the radio;
- TensorFlow training and evaluation during radio operation;
- two independent client radios;
- fairness/contention between two clients;
- repeated autonomous success;
- Pi ML processing and thermal stability;
- no-loss performance over longer distance;
- RSSI/SNR acceptance;
- RF compliance;
- production security;
- recovery after a real mid-gRPC link failure.

## Tasik Non-Flower Data Implications

The heavier clustering code does not enter the bridge unless Tasik explicitly
writes its artifacts into Flower or another TCP application.

Current state:

| Object | Size | Sent by current code? |
| --- | ---: | :---: |
| Flower model protobuf | approximately 48,824 B | yes |
| evaluate metrics | approximately 170 B | yes |
| local embedding file | approximately 781,056 B | no |
| node summary vector | 76 B raw | no sender exists |
| node cluster runtime bundle | 3,241 B | no |
| calculated clustering runtime set | 49,134,583 B | no |
| source dataset ZIP | 102,477,314 B | no |
| extracted dataset | 912,955,609 B | no |

If mistakenly transferred at 176 B/s:

| Object | DATA frames | One-copy payload time |
| --- | ---: | ---: |
| local embeddings | 4,882 | 1h14m |
| node cluster bundle | 21 | 18.4s |
| calculated cluster runtime set | 307,092 | 77h33m |
| source ZIP | 640,484 | 6.74 days |
| extracted data | 5,705,973 | 60 days |

The 7,200-second Flower phase timeout would be exceeded by:

- two serialized local embedding uploads in one phase;
- any cluster artifact transfer;
- any dataset transfer.

The 49,134,583-byte value is the sum of the current predictor, embedding
scaler, optional PCA pickle, label map, and best-method metadata. It is not a
separate checked-in bundle file.

Correct boundary:

- Flower weights may cross LoRa occasionally;
- compact summaries may cross LoRa routinely;
- embeddings, clustering artifacts, and datasets use an out-of-band
  maintenance path.

## Full Suite Timing

Measured/estimated components:

| Component | Evidence | Time |
| --- | --- | ---: |
| three-round application transfer | projected from physical round | approximately 83.3 min |
| full local Flower ML critical path | Pi estimate | approximately 3-6 min |
| enhanced clustering | Pi estimate | approximately 8-20 min |
| node behavior processing | Pi estimate | approximately 5-12 min |
| anomaly pass | Pi estimate | approximately 0.3-1.5 min |

Full research workflow arithmetic:

```text
approximately 99.7-122.8 minutes
```

Planning range with ordinary runtime margin:

```text
approximately 1 hour 40 minutes to 2 hours 15 minutes
```

Normal field operation must prebuild the clustering artifact, reducing the
expected Flower-plus-local-ML session to approximately 85-100 minutes.

## Communication Reduction Options

No codec is currently implemented.

### Size and schedule table

| Scenario | Bytes/movement | Model movements | Model bytes | Simplified schedule floor |
| --- | ---: | ---: | ---: | ---: |
| float32, 3 rounds, evaluate each | 48,712 | 18 | 876,816 | 61.2 min |
| float32, final evaluate only | 48,712 | 14 | 681,968 | 47.6 min |
| float16, evaluate each | 24,868 | 18 | 447,624 | 31.3 min |
| int8 group32, evaluate each | 12,748 | 18 | 229,464 | 15.9 min |
| int8 group32, final evaluate | 12,748 | 14 | 178,472 | 12.4 min |
| int8 group32, 2 rounds x 3 epochs, final evaluate | 12,748 | 10 | 127,480 | 8.8 min |
| int8 group32, 1 round x 6 epochs, final evaluate | 12,748 | 6 | 76,488 | 5.3 min |
| 6-bit group32, 2 rounds x 3 epochs, final evaluate | 9,768 | 10 | 97,680 | 6.9 min |

These omit:

- protobuf/gRPC;
- session control;
- local ML;
- retries;
- reconnects;
- radio loss;
- other channel users.

### Lossless compression

Measured tensor stream:

```text
float32 Flower tensors: 48,712 bytes
zstd/gzip/zlib:          approximately 42.9 KB
reduction:               approximately 12%
```

Lossless compression alone does not solve the radio problem. It becomes more
useful after quantization.

### Safest current decision

Do not deploy a codec until Tasik has:

- trustworthy train/validation partitions;
- genuine non-identical clients;
- compatible scaler/weights/threshold/cluster artifacts;
- held-out anomaly and clustering acceptance metrics;
- reproducible final global weights.

Research order:

1. float32 reference;
2. final-only evaluation;
3. float16 transport with float32 aggregation;
4. dense int8 group32 with float32 aggregation;
5. only then deltas, six-bit, sparse updates, or model broadcast.

### Meshnet boundary

Parameter-aware compression belongs in Tasik/Flower, not in the opaque bridge.

A custom wire format needs:

```text
magic
codec version
model bundle ID
round/global version
base model hash
tensor schema hash
bit width and scale layout
logical and encoded lengths
end-to-end digest
```

Meshnet should continue transporting authenticated bytes exactly.

## Runtime and Service Reliability

### Meshnet services

`meshnet-flower-bridge.service`:

- conflicts with normal Meshnet/Telegram services so only one process owns the
  serial radio;
- uses `PYTHONUNBUFFERED=1`;
- uses `PYTHONFAULTHANDLER=1`;
- restarts on failure after five seconds;
- retains the shared temporary directory because the serial-owner lock must be
  visible to manual and service processes;
- uses `NoNewPrivileges=true`.

Normal and legacy Telegram services also conflict with the Flower bridge.

### Tasik services

- client is bound to the Meshnet bridge;
- bridge stop/restart cycles the client;
- Flower server restarts on failure after ten seconds;
- Flower client restarts on failure after fifteen seconds;
- normal successful completion does not restart immediately;
- StrictFedAvg makes missing required responses fail visibly.

Systemd detects process exit, not every possible live hang. Flower phase
timeouts and bridge progress metrics provide additional bounds, but an
independent health monitor is still appropriate for unattended operation.

## Benchmark and Log Safety

Current runner:

- forces loopback TCP;
- rejects enabled MQTT/Telegram paths;
- requires pinned peers;
- uses the attached LoRa radio only;
- disables irrelevant online version checks;
- captures Git commit and detected firmware;
- warns on peer mismatch by default;
- supports strict mismatch rejection when explicitly requested;
- enforces the complete bridge profile over stale YAML;
- restores only services that were active before testing;
- handles SIGTERM;
- archives logs locally;
- redacts secrets;
- does not publish automatically.

Publishing:

```text
PUBLISH_FLOWER_LOGS=1
```

is required. Direct-to-base publishing requires a second explicit opt-in.
Successful publication does not delete source logs.

Optional HTTP export:

```text
SERVE=1 ./scripts/export-latest-flower-logs.sh central
default URL: http://172.20.10.2:8765/
```

The server binds broadly by default so hotspot peers can reach it and has no
authentication. Use it only on the controlled local test network, stop it
after transfer, and never expose unreviewed archives.

## Current Test Coverage

Automated tests cover:

- maximum frame encode/decode;
- HMAC tamper rejection;
- selective retransmission after dropped DATA;
- lost-ACK recovery without duplicate delivery;
- Flower-sized binary stream over split TCP bridges;
- occupied local port failure;
- two-client central scheduling without cross-talk;
- single-peer broadcast default and forced direct mode;
- attached-radio configuration rejection;
- start-to-start frame pacing;
- runner override of stale YAML;
- production profile values;
- loopback-only endpoints;
- local data waiting for its scheduled radio turn;
- OPEN burst readiness;
- nonblocking HALF_CLOSE behavior;
- current model/round benchmark calculations;
- peer Git/firmware mismatch policy;
- NPZ tensor byte counting;
- radio identity pinning and spoof rejection;
- frequency override/offset rejection;
- serial-owner lock safety;
- binary radio payload preservation;
- service/config/API reliability paths.

Tests do not replace physical RF, firmware, Pi, or full Flower validation.

## RF and Malaysia Compliance Workbench

This is engineering guidance, not a final legal opinion or certification
package.

Current references:

- MCMC Class Assignment No. 2 of 2025;
- MCMC MTSFB TC T007:2026, Short Range Devices, Third Revision;
- MCMC MTSFB TC T022:2026, Communications Equipment Baseline Requirements;
- MTSFB technical-code index:
  https://mtsfb.org.my/technical-code/

Working 900 MHz limits:

| Band | EIRP limit | Additional access condition |
| --- | ---: | --- |
| 916-919 MHz | 25 mW, approximately 14 dBm | below 1% duty cycle or certified FH or certified LBT |
| 919-923 MHz | 500 mW, 27 dBm | no extra row condition shown |
| 923-924 MHz | 500 mW, 27 dBm | below 1% duty cycle or certified FH or certified LBT |

“No extra row condition shown” does not remove interference, emissions,
certification, or power obligations.

### Current frequency

SHORT_FAST slot 8:

```text
center:                 920.875 MHz
nominal bandwidth:      250 kHz
nominal occupied band:  920.750-921.000 MHz
```

This is directionally inside 919-923 MHz.

### EIRP

YAML `tx_power` is conducted radio power, not EIRP.

```text
EIRP dBm =
conducted power
+ external PA gain
+ antenna gain
- cable/connector loss
```

Examples:

```text
24 + 3.5 - 0.5 = 27.0 dBm EIRP
24 + 5.0 - 0.5 = 28.5 dBm EIRP, over the working limit
```

Use margin below 27 dBm. Do not approve antenna changes without updated math
and measurement.

### SHORT_TURBO

| Preset | Raw rate | Bandwidth | Link budget |
| --- | ---: | ---: | ---: |
| SHORT_FAST | 10.94 kbps | 250 kHz | 143 dB |
| SHORT_TURBO | 21.88 kbps | 500 kHz | 140 dB |

SHORT_TURBO has approximately 3 dB less link budget and therefore less range or
fade margin.

SHORT_TURBO slot 8:

```text
center:                922.750 MHz
nominal occupied band: 922.500-923.000 MHz
```

With oscillator/filter margin it can enter 923-924 MHz.

Restrictions:

- do not use SHORT_TURBO slot 8;
- do not use slots 9 or 10;
- if support is researched, calculate and validate safe slots, likely 2-7;
- do not assume doubled raw rate means doubled application goodput.

### Airtime

`frame_interval_ms` is pacing, not a legal airtime ledger.

The bridge sends:

- DATA;
- ACK;
- POLL;
- repeated POLL_DONE;
- OPEN/OPEN_OK;
- close/reset;
- retries.

No rolling per-radio airtime ledger currently proves a below-1% path.
Meshtastic frequency behavior is not by itself proof of certified frequency
hopping, and CAD/backoff is not by itself proof of certified LBT.

### Production controls

Production must control:

- Meshtastic app;
- BLE admin;
- serial CLI;
- web UI;
- admin channel;
- remote configuration;
- MQTT downlink;
- environment/YAML;
- test modes;
- reset and firmware downgrade.

Code-level readback is valuable but cannot prove EIRP, occupied bandwidth,
spurious emissions, or certification of the exact radio, firmware, antenna,
cable, power supply, and enclosure.

Before a field/commercial claim, retrieve the final current regulatory PDFs
and verify the SIRIM/MCMC conformity route.

## Remaining Risks

### P0

- no measured EIRP or occupied emission;
- no airtime ledger;
- firmware mismatch between tested radios;
- only one physical client radio tested;
- no completed three-round benchmark;
- no real Flower/TensorFlow-over-LoRa capture;
- no exact Pi processing data;
- no accepted locked production radio-control plan.

### P1

- measured goodput is only approximately 176 B/s;
- runtime is hour-scale;
- long sessions increase interference and restart probability;
- two-client contention/fairness is unmeasured;
- optional HTTP log export is unauthenticated;
- broadcast radio destination has greater RF visibility even though application
  frames remain pinned/authenticated;
- a gRPC reconnect restarts a session rather than resuming partial bytes;
- stale configuration outside the benchmark can still exist unless setup and
  doctor are run.

### Tasik-driven transport risks

- a future implementation could accidentally upload embeddings;
- a node-summary design has no transport schema;
- a codec without base/schema hashes could corrupt aggregation;
- stale/mixed ML artifacts can make a byte-perfect transport deliver the wrong
  model;
- a 49 MB pickle is unsuitable for radio distribution.

## Recommended Operating Design

Routine:

```text
LoRa carries small telemetry, anomaly flags, and compact summaries.
```

Occasional federation:

```text
LoRa carries only versioned Flower parameter/update messages.
```

Offline maintenance:

```text
datasets, clustering training, cluster artifacts, and full software releases
move through USB, Ethernet, Wi-Fi, or prepared storage.
```

Scheduling:

- run clustering only when Flower/Meshnet is stopped or on another machine;
- preinstall identical model bundles;
- avoid unnecessary evaluation downlinks;
- do not run continuous full-model federation;
- retain a full-model recovery path for any future compressed protocol.

## Next Validation Sequence

1. Put all radios on the same supported Meshtastic firmware line.
2. Verify Flower configs with setup and doctor.
3. Record exact radio model, antenna, cable, power, and geometry.
4. Complete one model transfer in every direction for each real client.
5. Complete a three-round synthetic run with one client.
6. Complete a three-round synthetic run with two physical clients.
7. Capture RSSI, SNR, retries, resets, hashes, and elapsed times.
8. Run actual Flower/TensorFlow over loopback Meshnet with the historical data.
9. Measure actual gRPC bytes and client processing.
10. Repeat ten complete sessions.
11. Test service restart/recovery and corrupt/interrupted sessions.
12. Complete RF measurement and certification review before field claims.

## Field Acceptance Gates

- exact matching Meshnet commit on all Pis;
- same supported firmware;
- pinned and verified radio IDs;
- loopback-only bridge endpoints;
- MQTT and Telegram disabled;
- `MY_919`, `SHORT_FAST`, slot 8, hop 1;
- custom frequency and offset both zero;
- calculated/measured EIRP below limit with margin;
- occupied emission inside intended band with margin;
- exact payload hashes in both directions;
- two independent clients;
- completed three rounds;
- no silent client loss;
- acceptable retransmission/reset rates;
- declared RSSI/SNR margin;
- actual Pi ML/RAM/temperature within bounds;
- log redaction reviewed;
- safe stop-transmitting procedure;
- ten consecutive complete real Flower sessions before unattended operation.

## Related Documents

- `FLOWER_LORA_CAPACITY_MODEL.md`
- `FLOWER_ROUND_BENCHMARK.md`
- `ML_DATA_SIZE_REDUCTION_AUDIT.md`
- `FLOWER_LOG_AUDIT_20260730.md`
- `FLOWER_LOG_AUDIT_20260730_1201.md`
- `FLOWER_LOG_AUDIT_20260730_1222.md`
- `RF_COMPLIANCE_WORKING_NOTES.md`
- `IMPLEMENTATION_TODO.md`
- `../FLOWER_BRIDGE.md`

## Primary References

- MTSFB technical-code index:
  https://mtsfb.org.my/technical-code/
- Meshtastic Python API:
  https://python.meshtastic.org/
- Flower NumPy client:
  https://flower.ai/docs/framework/main/en/ref-api/flwr.client.NumPyClient.html
- Flower parameter serialization:
  https://flower.ai/docs/framework/1.23/fr/_modules/flwr/common/parameter.html
- HDBSCAN prediction:
  https://hdbscan.readthedocs.io/en/latest/faq.html
- scikit-learn model persistence:
  https://scikit-learn.org/stable/model_persistence.html
- Raspberry Pi 4 specifications:
  https://www.raspberrypi.com/products/raspberry-pi-4-model-b/specifications/

## Final Decision Record

```text
current feasibility:
  controlled benchmark/demo: yes
  occasional small-model Flower experiment: yes, hour-scale
  continuous unattended full-model federation: no
  production/compliance proven: no

current radio profile:
  MY_919 / SHORT_FAST / slot 8 / 160-byte DATA / window 8

current measured goodput:
  approximately 176 application B/s

current three-round transfer planning:
  approximately 83.3 minutes before local ML and failures

clustering implication:
  no Flower/LoRa byte change; offline processing and deployment only

safe architecture:
  small/versioned LoRa messages, out-of-band datasets and artifacts
```
