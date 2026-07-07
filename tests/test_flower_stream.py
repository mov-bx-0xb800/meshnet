from __future__ import annotations

import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Callable

import yaml

from src.config import load_config
from src.flower_bridge import FlowerBridge
from src.reliable_stream import ReliableStream, StreamMetrics
from src.stream_protocol import (
    MAX_STREAM_PAYLOAD,
    FrameType,
    StreamFrame,
    StreamProtocolError,
    decode_frame,
    derive_stream_key,
    encode_frame,
)


class StreamProtocolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = load_config("config.master.yaml")
        self.key = derive_stream_key(self.cfg)

    def test_maximum_frame_round_trip(self) -> None:
        frame = StreamFrame(
            FrameType.DATA,
            123,
            sequence=42,
            ack=11,
            sack=0x5,
            payload=b"x" * MAX_STREAM_PAYLOAD,
        )
        encoded = encode_frame(frame, self.key)
        self.assertEqual(len(encoded), 233)
        self.assertEqual(decode_frame(encoded, self.key), frame)

    def test_tampered_frame_is_rejected(self) -> None:
        encoded = bytearray(encode_frame(StreamFrame(FrameType.PING, 1), self.key))
        encoded[-1] ^= 0x01
        with self.assertRaises(StreamProtocolError):
            decode_frame(bytes(encoded), self.key)

    def test_window_recovers_one_dropped_frame_without_replaying_acked_frames(self) -> None:
        delivered = bytearray()
        metrics_a = StreamMetrics()
        metrics_b = StreamMetrics()
        a: ReliableStream
        b: ReliableStream
        dropped = False

        def send_a(_peer: str, frame: StreamFrame) -> None:
            nonlocal dropped
            if frame.frame_type == FrameType.DATA and frame.sequence == 2 and not dropped:
                dropped = True
                return
            b.receive(frame)

        def send_b(_peer: str, frame: StreamFrame) -> None:
            a.receive(frame)

        a = ReliableStream(
            peer_id="b",
            session_id=9,
            payload_bytes=32,
            window_size=8,
            ack_timeout_seconds=0.01,
            max_retries=3,
            max_buffer_bytes=4096,
            send_frame=send_a,
            deliver=lambda _data: None,
            metrics=metrics_a,
        )
        b = ReliableStream(
            peer_id="a",
            session_id=9,
            payload_bytes=32,
            window_size=8,
            ack_timeout_seconds=0.01,
            max_retries=3,
            max_buffer_bytes=4096,
            send_frame=send_b,
            deliver=delivered.extend,
            metrics=metrics_b,
        )
        payload = bytes(range(256))
        a.queue_bytes(payload)
        sent = a.send_window()

        self.assertEqual(sent, len(payload))
        self.assertEqual(bytes(delivered), payload)
        self.assertEqual(metrics_a.retransmitted_frames, 1)

    def test_window_recovers_a_lost_ack_without_duplicate_delivery(self) -> None:
        delivered = bytearray()
        metrics_a = StreamMetrics()
        metrics_b = StreamMetrics()
        a: ReliableStream
        b: ReliableStream
        dropped_ack = False

        def send_a(_peer: str, frame: StreamFrame) -> None:
            b.receive(frame)

        def send_b(_peer: str, frame: StreamFrame) -> None:
            nonlocal dropped_ack
            if frame.frame_type == FrameType.ACK and not dropped_ack:
                dropped_ack = True
                return
            a.receive(frame)

        common = {
            "session_id": 10,
            "payload_bytes": 32,
            "window_size": 4,
            "ack_timeout_seconds": 0.01,
            "max_retries": 2,
            "max_buffer_bytes": 4096,
        }
        a = ReliableStream(
            peer_id="b",
            send_frame=send_a,
            deliver=lambda _data: None,
            metrics=metrics_a,
            **common,
        )
        b = ReliableStream(
            peer_id="a",
            send_frame=send_b,
            deliver=delivered.extend,
            metrics=metrics_b,
            **common,
        )
        payload = b"lost-ack-must-not-duplicate-delivery" * 3
        a.queue_bytes(payload)
        a.send_window()

        self.assertEqual(bytes(delivered), payload)
        self.assertEqual(metrics_a.retransmitted_frames, 4)
        self.assertEqual(metrics_b.duplicate_frames, 4)


class MemoryBus:
    def __init__(self) -> None:
        self.transports: dict[str, MemoryTransport] = {}

    def register(self, transport: "MemoryTransport") -> None:
        self.transports[transport.node_id] = transport

    def send(self, source: str, destination: str, payload: bytes) -> None:
        target = self.transports[destination]
        if target.handler is not None:
            target.handler(source, payload)


