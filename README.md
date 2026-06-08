# MeshNet: Raspberry Pi + RAK Meshtastic Two-Node Test Network

MeshNet is a small Python app for a private two-node Meshtastic test network:

- Raspberry Pi 4
- RAK Meshtastic LoRa device over USB serial
- Malaysia LoRa region
- one master node and one slave node
- YAML config-driven radio setup and runtime
- compact JSON messages with app-level HMAC-SHA256 validation
- human-readable CLI output
- optional Telegram bridge

This project does not write raw LoRa packets and does not modify RAK firmware. It uses the official Meshtastic Python package, its `SerialInterface`, and the `meshtastic` CLI.

## Why Python

Python is the implementation path here because Raspberry Pi + USB serial automation is best served by the official `meshtastic[cli]` package. The Python package includes the CLI for persistent radio/channel settings and the common `SerialInterface` API for runtime send/receive. This keeps the codebase reliable and avoids generating two partial implementations.

Useful upstream docs:

- Meshtastic Python API and `SerialInterface`: https://python.meshtastic.org/
- Meshtastic Python CLI installation: https://meshtastic.org/docs/software/python/cli/installation/
- Meshtastic CLI usage and channel PSK commands: https://meshtastic.org/docs/software/python/cli/usage/
- LoRa region/configuration reference: https://meshtastic.org/docs/configuration/radio/lora/

## Hardware Setup

1. Attach the LoRa antenna to the RAK device before powering or transmitting.
2. Connect each RAK Meshtastic device to its Raspberry Pi 4 using a USB data cable.
3. Confirm each Pi can see its local device:

```bash
ls /dev/ttyACM* /dev/ttyUSB* 2>/dev/null
```

4. Confirm the radios can see each other after setup:

```bash
.venv/bin/meshtastic --nodes
```

Only one client can usually own the serial connection at a time. Close the Meshtastic web/app/CLI connection before starting MeshNet.

## Malaysia Region

Use the same region on both radios:

- `MY_919` for 919-924 MHz Malaysia hardware.
- `MY_433` for 433-435 MHz hardware.

Do not override legal frequency or duty-cycle settings. Keep the default `hop_limit: 3` unless you understand the mesh impact.

## Install

Run this on both Pis:

```bash
cd meshnet
./install.sh
```

The installer creates `meshnet/.venv`, installs Python dependencies, and adds the current user to the `dialout` group when available. Log out and back in if serial permissions changed.

## Configure Master

Edit `config.master.yaml` on the master Pi:

```yaml
app:
  node_id: "master-001"
  role: "master"
  node_name: "Master Pi Node"
  node_short_name: "MSTR"
```

Set a private shared password before real use:

```yaml
network:
  network_id: "ericmesh-malaysia-test"
  network_password: "change-this-password"
```

## Configure Slave

Edit `config.slave.yaml` on the slave Pi:

```yaml
app:
  node_id: "slave-001"
  role: "slave"
  node_name: "Slave Pi Node"
  node_short_name: "SLV1"
```

Only these should differ between master and slave:

- `app.node_id`
- `app.role`
- `app.node_name`
- `app.node_short_name`

These must match on both Pis:

- `radio.region`
- `radio.modem_preset`
- `radio.channel_name`
- `radio.channel_psk_mode`
- `radio.channel_psk_base64`, if using base64 mode
- `network.network_id`
- `network.network_password`

## Channel PSK Modes

`channel_psk_mode: derived` derives a 32-byte Meshtastic channel PSK from:

```text
SHA256(network_id + ":" + network_password)
```

Then it sends the key to the Meshtastic CLI as `base64:{key}`.

`channel_psk_mode: base64` uses `radio.channel_psk_base64` directly. It must decode to 16 or 32 bytes.

`channel_psk_mode: none` disables channel encryption for testing only.

## CLI Commands

Run commands from the `meshnet/` directory:

```bash
.venv/bin/python -m src.cli detect
.venv/bin/python -m src.cli preflight --config config.master.yaml
.venv/bin/python -m src.cli info --config config.master.yaml
.venv/bin/python -m src.cli setup-radio --config config.master.yaml
.venv/bin/python -m src.cli run --config config.master.yaml
.venv/bin/python -m src.cli discover --config config.master.yaml
.venv/bin/python -m src.cli send --config config.master.yaml --text "hello"
.venv/bin/python -m src.cli ping --config config.master.yaml
.venv/bin/python -m src.cli test --config config.master.yaml
.venv/bin/python -m src.cli telegram --config config.master.yaml
```

