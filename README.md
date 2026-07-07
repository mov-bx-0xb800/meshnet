# MeshNet: Reliable Raspberry Pi + RAK Meshtastic Network

MeshNet is a Python runtime for a private Meshtastic application network:

- Raspberry Pi 4
- RAK Meshtastic LoRa device over USB serial
- Malaysia LoRa region
- one central node and one or more pinned client nodes
- YAML config-driven radio setup and runtime
- compact JSON messages with app-level HMAC-SHA256 validation
- human-readable CLI output
- optional Telegram control inside the master runtime
- an authenticated, reliable binary byte-stream bridge for Flower/gRPC

This project does not write raw LoRa packets and does not modify RAK firmware. It uses the official Meshtastic Python package, its `SerialInterface`, and the `meshtastic` CLI.

For the three-node Tasik Chini Flower deployment, start with [FLOWER_BRIDGE.md](FLOWER_BRIDGE.md). The Flower bridge is a separate runtime and must not run alongside the normal master/slave or Telegram runtime on the same USB radio.

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

Do not override legal frequency, EIRP, or duty-cycle settings. The direct Tasik Chini star uses `SHORT_FAST`, an explicit in-band frequency slot, `CLIENT_MUTE`, and `hop_limit: 1`; it does not use Meshtastic relays.

## Install

Run this on both Pis:

```bash
cd meshnet
./install.sh
```

The installer creates `meshnet/.venv`, installs Python dependencies, and adds the current user to the `dialout` group when available. Log out and back in if serial permissions changed.

After installation, use the `meshnet` command:

```bash
meshnet how-to
meshnet check master
meshnet setup master
meshnet master
```

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

## Meshtastic Device Role

MeshNet has its own `app.role` (`master` or `slave`). Meshtastic firmware also has a real device role under `device.role`.

For Pi + USB serial MeshNet runtimes, keep both radios as:

```yaml
device:
  role: "CLIENT"
  rebroadcast_mode: "LOCAL_ONLY"
  node_info_broadcast_secs: 3600
  is_power_saving: false
  serial_enabled: true
```

Recommended options:

- `CLIENT`: default for master and slave; keeps serial/client access available.
- `CLIENT_MUTE`: acceptable for a direct-range endpoint that should not relay other traffic.
- `LOCAL_ONLY`: reduces rebroadcasting foreign/open mesh traffic while keeping this private channel useful.
- `serial_enabled: true`: required because MeshNet controls the radio over USB serial.

Avoid `ROUTER` or `REPEATER` for the master/slave runtime nodes. Those roles are for infrastructure devices, not USB-serial app endpoints.

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

Use these commands after running `./install.sh`:

```bash
meshnet detect
meshnet check master
meshnet check slave
meshnet info
meshnet setup master
meshnet setup slave
meshnet master
meshnet slave
meshnet nodes
meshnet registry
meshnet status
meshnet doctor
meshnet send "hello"
meshnet ping
meshnet test
meshnet telegram-id
meshnet how-to
```

`meshnet master` is the normal master entry point. It starts Telegram automatically when the bot token and allowed chat ID are configured; otherwise it runs the normal master heartbeat/ping loop. `meshnet telegram` remains as a legacy alias for the same unified master runtime.

Telegram mesh operations also reconnect the USB radio before retrying a command or background check.

MeshNet keeps a small SQLite state file next to the active config, for example `config.master.state.sqlite`. This stores discovered app IDs, Meshtastic node IDs, recent outbound delivery state, and duplicate-message protection.

For use from another Python project, see `LIBRARY_API.md`.

The old Python module form still works for debugging, but normal use should go through `meshnet`.

Helper scripts are still available:

```bash
scripts/detect-port.sh
scripts/preflight.sh config.master.yaml
scripts/apply-radio-config.sh config.master.yaml
scripts/show-nodes.sh
scripts/listen.sh config.slave.yaml
scripts/send-test.sh config.master.yaml "hello"
```

## Preflight Gate

MeshNet gates one-shot radio commands with preflight checks. Long-running runtimes use reconnect loops instead of exiting on transient USB loss.

Before one-shot radio commands such as `info`, `setup`, `discover`, `ping`, `send`, `test`, `doctor`, or `telegram` proceed, the CLI checks:

- the Meshtastic Python package is installed
- the `meshtastic` CLI is installed
- a configured serial port exists, or `/dev/ttyACM*` or `/dev/ttyUSB*` is attached
- the serial device is reachable through Meshtastic `SerialInterface`

Run the check directly:

```bash
meshnet check master
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

If the radio is missing, permissions are wrong, Meshtastic is not installed, or another client owns the serial port, MeshNet prints a coded problem and corrective action. Transient preflight failures retry according to `runtime.connect_retries`.

Long-running `master` and `slave` processes connect with retries and keep retrying after USB disconnects when `runtime.runtime_reconnect` is enabled.

## Apply Radio Setup

Run on the master Pi:

```bash
meshnet setup master
```

Expected final line:

```text
[setup] Set up node: DONE (1 attempt(s)).
```

Run on the slave Pi:

```bash
meshnet setup slave
```

Expected final line:

```text
[setup] Set up node: DONE (1 attempt(s)).
```

## Discover Nodes

Start the slave runtime first:

```bash
meshnet slave
```

Then on the master:

```bash
meshnet nodes
```

Expected result:

```text
[network] Reached 1 compatible node.
[compat] TRUE NODE: slave-001
```

Discovery retries according to `runtime.discovery_retries`. It also binds the MeshNet app ID to the radio's Meshtastic node ID. After discovery, direct messages are sent to that specific Meshtastic destination instead of being broadcast to the whole mesh. If a known app ID appears with a different Meshtastic node ID, MeshNet marks it as an identity change and ignores it until you trust the new binding:

```bash
meshnet registry
meshnet trust slave-001 '!12345678'
meshnet unpair slave-001
```

## Ping/Pong

On the slave:

```bash
meshnet slave
```

On the master:

```bash
meshnet ping
```

Default interval is 10 seconds. Values below 5 seconds are clamped to 5 seconds unless `runtime.allow_fast_ping_interval: true` is explicitly set.

Pings use both Meshtastic radio ACKs when a direct destination is known and MeshNet application ACKs (`pong`) with message-ID correlation. If a reply times out, MeshNet retries according to:

```yaml
runtime:
  radio_ack_timeout_seconds: 15
  send_retries: 3
  setup_retries: 3
  connect_retries: 3
  discovery_retries: 3
  retry_backoff_seconds: 5
  runtime_reconnect: true
  reconnect_delay_seconds: 5
  max_reconnect_attempts: 0