class MemoryTransport:
    def __init__(self, bus: MemoryBus, node_id: str) -> None:
        self.bus = bus
        self.node_id = node_id
        self.handler: Callable[[str, bytes], None] | None = None

    def set_handler(self, handler: Callable[[str, bytes], None]) -> None:
        self.handler = handler

    def start(self) -> None:
        self.bus.register(self)

    def stop(self) -> None:
        pass

    def send(self, peer_id: str, payload: bytes) -> None:
        self.bus.send(self.node_id, peer_id, payload)


class FlowerBridgeIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.echo_port = free_port()
        self.bridge_port = free_port()
        self.echo_stop = threading.Event()
        self.echo_thread = threading.Thread(
            target=run_echo_server,
            args=(self.echo_port, self.echo_stop),
            daemon=True,
        )
        self.echo_thread.start()
        wait_for_port(self.echo_port)

    def tearDown(self) -> None:
        self.echo_stop.set()
        try:
            with socket.create_connection(("127.0.0.1", self.echo_port), timeout=0.2):
                pass
        except OSError:
            pass

    def test_two_split_tcp_bridges_transfer_flower_sized_binary_stream(self) -> None:
        central_cfg = self._config(
            name="central",
            node_id="central-001",
            role="master",
            master_id="central-001",
            slave_id="client-001",
            peer_id="client-001",
            peer_mesh="!00000001",
            listen_port=free_port(),
            upstream_port=self.echo_port,
        )
        client_cfg = self._config(
            name="client",
            node_id="client-001",
            role="slave",
            master_id="central-001",
            slave_id="client-001",
            peer_id="central-001",
            peer_mesh="!00000002",
            listen_port=self.bridge_port,
            upstream_port=self.echo_port,
        )
        bus = MemoryBus()
        central = FlowerBridge(central_cfg, MemoryTransport(bus, "central-001"))
        client = FlowerBridge(client_cfg, MemoryTransport(bus, "client-001"))
        central.start()
        client.start()
        self.addCleanup(client.stop)
        self.addCleanup(central.stop)
        wait_for_port(self.bridge_port)

        payload = bytes((index * 31) % 256 for index in range(47_164))
        with socket.create_connection(("127.0.0.1", self.bridge_port), timeout=2) as app:
            app.settimeout(15)
            app.sendall(payload)
            received = receive_exact(app, len(payload))
            app.shutdown(socket.SHUT_WR)
            self.assertEqual(app.recv(1), b"")

        self.assertEqual(received, payload)
        self.assertGreaterEqual(client.metrics.data_bytes_sent, len(payload))
        self.assertGreaterEqual(central.metrics.data_bytes_sent, len(payload))
        self.assertEqual(client.metrics.retransmitted_frames, 0)
        self.assertEqual(central.metrics.retransmitted_frames, 0)
        wait_for_condition(lambda: not client._connections and not central._connections)

    def test_client_start_fails_loudly_when_local_flower_port_is_occupied(self) -> None:
        occupied_port = free_port()
        cfg = self._config(
            name="client-conflict",
            node_id="client-001",
            role="slave",
            master_id="central-001",
            slave_id="client-001",
            peer_id="central-001",
            peer_mesh="!00000002",
            listen_port=occupied_port,
            upstream_port=self.echo_port,
        )
        bus = MemoryBus()
        bridge = FlowerBridge(cfg, MemoryTransport(bus, "client-001"))
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as occupied:
            occupied.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            occupied.bind(("127.0.0.1", occupied_port))
            occupied.listen(1)
            with self.assertRaises(OSError):
                bridge.start()
        bridge.stop()

    def test_central_schedules_two_clients_without_cross_talk(self) -> None:
        client_one_port = free_port()
        client_two_port = free_port()
        central_cfg = self._config(
            name="central-two",
            node_id="central-001",
            role="master",
            master_id="central-001",
            slave_id="client-001",
            peer_id="client-001",
            peer_mesh="!00000001",
            peers=[
                {"app_id": "client-001", "mesh_id": "!00000001"},
                {"app_id": "client-002", "mesh_id": "!00000002"},
            ],
            listen_port=free_port(),
            upstream_port=self.echo_port,
        )
        client_one_cfg = self._config(
            name="client-one",
            node_id="client-001",
            role="slave",
            master_id="central-001",
            slave_id="client-001",
            peer_id="central-001",
            peer_mesh="!00000003",
            listen_port=client_one_port,
            upstream_port=self.echo_port,
        )
        client_two_cfg = self._config(
            name="client-two",
            node_id="client-002",
            role="slave",
            master_id="central-001",
            slave_id="client-002",
            peer_id="central-001",
            peer_mesh="!00000003",
            listen_port=client_two_port,
            upstream_port=self.echo_port,
        )
        bus = MemoryBus()
        central = FlowerBridge(central_cfg, MemoryTransport(bus, "central-001"))
        client_one = FlowerBridge(client_one_cfg, MemoryTransport(bus, "client-001"))
        client_two = FlowerBridge(client_two_cfg, MemoryTransport(bus, "client-002"))
        central.start()
        client_one.start()
        client_two.start()
        self.addCleanup(client_two.stop)
        self.addCleanup(client_one.stop)
        self.addCleanup(central.stop)
        wait_for_port(client_one_port)
        wait_for_port(client_two_port)

        payloads = {
            client_one_port: bytes((index * 17) % 256 for index in range(18_000)),
            client_two_port: bytes((index * 29 + 7) % 256 for index in range(21_000)),
        }
        results: dict[int, bytes] = {}

        def transfer(port: int, payload: bytes) -> None:
            with socket.create_connection(("127.0.0.1", port), timeout=2) as app:
                app.settimeout(15)
                app.sendall(payload)
                results[port] = receive_exact(app, len(payload))

        workers = [
            threading.Thread(target=transfer, args=(port, payload), daemon=True)
            for port, payload in payloads.items()
        ]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=20)

        self.assertEqual(results, payloads)
        self.assertGreaterEqual(central.metrics.sessions_opened, 2)

    def _config(
        self,
        *,
        name: str,
        node_id: str,
        role: str,
        master_id: str,
        slave_id: str,
        peer_id: str,
        peer_mesh: str,
        peers: list[dict[str, str]] | None = None,
        listen_port: int,
        upstream_port: int,
    ):
        raw = {
            "app": {
                "node_id": node_id,
                "role": role,
                "node_name": name,
                "node_short_name": name[:4].upper(),
                "log_level": "ERROR",
            },
            "radio": {
                "port": "auto",
                "region": "MY_919",
                "modem_preset": "SHORT_FAST",
                "hop_limit": 1,
                "frequency_slot": 8,
                "tx_power": 20,
                "ignore_mqtt": True,
                "ok_to_mqtt": False,
                "channel_index": 0,
                "channel_name": "TEST",
                "channel_psk_mode": "derived",
                "channel_psk_base64": "",
                "transmit_enabled": True,
            },
            "device": {
                "role": "CLIENT_MUTE",
                "rebroadcast_mode": "LOCAL_ONLY",
                "node_info_broadcast_secs": 86400,
                "is_power_saving": False,
                "serial_enabled": True,
            },
            "network": {
                "network_id": "test-flower",
                "network_password": "test-secret-value",
                "allow_broadcast": False,
                "master_id": master_id,
                "slave_id": slave_id,
                "peers": peers or [{"app_id": peer_id, "mesh_id": peer_mesh}],
            },
            "runtime": {
                "heartbeat_interval_seconds": 3600,
                "ping_interval_seconds": 60,
                "ack_timeout_seconds": 1,
                "radio_ack_timeout_seconds": 1,
                "send_retries": 2,
                "setup_retries": 2,
                "connect_retries": 2,
                "discovery_retries": 2,
                "retry_backoff_seconds": 0,
                "runtime_reconnect": False,
                "reconnect_delay_seconds": 0,
                "max_reconnect_attempts": 0,
                "test_message_count": 1,
                "max_payload_chars": 60,
                "discovery_timeout_seconds": 1,
                "compatibility_timeout_seconds": 1,
                "allow_fast_ping_interval": False,
            },
            "telegram": {"enabled": False, "bot_token": "", "allowed_chat_id": ""},
            "bridge": {
                "enabled": True,
                "listen_host": "127.0.0.1",
                "listen_port": listen_port,
                "upstream_host": "127.0.0.1",
                "upstream_port": upstream_port,
                "payload_bytes": 192,
                "window_size": 8,
                "ack_timeout_seconds": 0.1,
                "control_timeout_seconds": 0.5,
                "max_retries": 3,
                "frame_interval_ms": 0,
                "poll_interval_ms": 50,
                "max_buffer_bytes": 65536,
                "metrics_interval_seconds": 60,
            },
        }
        path = Path(self.temp.name) / f"{name}.yaml"
        path.write_text(yaml.safe_dump(raw), encoding="utf-8")
        return load_config(path)


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_for_port(port: int, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                return
        except OSError:
            time.sleep(0.02)
    raise TimeoutError(f"port {port} did not open")


def wait_for_condition(condition: Callable[[], bool], timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.02)
    raise TimeoutError("condition was not reached")


def run_echo_server(port: int, stop: threading.Event) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(("127.0.0.1", port))
        server.listen(4)
        server.settimeout(0.2)
        while not stop.is_set():
            try:
                connection, _ = server.accept()
            except socket.timeout:
                continue
            threading.Thread(target=echo_connection, args=(connection,), daemon=True).start()


def echo_connection(connection: socket.socket) -> None:
    with connection:
        while True:
            data = connection.recv(4096)
            if not data:
                return
            connection.sendall(data)


def receive_exact(connection: socket.socket, size: int) -> bytes:
    received = bytearray()
    while len(received) < size:
        data = connection.recv(size - len(received))
        if not data:
            break
        received.extend(data)
    return bytes(received)


if __name__ == "__main__":
    unittest.main()
