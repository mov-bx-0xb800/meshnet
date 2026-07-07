# Flower and Meshnet setup

## Required structure

Each of the three Pis needs:

```text
/home/pi/TasikChiniResearch/
    .env
    centralNode.py or clientNode.py
    .venv/

/home/pi/meshnet/
    config.flower.yaml
    .venv/
```

Attach exactly one Meshtastic USB radio to each Pi. Only the Meshnet bridge may own that serial device. Do not run the normal Meshnet or Telegram service at the same time.

Use compatible 919 MHz radios with the same supported Meshtastic firmware version.

## Shared radio baseline

The Flower configuration examples define:

| Setting | Value |
| --- | --- |
| Region | `MY_919` |
| Modem preset | `SHORT_FAST` |
| Frequency slot | `8` |
| Channel index | `0` |
| Channel name | `TASIKFL` |
| Hop limit | `1` |
| Device role | `CLIENT_MUTE` |
| MQTT | disabled |
| Power saving | disabled |

All nodes must use the same region, preset, slot, channel name, network ID and network password. Meshnet derives the Meshtastic channel PSK from the shared network ID and password.

The examples use 23 dBm TX power on the central and 24 dBm on the clients. Confirm permitted EIRP using the actual radio, cable loss and antenna gain before field deployment.

## 1. Install both projects

Clone both repositories onto each Pi. From the Meshnet directory:

```bash
cd /home/pi/meshnet
./install.sh
```

Install TasikChiniResearch into its own `.venv` using the Python version supported by its TensorFlow build.

## 2. Create the Flower environment

On all three Pis:

```bash
cd /home/pi/TasikChiniResearch
cp .env.example .env
```

Keep these defaults for the three-node deployment:

```text
FLOWER_SERVER_ADDRESS=127.0.0.1:8081
FLOWER_SERVER_BIND_ADDRESS=127.0.0.1:8081
FLOWER_MIN_CLIENTS=2
FLOWER_NUM_ROUNDS=3
FLOWER_ROUND_TIMEOUT_SECONDS=0
```

Only the relevant client or server variables are used by each process.

## 3. Create the Meshnet configurations

Central Pi:

```bash
cd /home/pi/meshnet
cp config.flower-central.example.yaml config.flower.yaml
```

Each client Pi:

```bash
cd /home/pi/meshnet
cp config.flower-client.example.yaml config.flower.yaml
```

Generate one shared network password and place the same value in all three files:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

Replace the example values:

- Central config: actual Meshtastic IDs for client 1 and client 2.
- Client 1 config: actual central Meshtastic ID.
- Client 2 config: actual central Meshtastic ID, plus change application/node identifiers from `client-001` to `client-002`.
- All configs: the same long network password.
- Each config: verified TX power and serial port if automatic detection is ambiguous.

Meshtastic IDs are unique values resembling `!a1b2c3d4`. Do not copy one radio's ID onto another node.

## 4. Configure and verify the radios

Central Pi:

```bash
cd /home/pi/meshnet
meshnet check --config config.flower.yaml
meshnet setup master --config config.flower.yaml
meshnet doctor --config config.flower.yaml
```

Each client Pi:

```bash
cd /home/pi/meshnet
meshnet check --config config.flower.yaml
meshnet setup slave --config config.flower.yaml
meshnet doctor --config config.flower.yaml
```

Do not proceed until `doctor` confirms matching region, preset, frequency slot, channel, PSK, device role and TX configuration.

## 5. Start manually

Start the central Flower server first:

```bash
cd /home/pi/TasikChiniResearch
set -a
. ./.env
set +a
.venv/bin/python centralNode.py
```

In a second central terminal, start the central Meshnet bridge:

```bash
cd /home/pi/meshnet
.venv/bin/python -m src.cli bridge --config config.flower.yaml
```

On each client, start Meshnet first:

```bash
cd /home/pi/meshnet
.venv/bin/python -m src.cli bridge --config config.flower.yaml
```

Then start that client's Flower process:

```bash
cd /home/pi/TasikChiniResearch
set -a
. ./.env
set +a
.venv/bin/python clientNode.py
```

The central Flower server waits until both clients are connected before beginning a round.

## 6. Run with systemd

Install the Meshnet bridge unit on all three Pis:

```bash
cd /home/pi/meshnet
sudo cp systemd/meshnet-flower-bridge.service /etc/systemd/system/
```

On the central Pi, install `systemd/tasik-flower-server.service` from TasikChiniResearch. On each client, install `systemd/tasik-flower-client.service` instead.

Review `User`, `WorkingDirectory`, `ExecStart` and configuration paths in every copied unit before enabling them.

Central startup:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now tasik-flower-server.service
sudo systemctl enable --now meshnet-flower-bridge.service
```

Client startup:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now meshnet-flower-bridge.service
sudo systemctl enable --now tasik-flower-client.service
```

Inspect logs with:

```bash
journalctl -u meshnet-flower-bridge.service -f
journalctl -u tasik-flower-server.service -f
journalctl -u tasik-flower-client.service -f
```

## 7. Acceptance test

Before lake deployment, run the 47,164-byte benchmark described in [FLOWER_BRIDGE.md](FLOWER_BRIDGE.md) through all three physical radios. Confirm exact hashes, transfer time, retransmission rate, RSSI and SNR in both directions for each client. Then complete repeated three-round Flower runs before enabling unattended operation.
