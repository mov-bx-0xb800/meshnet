from __future__ import annotations

import json
import queue
import secrets
import socket
import threading
import time
from typing import Callable, Protocol

from . import logger
from .config import MeshConfig
from .errors import MeshNetError, as_meshnet_error, log_meshnet_error
from .radio import RadioClient, packet_from_mesh_id
from .reliable_stream import ReliableStream, ReliableStreamError, StreamMetrics
from .stream_protocol import (
    FLAG_EMPTY,
    FrameType,
    StreamFrame,
    StreamProtocolError,
    decode_frame,
    derive_stream_key,
    encode_frame,
    is_stream_payload,
)


class FrameTransport(Protocol):
    def set_handler(self, handler: Callable[[str, bytes], None]) -> None: ...

    def start(self) -> None: ...

    def stop(self) -> None: ...

    def send(self, peer_id: str, payload: bytes) -> None: ...


class RadioFrameTransport:
    def __init__(self, cfg: MeshConfig) -> None:
        self.cfg = cfg
        self.radio = RadioClient(cfg, "bridge-radio")
        self._handler: Callable[[str, bytes], None] | None = None
        self._send_lock = threading.Lock()
        self._next_send = 0.0

    def set_handler(self, handler: Callable[[str, bytes], None]) -> None:
        self._handler = handler

    def start(self) -> None:
        self.radio.add_binary_handler(self._on_binary)
        self.radio.connect(no_nodes=False)

    def stop(self) -> None:
        self.radio.close()

    def send(self, peer_id: str, payload: bytes) -> None:
        mesh_id = self.cfg.mesh_id_for(peer_id)
        if not mesh_id:
            raise MeshNetError(
                "BRIDGE_PEER_UNPINNED",
                "bridge",
                f"no pinned Meshtastic mesh ID for {peer_id}",
                "Add this peer's !xxxxxxxx mesh_id to network.peers.",
            )
        with self._send_lock:
            remaining = self._next_send - time.monotonic()
            if remaining > 0:
                time.sleep(remaining)
            self.radio.send_bytes(payload, destination_id=mesh_id, want_ack=False)
            full_interval = self.cfg.bridge.frame_interval_ms / 1000.0
            # Full data frames need the configured SHORT_FAST airtime spacing.
            # Compact ACK/control frames need much less airtime, but retain a
            # conservative 60 ms floor so the firmware queue is not flooded.
            interval = max(0.060, full_interval * min(1.0, len(payload) / 233.0))
            self._next_send = time.monotonic() + interval

    def _on_binary(self, payload: bytes, packet: dict) -> None:
        if not is_stream_payload(payload):
            return
        mesh_id = packet_from_mesh_id(packet).lower()
        peer_id = self.cfg.app_id_for_mesh(mesh_id)
        if peer_id is None:
            logger.line("security", f"Ignored stream frame from unpinned radio {mesh_id or 'unknown'}.")
            return
        if self._handler is not None:
            self._handler(peer_id, payload)


