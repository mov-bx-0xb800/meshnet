# Package Examples

## Complete Master And Slave

These two scripts cover setup, sending, listening, retries, and delivery confirmation:

- `master.py`: sets up the master, discovers the slave, and sends messages.
- `slave.py`: sets up the slave, listens, and acknowledges messages.

Install MeshNet on both Pis and edit the appropriate config file:

```bash
python -m pip install -e .
```

On the slave Pi:

```bash
python examples/slave.py
```

Then on the master Pi:

```bash
python examples/master.py
```

Both scripts start with `SETUP_RADIO = True`, so they apply the YAML settings to the radio before starting. Change it to `False` after the first successful setup. Run only one script/process per USB radio.

## What Is Actually Needed

Once on each Pi/node:

1. Install MeshNet.
2. Edit `config.master.yaml` or `config.slave.yaml`.
3. Attach the Meshtastic radio.
4. Apply the radio settings once with `meshnet setup master` or `meshnet setup slave`.

Both configs must share the same network password, region, modem preset, channel, and PSK settings. Their `app.role` and `app.node_id` must be different.

Every time you use it:

1. Start `python examples/slave.py` on the receiving node.
2. Start `python examples/master.py` on the sending node.

The scripts retry setup, verify the applied radio settings, retry discovery and delivery, and print a problem code plus corrective action. Long-running listeners reconnect after USB loss. Do not run another MeshNet process on the same radio.

You do not need to call setup, preflight, discovery, or Telegram from every script.

## Run The Samples

Install this checkout:

```bash
python -m pip install -e .
```

Open a script and change the values at the top:

```python
CONFIG_FILE = "config.master.yaml"
MESSAGE = "Hello from MeshNet"
```

The destination is already defined by `master_id` and `slave_id` in the config file.

Then run it:

```bash
python examples/slave.py
python examples/master.py
python examples/status.py
python examples/send_message.py
python examples/session.py
```

- `master.py`: complete setup and send flow.
- `slave.py`: complete setup and listening flow.
- `status.py`: view config, status, and known nodes.
- `send_message.py`: send one message.
- `session.py`: send multiple operations over one connection.
