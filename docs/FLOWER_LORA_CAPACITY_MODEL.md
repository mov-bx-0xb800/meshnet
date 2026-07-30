# Flower LoRa Capacity Model

Last refreshed: 2026-07-30

Scope: current TasikChiniResearch Flower model over the Meshnet split-TCP bridge.

## Unit Rule

LoRa modem speed is in kilobits per second.

Payload and file sizes are in bytes.

Do not mix these:

- `kbps` = kilobits per second.
- `KB/s` = kilobytes per second.
- `233 bytes` = current Meshtastic application-frame cap in this repo.
- `160 bytes` = current conservative bridge stream payload per DATA frame.

## Current ML Model

Current model code:

- `LOOKBACK_WINDOW = 72`
- `N_FEATURES = 18`
- encoder GRU units = `32`
- decoder GRU units = `32`
- output Dense units = `18`

Actual current 18-feature tensor layout from `../TasikChiniResearch/global_autoencoder_weights.npz`:

```text
arr_0 encoder kernel       (18, 96)  =  1,728 params
arr_1 encoder recurrent    (32, 96)  =  3,072 params
arr_2 encoder bias         (2, 96)   =    192 params
arr_3 decoder kernel       (32, 96)  =  3,072 params
arr_4 decoder recurrent    (32, 96)  =  3,072 params
arr_5 decoder bias         (2, 96)   =    192 params
arr_6 dense kernel         (32, 18)  =    576 params
arr_7 dense bias           (18,)     =     18 params
total                                  11,922 params
float32 raw bytes                      47,688 bytes
```

Flower serializes each NumPy array with `np.save`, so the effective Flower tensor payload is raw data plus `.npy` headers.

```text
current 18-feature Flower tensor bytes = 48,712 bytes per full model transfer
```

Stale artifact note:

- `../TasikChiniResearch/assets/global_autoencoder_weights.npz` is an older 15-feature model.
- That older artifact has 11,535 params and 47,164 Flower tensor bytes.
- `../TasikChiniResearch/LORA_TRANSPORT.md` still describes that older 15-feature size.
- Current code in `model.py`, `centralNode.py`, and `preprocessing.py` is 18-feature.

## Current Bridge Settings

Current bridge facts:

- Meshtastic application frame cap: `233` bytes.
- Stream header and tag reduce usable stream payload.
- Default DATA payload: `160` bytes (`200` bytes after the bridge header and tag).
- Window size: `8` DATA frames.
- ACK requested on the final missing DATA frame in a window.
- `frame_interval_ms`: `400`.
- `poll_interval_ms`: `500`.
- `POLL_DONE` is sent twice.
- `max_retries`: `8`.

## One Full Model Transfer

Using current `48,712` bytes and `160` byte DATA payloads:

```text
data frames = ceil(48,712 / 160) = 305
windows     = ceil(305 / 8)      = 39
```

Approximate minimum LoRa packet count for one model-sized movement:

```text
DATA      305
ACK        39
POLL       39
POLL_DONE  78
total     461 packets before open/close frames, retries, broadcasts, and firmware overhead
```

This packet count is an implementation estimate from current bridge logic, not a measured RF capture.

## Two Clients, Three Rounds

Current Flower defaults:

- `FLOWER_MIN_CLIENTS = 2`
- `FLOWER_NUM_ROUNDS = 3`

Repo docs define six full-model transmissions per round with two clients:

1. central model to client 1 for fit
2. central model to client 2 for fit
3. client 1 updated model to central
4. client 2 updated model to central
5. aggregated model to client 1 for evaluate
6. aggregated model to client 2 for evaluate

Three rounds:

```text
full model movements = 6 * 3 = 18
model payload only   = 18 * 48,712 = 876,816 bytes
DATA frames only     = 18 * 305    = 5,490 frames
minimum packets      = 18 * 461    = 8,298 packets
```

At the old repo target of `700 B/s` worst-link application goodput:

```text
876,816 / 700 = 1,252.6 seconds = about 20.9 minutes
```

That `20.9 minute` figure is payload-only and is not reachable with the current
packet schedule and `400 ms` frame pacing. Pacing `8,298` estimated packets
alone takes at least:

```text
8,298 * 0.400 = 3,319.2 seconds = about 55.3 minutes
```

One model movement has a packet-pacing ceiling of about `264 B/s` before USB
send acceptance time, poll delays, retries, and RF loss. The real full test can
therefore take considerably longer than 55 minutes.

The pacing floor still excludes:

- local training time,
- TCP/gRPC churn,
- radio queue delays,
- poor RSSI/SNR,
- retries,
- failed windows,
- node reboot/reconnect time,
- interference from other class-assigned users.

## SHORT_FAST Versus SHORT_TURBO

Meshtastic theoretical preset table:

| Preset | Raw rate | Approx raw bytes/sec | Bandwidth | Range effect |
| --- | ---: | ---: | ---: | --- |
| SHORT_FAST | 10.94 kbps | 1.37 KB/s | 250 kHz | current bridge target |
| SHORT_TURBO | 21.88 kbps | 2.74 KB/s | 500 kHz | shorter range, lower link budget |

`SHORT_TURBO` does not automatically double application goodput:

- bridge validation currently rejects it,
- frame pacing is still tuned around `SHORT_FAST`,
- Meshtastic and bridge headers still exist,
- ACK/POLL/POLL_DONE overhead remains,
- retries can erase the speed gain,
- range and fade margin get worse.

Working conclusion:

- `SHORT_TURBO` may help only after code changes, safe-slot enforcement, radio bench testing, and retuned pacing.
- If it causes worse packet loss, it can be slower than `SHORT_FAST` in the field.

## Feasibility Verdict

Current design is feasible for:

- controlled bench tests,
- short demonstrations,
- proving exact byte delivery,
- occasional small model transfers,
- one or two nearby line-of-sight nodes.

Current design is not a good production transport for:

- frequent full Flower training,
- unattended continuous operation,
- poor RF paths,
- dense or noisy channels,
- 923-924 MHz operation without certified FH/LBT or a real airtime ledger.

Best technical direction:

- quantize weights,
- send deltas instead of full weights,
- send fewer layers,
- reduce rounds,
- reduce client count per LoRa session,
- consider local-only training plus occasional summarized updates,
- keep LoRa for low-rate telemetry/control rather than full model transport.