class BridgeConnection:
    def __init__(
        self,
        *,
        peer_id: str,
        session_id: int,
        local_socket: socket.socket,
        cfg: MeshConfig,
        send_frame: Callable[[str, StreamFrame], None],
        metrics: StreamMetrics,
        on_local_eof: Callable[["BridgeConnection"], None],
        on_error: Callable[["BridgeConnection", Exception], None],
    ) -> None:
        self.peer_id = peer_id
        self.session_id = session_id
        self.socket = local_socket
        self.cfg = cfg
        self.open_event = threading.Event()
        self.poll_done_event = threading.Event()
        self.poll_lock = threading.Lock()
        self.closed = threading.Event()
        self.local_eof = threading.Event()
        self.remote_eof = threading.Event()
        self.local_half_close_sent = threading.Event()
        self._completion_lock = threading.Lock()
        self._completion_started = False
        self.next_poll_sequence = 1
        self.expected_poll_sequence = 0
        self.last_poll_sequence = 0
        self.last_poll_bytes = 0
        self._on_local_eof = on_local_eof
        self._on_error = on_error
        queue_slots = max(8, cfg.bridge.max_buffer_bytes // cfg.bridge.payload_bytes)
        self._rx_queue: "queue.Queue[bytes | None]" = queue.Queue(maxsize=queue_slots)
        self.stream = ReliableStream(
            peer_id=peer_id,
            session_id=session_id,
            payload_bytes=cfg.bridge.payload_bytes,
            window_size=cfg.bridge.window_size,
            ack_timeout_seconds=cfg.bridge.ack_timeout_seconds,
            max_retries=cfg.bridge.max_retries,
            max_buffer_bytes=cfg.bridge.max_buffer_bytes,
            send_frame=send_frame,
            deliver=self._queue_incoming,
            metrics=metrics,
        )
        self._reader = threading.Thread(
            target=self._reader_loop,
            name=f"bridge-read-{peer_id}-{session_id}",
            daemon=True,
        )
        self._writer = threading.Thread(
            target=self._writer_loop,
            name=f"bridge-write-{peer_id}-{session_id}",
            daemon=True,
        )

    def start(self) -> None:
        self.socket.settimeout(1.0)
        self._reader.start()
        self._writer.start()

    def close(self) -> None:
        if self.closed.is_set():
            return
        self.closed.set()
        self.stream.close()
        try:
            self._rx_queue.put_nowait(None)
        except queue.Full:
            pass
        try:
            self.socket.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.socket.close()
        except OSError:
            pass

    def mark_remote_eof(self) -> None:
        if self.remote_eof.is_set():
            return
        self.remote_eof.set()
        self._rx_queue.put(None)

    def begin_completed_close(self) -> bool:
        """Claim teardown once both TCP directions have completed."""
        with self._completion_lock:
            if (
                self._completion_started
                or self.closed.is_set()
                or not self.local_half_close_sent.is_set()
                or not self.remote_eof.is_set()
            ):
                return False
            self._completion_started = True
            return True

    def _queue_incoming(self, payload: bytes) -> None:
        while not self.closed.is_set():
            try:
                self._rx_queue.put(payload, timeout=0.5)
                return
            except queue.Full:
                continue
        raise ReliableStreamError("connection closed while delivering received bytes")

    def _reader_loop(self) -> None:
        try:
            while not self.closed.is_set():
                try:
                    data = self.socket.recv(4096)
                except socket.timeout:
                    continue
                if not data:
                    self.local_eof.set()
                    self._on_local_eof(self)
                    return
                self.stream.queue_bytes(data)
        except (OSError, ReliableStreamError) as exc:
            if not self.closed.is_set():
                self._on_error(self, exc)

    def _writer_loop(self) -> None:
        try:
            while not self.closed.is_set():
                try:
                    data = self._rx_queue.get(timeout=0.5)
                except queue.Empty:
                    continue
                if data is None:
                    try:
                        self.socket.shutdown(socket.SHUT_WR)
                    except OSError:
                        pass
                    return
                self.socket.sendall(data)
        except OSError as exc:
            if not self.closed.is_set():
                self._on_error(self, exc)


class FlowerBridge:
    def __init__(self, cfg: MeshConfig, transport: FrameTransport | None = None) -> None:
        if not cfg.bridge.enabled:
            raise ValueError("bridge.enabled must be true")
        if cfg.network.network_password in {
            "change-this-password",
            "REPLACE_WITH_A_LONG_RANDOM_SHARED_SECRET",
        }:
            raise ValueError("replace the example network password before starting the bridge")
        self.cfg = cfg
        self.transport = transport or RadioFrameTransport(cfg)
        self.transport.set_handler(self._on_transport_payload)
        self.key = derive_stream_key(cfg)
        self.metrics = StreamMetrics()
        self._connections: dict[str, BridgeConnection] = {}
        self._connections_lock = threading.RLock()
        self._running = threading.Event()
        self._listener: socket.socket | None = None
        self._threads: list[threading.Thread] = []
        self._central_open_locks: dict[str, threading.Lock] = {
            peer.app_id: threading.Lock() for peer in cfg.network.peers
        }
        self._seen_sessions: dict[tuple[str, int], float] = {}

    @property
    def is_central(self) -> bool:
        return self.cfg.app.role == "master"

    def start(self) -> None:
        self._running.set()
        self.transport.start()
        if self.is_central:
            scheduler = threading.Thread(
                target=self._central_scheduler,
                name="flower-bridge-scheduler",
                daemon=True,
            )
            scheduler.start()
            self._threads.append(scheduler)
        else:
            self._listener = self._create_client_listener()
            listener = threading.Thread(
                target=self._client_listener,
                name="flower-bridge-listener",
                daemon=True,
            )
            listener.start()
            self._threads.append(listener)
        reporter = threading.Thread(
            target=self._metrics_reporter,
            name="flower-bridge-metrics",
            daemon=True,
        )
        reporter.start()
        self._threads.append(reporter)

    def run_forever(self) -> None:
        self.start()
        logger.line("bridge", f"Flower bridge running as {'central' if self.is_central else 'client'}.")
        try:
            while self._running.is_set():
                time.sleep(1)
                if isinstance(self.transport, RadioFrameTransport) and not self.transport.radio.is_connected():
                    raise MeshNetError(
                        "RADIO_DISCONNECTED",
                        "bridge",
                        "the Meshtastic USB connection was lost",
                        "Reconnect the USB radio; the bridge service will restart.",
                        retryable=True,
                    )
        except KeyboardInterrupt:
            logger.line("bridge", "Stopping.")
        finally:
            self.stop()

    def stop(self) -> None:
        if not self._running.is_set():
            return
        self._running.clear()
        if self._listener is not None:
            try:
                self._listener.close()
            except OSError:
                pass
            self._listener = None
        with self._connections_lock:
            connections = list(self._connections.values())
            self._connections.clear()
        for connection in connections:
            connection.close()
        self.transport.stop()

    def _send_frame(self, peer_id: str, frame: StreamFrame) -> None:
        self.transport.send(peer_id, encode_frame(frame, self.key))

    def _send_control(
        self,
        peer_id: str,
        frame_type: FrameType,
        session_id: int,
        *,
        flags: int = 0,
        payload: bytes = b"",
        repeat: int = 1,
        sequence: int = 0,
    ) -> None:
        frame = StreamFrame(
            frame_type,
            session_id,
            sequence=sequence,
            flags=flags,
            payload=payload,
        )
        for _ in range(repeat):
            self._send_frame(peer_id, frame)
            self.metrics.frames_sent += 1

    def _on_transport_payload(self, peer_id: str, payload: bytes) -> None:
        try:
            frame = decode_frame(payload, self.key)
        except StreamProtocolError as exc:
            self.metrics.invalid_frames += 1
            logger.line("security", f"Rejected bridge frame from {peer_id}: {exc}")
            return
        if peer_id not in {peer.app_id for peer in self.cfg.network.peers}:
            self.metrics.invalid_frames += 1
            return

        if frame.frame_type in {FrameType.DATA, FrameType.ACK}:
            connection = self._connection_for(peer_id, frame.session_id)
            if connection is None:
                self._send_control(peer_id, FrameType.RESET, frame.session_id, payload=b"unknown session")
                return
            connection.stream.receive(frame)
            return

        self.metrics.frames_received += 1
        if frame.frame_type == FrameType.OPEN and self.is_central:
            threading.Thread(
                target=self._handle_central_open,
                args=(peer_id, frame.session_id),
                name=f"bridge-open-{peer_id}",
                daemon=True,
            ).start()
        elif frame.frame_type == FrameType.OPEN_OK and not self.is_central:
            connection = self._connection_for(peer_id, frame.session_id)
            if connection is not None:
                connection.open_event.set()
        elif frame.frame_type == FrameType.POLL and not self.is_central:
            connection = self._connection_for(peer_id, frame.session_id)
            if connection is not None:
                threading.Thread(
                    target=self._serve_client_poll,
                    args=(connection, frame.sequence),
                    name=f"bridge-poll-{frame.session_id}",
                    daemon=True,
                ).start()
        elif frame.frame_type == FrameType.POLL_DONE and self.is_central:
            connection = self._connection_for(peer_id, frame.session_id)
            if (
                connection is not None
                and frame.sequence == connection.expected_poll_sequence
            ):
                connection.poll_done_event.set()
        elif frame.frame_type == FrameType.HALF_CLOSE:
            connection = self._connection_for(peer_id, frame.session_id)
            if connection is not None:
                connection.mark_remote_eof()
                self._complete_half_closed_connection(connection)
        elif frame.frame_type in {FrameType.CLOSE, FrameType.RESET}:
            connection = self._connection_for(peer_id, frame.session_id)
            if connection is not None:
                self._remove_connection(connection)

    def _client_listener(self) -> None:
        listener = self._listener
        if listener is None:
            return
        central_id = self.cfg.network.peers[0].app_id
        logger.line(
            "bridge",
            f"Listening for Flower on {self.cfg.bridge.listen_host}:{self.cfg.bridge.listen_port}",
        )
        while self._running.is_set():
            try:
                local_socket, address = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            logger.line("bridge", f"Flower client connected locally from {address[0]}:{address[1]}")
            session_id = self._new_session_id()
            connection = self._make_connection(central_id, session_id, local_socket)
            self._replace_connection(connection)
            connection.start()
            opened = False
            for _ in range(self.cfg.bridge.max_retries + 1):
                self._send_control(central_id, FrameType.OPEN, session_id)
                if connection.open_event.wait(self.cfg.bridge.control_timeout_seconds):
                    opened = True
                    break
            if not opened:
                logger.line("bridge", f"Central did not open session {session_id}; closing local Flower socket.")
                self._remove_connection(connection)
            else:
                self.metrics.sessions_opened += 1
                logger.line("bridge", f"Flower tunnel session {session_id} is open.")

    def _create_client_listener(self) -> socket.socket:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind((self.cfg.bridge.listen_host, self.cfg.bridge.listen_port))
            listener.listen(2)
            listener.settimeout(1.0)
            return listener
        except Exception:
            listener.close()
            raise

    def _handle_central_open(self, peer_id: str, session_id: int) -> None:
        lock = self._central_open_locks[peer_id]
        with lock:
            existing = self._connection_for(peer_id, session_id)
            if existing is not None:
                self._send_control(peer_id, FrameType.OPEN_OK, session_id, repeat=2)
                return
            seen_at = self._seen_sessions.get((peer_id, session_id))
            if seen_at is not None:
                self._send_control(peer_id, FrameType.RESET, session_id, payload=b"stale session")
                return
            try:
                upstream = socket.create_connection(
                    (self.cfg.bridge.upstream_host, self.cfg.bridge.upstream_port),
                    timeout=self.cfg.bridge.control_timeout_seconds,
                )
            except OSError as exc:
                logger.line("bridge", f"Cannot connect Flower upstream for {peer_id}: {exc}")
                self._send_control(peer_id, FrameType.RESET, session_id, payload=b"upstream unavailable")
                return
            connection = self._make_connection(peer_id, session_id, upstream)
            self._replace_connection(connection)
            self._seen_sessions[(peer_id, session_id)] = time.time()
            self._prune_seen_sessions()
            connection.start()
            self.metrics.sessions_opened += 1
            self._send_control(peer_id, FrameType.OPEN_OK, session_id, repeat=2)
            logger.line(
                "bridge",
                f"Opened {peer_id} session {session_id} to "
                f"{self.cfg.bridge.upstream_host}:{self.cfg.bridge.upstream_port}",
            )

    def _central_scheduler(self) -> None:
        delay = self.cfg.bridge.poll_interval_ms / 1000.0
        while self._running.is_set():
            with self._connections_lock:
                connections = list(self._connections.values())
            if not connections:
                time.sleep(delay)
                continue
            for connection in connections:
                if not self._running.is_set() or connection.closed.is_set():
                    continue
                try:
                    if connection.stream.pending_bytes:
                        connection.stream.send_window()
                    connection.poll_done_event.clear()
                    poll_sequence = connection.next_poll_sequence
                    connection.next_poll_sequence += 1
                    connection.expected_poll_sequence = poll_sequence
                    self._send_control(
                        connection.peer_id,
                        FrameType.POLL,
                        connection.session_id,
                        sequence=poll_sequence,
                    )
                    completed = connection.poll_done_event.wait(
                        self.cfg.bridge.control_timeout_seconds
                    )
                    if completed:
                        # POLL_DONE is transmitted twice. Leave enough guard
                        # time for the redundant control copy to clear the air
                        # before granting the next peer.
                        time.sleep(max(0.15, self.cfg.bridge.frame_interval_ms / 1000.0))
                except Exception as exc:
                    self._connection_error(connection, exc)
            time.sleep(delay)

    def _serve_client_poll(self, connection: BridgeConnection, poll_sequence: int) -> None:
        if not connection.poll_lock.acquire(blocking=False):
            return
        try:
            if poll_sequence <= connection.last_poll_sequence:
                sent = connection.last_poll_bytes
            else:
                sent = 0
                if not connection.closed.is_set() and connection.stream.pending_bytes:
                    sent = connection.stream.send_window()
                connection.last_poll_sequence = poll_sequence
                connection.last_poll_bytes = sent
            flags = FLAG_EMPTY if sent == 0 else 0
            payload = sent.to_bytes(4, "big")
            self._send_control(
                connection.peer_id,
                FrameType.POLL_DONE,
                connection.session_id,
                flags=flags,
                payload=payload,
                repeat=2,
                sequence=poll_sequence,
            )
        except Exception as exc:
            self._connection_error(connection, exc)
        finally:
            connection.poll_lock.release()

    def _make_connection(
        self,
        peer_id: str,
        session_id: int,
        local_socket: socket.socket,
    ) -> BridgeConnection:
        return BridgeConnection(
            peer_id=peer_id,
            session_id=session_id,
            local_socket=local_socket,
            cfg=self.cfg,
            send_frame=self._send_frame,
            metrics=self.metrics,
            on_local_eof=self._on_local_eof,
            on_error=self._connection_error,
        )

    def _on_local_eof(self, connection: BridgeConnection) -> None:
        def flush_and_close_write() -> None:
            deadline = time.monotonic() + max(
                30.0,
                self.cfg.bridge.ack_timeout_seconds * (self.cfg.bridge.max_retries + 1),
            )
            while (
                self._running.is_set()
                and not connection.closed.is_set()
                and connection.stream.pending_bytes
                and time.monotonic() < deadline
            ):
                if self.is_central:
                    time.sleep(0.25)
                else:
                    time.sleep(0.5)
            if not connection.closed.is_set():
                try:
                    self._send_control(
                        connection.peer_id,
                        FrameType.HALF_CLOSE,
                        connection.session_id,
                    )
                    connection.local_half_close_sent.set()
                    self._complete_half_closed_connection(connection)
                except Exception as exc:
                    self._connection_error(connection, exc)

        threading.Thread(
            target=flush_and_close_write,
            name=f"bridge-half-close-{connection.session_id}",
            daemon=True,
        ).start()

    def _complete_half_closed_connection(self, connection: BridgeConnection) -> None:
        if not connection.begin_completed_close():
            return
        try:
            self._send_control(
                connection.peer_id,
                FrameType.CLOSE,
                connection.session_id,
                repeat=2,
            )
        except Exception as exc:
            logger.line(
                "bridge",
                f"Could not confirm close for session {connection.session_id}: {exc}",
            )
        finally:
            self._remove_connection(connection)

    def _connection_error(self, connection: BridgeConnection, exc: Exception) -> None:
        if connection.closed.is_set():
            return
        logger.line(
            "bridge",
            f"Session {connection.session_id} for {connection.peer_id} failed: {exc}",
        )
        try:
            self._send_control(
                connection.peer_id,
                FrameType.RESET,
                connection.session_id,
                payload=str(exc).encode("utf-8")[:64],
            )
        except Exception:
            pass
        self.metrics.sessions_reset += 1
        self._remove_connection(connection)

    def _replace_connection(self, connection: BridgeConnection) -> None:
        with self._connections_lock:
            old = self._connections.get(connection.peer_id)
            self._connections[connection.peer_id] = connection
        if old is not None and old is not connection:
            old.close()

    def _remove_connection(self, connection: BridgeConnection) -> None:
        with self._connections_lock:
            current = self._connections.get(connection.peer_id)
            if current is connection:
                del self._connections[connection.peer_id]
        connection.close()

    def _connection_for(self, peer_id: str, session_id: int) -> BridgeConnection | None:
        with self._connections_lock:
            connection = self._connections.get(peer_id)
        if connection is None or connection.session_id != session_id:
            return None
        return connection

    def _metrics_reporter(self) -> None:
        interval = self.cfg.bridge.metrics_interval_seconds
        while self._running.is_set():
            time.sleep(interval)
            if not self._running.is_set():
                return
            snapshot = self.metrics.snapshot()
            logger.line("bridge-metrics", json.dumps(snapshot, sort_keys=True))
            self._write_metrics(snapshot)

    def _prune_seen_sessions(self) -> None:
        cutoff = time.time() - 24 * 60 * 60
        stale = [key for key, seen_at in self._seen_sessions.items() if seen_at < cutoff]
        for key in stale:
            del self._seen_sessions[key]

    def _write_metrics(self, snapshot: dict[str, int | float]) -> None:
        path = self.cfg.path.with_suffix(".bridge-metrics.json")
        temporary = path.with_suffix(path.suffix + ".tmp")
        try:
            temporary.write_text(json.dumps(snapshot, indent=2, sort_keys=True), encoding="utf-8")
            temporary.replace(path)
        except OSError as exc:
            logger.line("bridge-metrics", f"Could not write {path}: {exc}")

    @staticmethod
    def _new_session_id() -> int:
        session_id = 0
        while session_id == 0:
            session_id = secrets.randbits(32)
        return session_id


def run_flower_bridge(cfg: MeshConfig) -> None:
    reconnects = 0
    while True:
        bridge = FlowerBridge(cfg)
        try:
            bridge.run_forever()
            return
        except KeyboardInterrupt:
            return
        except Exception as exc:
            reconnects += 1
            error = as_meshnet_error(exc, "bridge", attempts=reconnects)
            log_meshnet_error(error, "bridge")
            if not error.retryable or not cfg.runtime.runtime_reconnect or (
                cfg.runtime.max_reconnect_attempts > 0
                and reconnects >= cfg.runtime.max_reconnect_attempts
            ):
                raise error from exc
            time.sleep(cfg.runtime.reconnect_delay_seconds)
        finally:
            bridge.stop()
