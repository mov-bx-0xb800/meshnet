# Flower over Meshtastic deployment

## Scope

This runtime carries the existing Flower TCP/gRPC byte stream over Meshtastic raw LoRa data packets. It does not modify the Flower model, float32 weights, training epochs, aggregation, or evaluation logic. It is not LoRaWAN and it does not use Meshtastic's pre-alpha IP tunnel.

The deployment is a direct three-node star:

```text
Flower client 1 -> 127.0.0.1:8081 -> Meshnet client bridge --\
                                                            LoRa -> central bridge -> 127.0.0.1:8081 -> Flower server
Flower client 2 -> 127.0.0.1:8081 -> Meshnet client bridge --/
```

The central bridge opens one independent local TCP connection to Flower for each remote client. Only one radio transmitter is granted application airtime at a time.

## Protocol properties

- 233-byte maximum Meshtastic application frame.
- 24-byte binary header and 16-byte HMAC-SHA256 tag.
- 192-byte default stream payload.
- Eight-frame default transmission window.
- Cumulative ACK plus 32-bit selective ACK bitmap.
- Selective retransmission, duplicate rejection and in-order delivery.
- 64 KiB bounded buffers with TCP backpressure.
- Random session ID for every local TCP connection.
- Central round-robin downlink plus one-window client polls/grants.
- Pinned application ID to Meshtastic `!xxxxxxxx` identity mapping.
- Separate key derivation from the Meshtastic channel encryption key.

The bridge intentionally closes the local TCP connection after an unrecoverable radio/session failure. Flower then establishes a new gRPC connection instead of receiving replayed bytes from a failed TCP session.

## Prepare configurations

On the central Pi:

```bash
cp config.flower-central.example.yaml config.flower.yaml
```

On each client Pi:

```bash
cp config.flower-client.example.yaml config.flower.yaml
```

For client 2, change all `client-001` values to `client-002` and use a distinct node name/short name.

Replace before deployment:

1. `network.network_password` with the same long random secret on all three Pis.
2. Every placeholder `mesh_id` with the actual Meshtastic `!xxxxxxxx` ID.
3. `radio.tx_power` using the certified radio, measured cable/connector loss, and antenna gain.
4. `radio.frequency_slot` if the field spectrum survey selects a different legal slot.
5. Serial port if automatic detection is ambiguous.

The central config lists both clients. Each client config lists only the central node. A bridge-enabled config refuses to start if a peer mesh ID is missing or duplicated.

## Radio baseline

All radios must have identical:

- Region: `MY_919`
- Preset: `SHORT_FAST`
- Frequency slot: explicit and verified; example slot `8`
- Channel name and channel PSK
- Firmware version

Deployment settings:

- `hop_limit: 1`
- `device.role: CLIENT_MUTE`
- `ignore_mqtt: true`
- `ok_to_mqtt: false`
- power saving disabled while the Pi bridge is active
- no normal Meshnet heartbeat, ping, Telegram, or discovery runtime

`tx_power` is radio output, not system EIRP. Calculate:

```text
EIRP dBm = TX dBm - cable/connector loss dB + antenna gain dBi
```

Verify the final frequency, EIRP, equipment certification, and applicable MCMC requirements before field operation.

## Install and configure each radio

```bash
./install.sh
meshnet check --config config.flower.yaml
meshnet setup master --config config.flower.yaml   # central
meshnet setup slave --config config.flower.yaml    # each client
meshnet doctor --config config.flower.yaml
```

`doctor` must show matching region, preset, frequency slot, TX power, device role, channel and PSK.

## Start order

Central Pi:

```bash
cd /home/pi/TasikChiniResearch
FLOWER_SERVER_BIND_ADDRESS=127.0.0.1:8081 FLOWER_MIN_CLIENTS=2 python centralNode.py

cd /home/pi/meshnet
meshnet bridge --config config.flower.yaml
```

Each client Pi:

```bash
cd /home/pi/meshnet
meshnet bridge --config config.flower.yaml

cd /home/pi/TasikChiniResearch
FLOWER_SERVER_ADDRESS=127.0.0.1:8081 python clientNode.py
```

The central Flower process and central bridge can start in either order. Start client bridges before client Flower processes to avoid unnecessary gRPC reconnects.

## systemd

Install the bridge unit on all three Pis:

```bash
sudo cp systemd/meshnet-flower-bridge.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now meshnet-flower-bridge.service
```

Do not enable `meshnet.service` or `meshnet-telegram.service` on the same radio. Meshtastic serial access is intentionally single-owner.

## Metrics and logs

The bridge logs cumulative counters every `bridge.metrics_interval_seconds` and writes:

```text
config.flower.bridge-metrics.json
```

Important counters:

- `data_bytes_sent` / `data_bytes_received`
- `retransmitted_frames`
- `duplicate_frames`
- `out_of_order_frames`
- `invalid_frames`
- `sessions_opened` / `sessions_reset`

For the initial field acceptance test, require:

- exact hash match for repeated 47,164-byte binary transfers;
- no invalid/HMAC frames;
- sustained worst-link goodput of at least 700 B/s;
- normal retransmissions below 10%;
- one model transfer no slower than 75 seconds;
- ten consecutive complete three-round Flower runs.

## Bench verification

The software suite includes protocol tampering, dropped-frame recovery, a 47,164-byte split-TCP echo transfer, and two concurrent client tunnels:

```bash
.venv/bin/python -m unittest discover -s tests -v
```

These tests do not replace a three-radio bench test. Before lake deployment, repeat the same payload test through the real USB radios and record elapsed time, RSSI, SNR, retransmissions and hashes in both directions for each client.

For that real-radio test, stop Flower and run the included echo server on the central Pi while all three Meshnet bridges remain active:

```bash
# Central Pi, replacing Flower temporarily
python scripts/flower-bridge-benchmark.py server --host 127.0.0.1 --port 8081

# Each client Pi
python scripts/flower-bridge-benchmark.py client \
  --host 127.0.0.1 --port 8081 --bytes 47164 --count 10 --timeout 300
```

The client prints SHA-256 integrity, elapsed time, and application goodput for every model-sized transfer. Test clients separately first, then simultaneously to validate central scheduling.

## Tuning order

If the worst client misses the target:

1. Improve antenna height and Fresnel clearance.
2. Shorten or improve coax and connectors.
3. Reposition the central antenna or client antenna.
4. Survey and change the explicit frequency slot on all radios.
5. Increase `ack_timeout_seconds` if acknowledgements arrive late.
6. Reduce `window_size` if the firmware queue is dropping bursts.
7. Increase `frame_interval_ms` if radio queueing is unstable.

Do not move to `LONG_FAST` as the first fix. The unchanged float32 Flower traffic would take multiple hours for three rounds.