Helper scripts are also available:

```bash
scripts/detect-port.sh
scripts/preflight.sh config.master.yaml
scripts/apply-radio-config.sh config.master.yaml
scripts/show-nodes.sh
scripts/listen.sh config.slave.yaml
scripts/send-test.sh config.master.yaml "hello"
```

## Preflight Gate

MeshNet refuses to start radio-dependent commands unless the local machine looks like a real Meshtastic host.

Before `info`, `setup-radio`, `run`, `discover`, `ping`, `send`, `test`, or `telegram` proceeds, the CLI checks:

- the Meshtastic Python package is installed
- the `meshtastic` CLI is installed
- a configured serial port exists, or `/dev/ttyACM*` or `/dev/ttyUSB*` is attached
- the serial device is reachable through Meshtastic `SerialInterface`

Run the check directly:

```bash
.venv/bin/python -m src.cli preflight --config config.master.yaml
```

Expected success:

```text
[preflight] Checking required Meshtastic environment...
[preflight] Meshtastic Python package: OK
[preflight] Meshtastic CLI: /path/to/meshnet/.venv/bin/meshtastic
[preflight] USB serial radio: /dev/ttyACM0 OK
[preflight] Radio reachable: OK
[preflight] Preflight passed. Proceeding.
```

If the radio is missing, permissions are wrong, Meshtastic is not installed, or another client owns the serial port, MeshNet prints `BLOCKED - MeshNet will not start.` and exits before running the requested command.

## Apply Radio Setup

Run on the master Pi:

```bash
.venv/bin/python -m src.cli setup-radio --config config.master.yaml
```

Expected final line:

```text
[setup] Set up node: DONE.
```

Run on the slave Pi:

```bash
.venv/bin/python -m src.cli setup-radio --config config.slave.yaml
```

Expected final line:

```text
[setup] Set up node: DONE.
```

## Discover Nodes

Start the slave runtime first:

```bash
.venv/bin/python -m src.cli run --config config.slave.yaml
```

Then on the master:

```bash
.venv/bin/python -m src.cli discover --config config.master.yaml
```

Expected result:

```text
[network] Reached 1 compatible node.
[compat] TRUE NODE: slave-001
```

## Ping/Pong

On the slave:

```bash
.venv/bin/python -m src.cli run --config config.slave.yaml
```

On the master:

```bash
.venv/bin/python -m src.cli ping --config config.master.yaml
```

Default interval is 10 seconds. Values below 5 seconds are clamped to 5 seconds unless `runtime.allow_fast_ping_interval: true` is explicitly set.

## Full Test

With the slave runtime running, run on the master:

```bash
.venv/bin/python -m src.cli test --config config.master.yaml
```

The test suite checks:

- local radio connection
- node discovery
- compatibility
- HMAC validation
- JSON encode/decode
- ping/pong
- payload hash validation
- multi-message reliability

The reliability test passes at 80% acknowledgement or higher because LoRa/Meshtastic packets can be dropped.

Expected final line:

```text
[test] Final result: PASS
```

## Message Protocol

MeshNet sends compact JSON text messages over Meshtastic:

```json
{"body":"hello","dst":"slave-001","h":"1234abcd5678ef90","id":"a1b2c3","n":"ericmesh-malaysia-test","seq":1,"src":"master-001","t":"ping","ts":1710000000,"v":1}
```

The app signs each message without `h` using HMAC-SHA256 and `network.network_password`, stores the first 16 hex chars in `h`, and rejects invalid or wrong-network messages before processing.

MeshNet also rejects encoded envelopes above 230 characters. This is intentional: the JSON envelope itself consumes payload space, so very long human text should be shortened before sending.

## Telegram Bridge

Telegram is optional and requires internet on the master Pi.

1. Create a Telegram bot with BotFather.
2. Put the bot token and allowed chat ID in `config.master.yaml` or `.env`.
3. Enable Telegram:

