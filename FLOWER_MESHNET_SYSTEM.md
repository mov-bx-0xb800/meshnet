# Flower and Meshnet system overview

## Purpose

TasikChiniResearch continues to use Flower for federated learning. Meshnet adds the off-grid transport between three physical nodes:

- one central node;
- two remote client nodes;
- one Meshtastic USB radio attached to each node.

Flower and Meshnet are separate repositories, installations and Linux processes. Neither project imports the other. They communicate only through a local TCP connection on each Pi.

## Process layout

The central Pi runs:

1. `centralNode.py`, the Flower server;
2. the Meshnet central bridge, which owns the central Meshtastic radio.

Each client Pi runs:

1. the Meshnet client bridge, which owns that client's Meshtastic radio;
2. `clientNode.py`, the Flower client.

```text
Client 1 Flower -> localhost TCP -> Client 1 Meshnet --\
                                                        LoRa -> Central Meshnet -> localhost TCP -> Central Flower
Client 2 Flower -> localhost TCP -> Client 2 Meshnet --/
```

The central Flower server sees two ordinary Flower client connections. It does not know that the middle of each connection is LoRa.

## Port ownership

All local endpoints use `127.0.0.1:8081`, but ownership differs by role:

| Node | Process listening on port 8081 | Connecting process |
| --- | --- | --- |
| Central | Flower server | Meshnet central bridge |
| Client 1 | Meshnet bridge | Flower client 1 |
| Client 2 | Meshnet bridge | Flower client 2 |

Because every address is loopback-only, port 8081 is not exposed over Wi-Fi or Ethernet.

## Responsibilities

Flower remains responsible for:

- model initialization and float32 tensors;
- client training and evaluation;
- federated rounds and aggregation;
- training metrics.

Meshnet is responsible for:

- converting the local Flower byte stream into Meshtastic packets;
- scheduling the clients so only one application stream transmits at a time;
- acknowledgements, selective retransmission and duplicate removal;
- ordered byte-stream reconstruction;
- direct central-to-client radio routing.

Meshnet does not inspect, convert, quantize or compress the Flower model.

## Startup order

Use this order for predictable startup:

1. Start Flower on the central Pi.
2. Start Meshnet on the central Pi.
3. Start Meshnet on client 1 and client 2.
4. Start Flower on client 1 and client 2.

The two central processes can technically start in either order. Starting Flower first ensures the local upstream server exists when the first client radio session arrives. On a client, start Meshnet first because it must listen locally before Flower connects.

## Connection and failure behavior

When a Flower client opens its local connection, its Meshnet bridge requests a radio session with the central bridge. The central bridge then opens a separate local TCP connection to the central Flower server.

If a radio session becomes unrecoverable, Meshnet closes the affected local TCP connection. Flower reconnects and starts a fresh session. Meshnet does not replay a partially failed byte stream.

## Repository relationship

Install both repositories beside each other on every Pi:

```text
/home/pi/TasikChiniResearch/
/home/pi/meshnet/
```

They are not Git submodules and do not share a Python virtual environment. Each repository keeps its own dependencies and systemd service.

See [FLOWER_MESHNET_SETUP.md](FLOWER_MESHNET_SETUP.md) for deployment commands. See [FLOWER_BRIDGE.md](FLOWER_BRIDGE.md) for detailed framing, retransmission and benchmark information.
