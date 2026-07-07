# MeshNet Python API

## What Is Required

The short Python call does not replace node setup.

Required once on each node:

1. Install the package.
2. Create/edit its master or slave YAML config.
3. Attach the Meshtastic radio.
4. Run `meshnet setup master` or `meshnet setup slave` once.

Both configs must share the same network password, region, modem preset, channel, and PSK settings. Their `app.role` and `app.node_id` must be different.

Required while sending:

1. Keep `meshnet slave` running on the receiving node.
2. Let only your Python project use the sending node's USB radio.

`send_message()` automatically loads the config, checks and opens the radio, retries, waits for receiver confirmation, and closes the radio. Run setup again only after changing radio/channel settings or replacing the radio.

Setup is retried and verified against the radio. Discovery is retried. Long-running master/slave runtimes reconnect after USB loss.

You do not need to call `setup_radio()`, `preflight()`, `discover()`, or Telegram from every script.

## Install From Git

```bash
python -m pip install "git+https://YOUR_GIT_URL/MESHTASTIC.git#subdirectory=meshnet"
```

## Examples

See [`examples/`](examples/README.md) for short runnable scripts:

- complete master setup and send flow
- complete slave setup and listening flow
- inspect config, status, and registry
- send a message and inspect delivery
- reuse a long-lived radio session

## Send A Message

```python
from meshnet_api import MeshNetClient

mesh = MeshNetClient("config.master.yaml")
delivery = mesh.send_message("Hello")

print("Delivered:", delivery.ok)
print("Status:", delivery.status)
print("Attempts:", delivery.attempts)
if not delivery.ok:
    print("Problem:", delivery.error_code, delivery.last_error)
    print("Fix:", delivery.action)
```

## Setup Result

```python
from meshnet_api import MeshNetClient

mesh = MeshNetClient("config.master.yaml")
setup = mesh.setup_radio()

print("Configured:", setup.ok)
if not setup.ok:
    print("Problem:", setup.error_code, setup.error)
    print("Fix:", setup.action)
```

## Discovery Result

```python
from meshnet_api import MeshNetClient

mesh = MeshNetClient("config.master.yaml")
discovery = mesh.discover_report()

print("Found:", discovery.ok)
print("Attempts:", discovery.attempts)
print("Nodes:", discovery.nodes)
```

## Check Status

```python
from meshnet_api import MeshNetClient

mesh = MeshNetClient("config.master.yaml")

print(mesh.status())
print(mesh.registry())
```

## Keep The Radio Open

```python
from meshnet_api import MeshNetSession

with MeshNetSession("config.master.yaml") as mesh:
    print(mesh.ping().ok)
    print(mesh.send_message("Hello").ok)
```

## Main Methods

- `config_summary()`
- `read_config_file()`
- `update_config(patch)`
- `preflight()`
- `setup_radio()`
- `status()`
- `registry()`
- `recent_outbound(limit)`
- `trust_node(app_id, mesh_id)`
- `unpair_node(app_id)`
- `detect_ports()`
- `discover()`
- `discover_report()`
- `send_message(text, dst=None)`
- `ping(dst=None)`
- `send_raw(message_type, dst, body, expect_reply_type=None)`
- `run_tests()`
- `run_master()`
- `run_slave()`

## Delivery Report

- `ok`
- `message_id`
- `message_type`
- `src`
- `dst`
- `ack_for`
- `seq`
- `attempts`
- `status`
- `last_error`
- `error_code`
- `action`
- `retryable`
- `reply`

Configuration errors raise `MeshNetError`. Operational setup, discovery, and delivery failures include an error code, problem, attempted count, and corrective action in their result.

Common codes: `RADIO_NOT_FOUND`, `RADIO_BUSY`, `RADIO_DISCONNECTED`, `SETUP_VERIFY_FAILED`, `DISCOVERY_TIMEOUT`, `RADIO_ACK_TIMEOUT`, and `APP_ACK_TIMEOUT`.