```yaml
telegram:
  enabled: true
  bot_token: "123456:token"
  allowed_chat_id: "-123456789"
```

4. Start the bridge:

```bash
.venv/bin/python -m src.cli telegram --config config.master.yaml
```

Supported Telegram commands:

- `/start`
- `/status`
- `/nodes`
- `/discover`
- `/ping`
- `/test`

Text sent by the allowed chat is forwarded as a MeshNet `text` message. Valid incoming MeshNet text messages are posted back to Telegram.

## Run On Boot With Systemd

Edit the service files first. Replace `pi` and `/home/pi/meshnet` with your real user/path.

For master runtime:

```bash
sudo cp systemd/meshnet.service /etc/systemd/system/meshnet.service
sudo systemctl daemon-reload
sudo systemctl enable meshnet.service
sudo systemctl start meshnet.service
sudo journalctl -u meshnet.service -f
```

For Telegram bridge on the master:

```bash
sudo cp systemd/meshnet-telegram.service /etc/systemd/system/meshnet-telegram.service
sudo systemctl daemon-reload
sudo systemctl enable meshnet-telegram.service
sudo systemctl start meshnet-telegram.service
sudo journalctl -u meshnet-telegram.service -f
```

On the slave Pi, edit `meshnet.service` so the config path is `config.slave.yaml`.

## Troubleshooting

### No Serial Device Found

Output:

```text
[radio] No serial radio found.
```

Check:

- USB cable is a data cable, not charge-only.
- RAK device is powered.
- Device appears under `/dev/ttyACM*` or `/dev/ttyUSB*`.
- No other Meshtastic client is using the port.

### Permission Denied On `/dev/ttyACM0`

Add your user to `dialout` and log out/back in:

```bash
sudo usermod -a -G dialout $USER
```

### Nodes Show Up But App Says Wrong Network

Both radios may be visible at RF level, but MeshNet rejects messages unless `network.network_id` matches.

### Nodes Show Up But Messages Do Not Decode

Check both nodes use the same channel name, channel PSK, modem preset, and region. If Meshtastic cannot decrypt the channel, the app will not receive valid JSON text.

### HMAC Failed

`network.network_password` differs, or a message came from another app/network. Set the same password on both configs and rerun `setup-radio` if using derived PSK.

### Wrong Region

Set both configs to `MY_919` for 919-924 MHz hardware, or `MY_433` for 433-435 MHz hardware. Apply setup again on both radios.

### Wrong PSK

Use the same `network_id`, `network_password`, and `channel_psk_mode` on both nodes. For `base64` mode, `channel_psk_base64` must be identical.

### Wrong Channel Name

Set the same `radio.channel_name` on both configs and rerun `setup-radio`.

### Wrong Modem Preset

Set both configs to the same `radio.modem_preset`, default `LONG_FAST`.

### Wrong USB Cable

Charge-only USB cables often power the RAK but expose no serial port. Use a known data cable.

### RAK Not Detected

Try unplugging/replugging, checking `dmesg`, using another USB port, and confirming the board is running Meshtastic firmware.

### Telegram Chat ID Wrong

The bridge silently ignores chats that do not match `telegram.allowed_chat_id`. Confirm the numeric chat ID, including a leading minus sign for groups.

### Pi Has No Internet For Installation

Internet is only required to install packages and for Telegram. Install dependencies while online, or prepare a wheelhouse/apt cache separately.

### Meshtastic Works Without Internet After Installed

The Meshtastic radio mesh and Python runtime work offline after installation. Telegram still needs internet.

## Acceptance Flow

Master:

```bash
.venv/bin/python -m src.cli preflight --config config.master.yaml
.venv/bin/python -m src.cli setup-radio --config config.master.yaml
```

Slave:

```bash
.venv/bin/python -m src.cli preflight --config config.slave.yaml
.venv/bin/python -m src.cli setup-radio --config config.slave.yaml
.venv/bin/python -m src.cli run --config config.slave.yaml
```

Master:

```bash
.venv/bin/python -m src.cli discover --config config.master.yaml
.venv/bin/python -m src.cli ping --config config.master.yaml
.venv/bin/python -m src.cli test --config config.master.yaml
```