```

`max_reconnect_attempts: 0` means keep reconnecting until the radio returns or the process is stopped.

When a direct radio destination is not known yet, MeshNet temporarily falls back to broadcast and learns the destination from the signed reply.

## Status And Doctor

Use `status` for a fast state summary:

```bash
meshnet status
```

Use `doctor` when checking a Pi/radio:

```bash
meshnet doctor
```

`doctor` runs preflight, prints the active config fingerprint, connects to the local radio, reports the local Meshtastic node ID, and lists known radio nodes. The fingerprint is also sent in MeshNet hello messages so mismatched channel/network/runtime settings are visible.

## Full Test

With the slave runtime running, run on the master:

```bash
meshnet test
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

MeshNet sends compact JSON packets over Meshtastic `PRIVATE_APP` payloads:

```json
{"a":1,"af":"","b":{"target":"slave-001"},"d":"slave-001","h":"1234abcd5678ef90","i":"a1b2c3","n":"ericmesh-malaysia-test","q":1,"s":"master-001","t":"ping","ts":1710000000,"v":2}
```

The app signs each message without `h` using HMAC-SHA256 and an app key derived from `network.network_password`, stores the first 16 hex chars in `h`, and rejects invalid or wrong-network messages before processing. The `i` field is the message ID, `a` is the retry attempt, and `af` identifies the message being acknowledged.

MeshNet also rejects encoded envelopes above 230 characters. This is intentional: the JSON envelope itself consumes payload space, so human text is limited to 60 characters by default.

## Telegram

Telegram is optional and requires internet on the master Pi.

1. Create a Telegram bot with BotFather.
2. Put the bot token in `.env`, not in tracked YAML:

```bash
cp .env.example .env
```

```dotenv
TELEGRAM_BOT_TOKEN="123456:token"
TELEGRAM_ALLOWED_CHAT_ID=""
```

3. Open Telegram and send any message to your bot.
4. Print the recent chat IDs:

```bash
meshnet telegram-id
```

5. Copy the correct chat ID into `.env`:

```dotenv
TELEGRAM_ALLOWED_CHAT_ID="123456789"
```

Group chat IDs usually start with `-`.

6. Start the normal master runtime:

```bash
meshnet master
```

If Telegram is configured, `meshnet master` starts the Telegram bridge and the master runtime checks in one process. If Telegram is missing or cannot start, MeshNet logs the reason, retries startup, then falls back to the normal master runtime. Do not run a separate Telegram process against the same USB radio.

Supported Telegram commands:

- `/start`
- `/help`
- `/status`
- `/stats`
- `/nodes`
- `/discover`
- `/ping`
- `/test`
- `/events on`
- `/events off`
- `/send hello`

Text sent by the allowed chat is forwarded as a MeshNet `text` message. Valid incoming MeshNet text, status, and error messages are posted back to Telegram while event notifications are on. `/stats` reports bridge counters such as Telegram messages, mesh messages, text in/out, ping results, discoveries, quick tests, and last mesh RX/TX times.

## Run On Boot With Systemd

Edit the service files first. Replace `pi` and `/home/pi/meshnet` with your real user/path.

Use one service per Pi. On the master, `meshnet.service` starts the unified runtime and auto-enables Telegram when credentials are configured:

```bash
sudo cp systemd/meshnet.service /etc/systemd/system/meshnet.service
sudo systemctl daemon-reload
sudo systemctl enable meshnet.service
sudo systemctl start meshnet.service
sudo journalctl -u meshnet.service -f
```

On the slave Pi, edit `meshnet.service` so the config path is `config.slave.yaml`.

`systemd/meshnet-telegram.service` is kept only as a legacy alias and conflicts with `meshnet.service`; do not enable both.

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

Set all participating configs to the same `radio.modem_preset`. The Tasik Chini Flower bridge requires `SHORT_FAST`; the legacy message runtime can still use another mutually matching preset.

### Wrong USB Cable

Charge-only USB cables often power the RAK but expose no serial port. Use a known data cable.

### RAK Not Detected

Try unplugging/replugging, checking `dmesg`, using another USB port, and confirming the board is running Meshtastic firmware.

### Telegram Chat ID Wrong

Chats that do not match `telegram.allowed_chat_id` are ignored and logged. Confirm the numeric chat ID, including a leading minus sign for groups.

### Pi Has No Internet For Installation

Internet is only required to install packages and for Telegram. Install dependencies while online, or prepare a wheelhouse/apt cache separately.

### Meshtastic Works Without Internet After Installed

The Meshtastic radio mesh and Python runtime work offline after installation. Telegram still needs internet.

## Acceptance Flow

Master:

```bash
meshnet check master
meshnet setup master
```

Slave:

```bash
meshnet check slave
meshnet setup slave
meshnet slave
```

Master:

```bash
meshnet nodes
meshnet registry
meshnet ping
meshnet test
meshnet status
meshnet master
```
