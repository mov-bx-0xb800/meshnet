from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from .stream_protocol import (
    FLAG_ACK_REQUIRED,
    FrameType,
    StreamFrame,
)


@dataclass
class StreamMetrics:
    frames_sent: int = 0
    frames_received: int = 0
    data_bytes_sent: int = 0
    data_bytes_received: int = 0
    acknowledgements_sent: int = 0
    acknowledgements_received: int = 0
    retransmitted_frames: int = 0
    duplicate_frames: int = 0
    out_of_order_frames: int = 0
    invalid_frames: int = 0
    sessions_opened: int = 0
    sessions_reset: int = 0
    started_at: float = field(default_factory=time.time)

    def snapshot(self) -> dict[str, int | float]:
        return {
            "frames_sent": self.frames_sent,
            "frames_received": self.frames_received,
            "data_bytes_sent": self.data_bytes_sent,
            "data_bytes_received": self.data_bytes_received,
            "acknowledgements_sent": self.acknowledgements_sent,
            "acknowledgements_received": self.acknowledgements_received,
            "retransmitted_frames": self.retransmitted_frames,
            "duplicate_frames": self.duplicate_frames,
            "out_of_order_frames": self.out_of_order_frames,
            "invalid_frames": self.invalid_frames,
            "sessions_opened": self.sessions_opened,
            "sessions_reset": self.sessions_reset,
            "uptime_seconds": round(time.time() - self.started_at, 3),
        }


class ReliableStreamError(RuntimeError):
    pass


class ReliableStream:
    """Ordered byte stream with windowed SACK retransmission.

    The owner controls when ``send_window`` is called. That makes the same
    reliability implementation usable by the central scheduler and by a
    client only while it holds a central-issued poll/grant.
    """

    def __init__(
        self,
        *,
        peer_id: str,
        session_id: int,
        payload_bytes: int,
        window_size: int,
        ack_timeout_seconds: float,
        max_retries: int,
        max_buffer_bytes: int,
        send_frame: Callable[[str, StreamFrame], None],
        deliver: Callable[[bytes], None],
        metrics: StreamMetrics,
    ) -> None:
        self.peer_id = peer_id
        self.session_id = session_id
        self.payload_bytes = payload_bytes
        self.window_size = window_size
        self.ack_timeout_seconds = ack_timeout_seconds
        self.max_retries = max_retries
        self.max_buffer_bytes = max_buffer_bytes
        self._send_frame = send_frame
        self._deliver = deliver
        self.metrics = metrics

        self._tx_buffer = bytearray()
        self._pending: dict[int, bytes] = {}
        self._tx_next = 0
        self._remote_ack = 0
        self._remote_sack = 0

        self._rx_next = 0
        self._rx_pending: dict[int, bytes] = {}

        self._condition = threading.Condition(threading.RLock())
        self._send_lock = threading.Lock()
        self._closed = False

    @property
    def pending_bytes(self) -> int:
        with self._condition:
            return len(self._tx_buffer) + sum(len(item) for item in self._pending.values())

    @property
    def is_closed(self) -> bool:
        with self._condition:
            return self._closed

    def close(self) -> None:
        with self._condition:
            self._closed = True
            self._condition.notify_all()

    def queue_bytes(self, data: bytes) -> None:
        if not data:
            return
        if not isinstance(data, bytes):
            raise TypeError("stream data must be bytes")
        offset = 0
        with self._condition:
            while offset < len(data):
                while not self._closed and self.pending_bytes >= self.max_buffer_bytes:
                    self._condition.wait(timeout=0.5)
                if self._closed:
                    raise ReliableStreamError("stream closed while buffering data")
                free = self.max_buffer_bytes - self.pending_bytes
                chunk = data[offset : offset + max(1, free)]
                self._tx_buffer.extend(chunk)
                offset += len(chunk)
                self._condition.notify_all()

    def wait_for_data(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        with self._condition:
            while not self._closed and self.pending_bytes == 0:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(timeout=min(0.5, remaining))
            return self.pending_bytes > 0

    def send_window(self) -> int:
        with self._send_lock:
            with self._condition:
                if self._closed:
                    raise ReliableStreamError("cannot send on a closed stream")
                if not self._pending:
                    for _ in range(self.window_size):
                        if not self._tx_buffer:
                            break
                        payload = bytes(self._tx_buffer[: self.payload_bytes])
                        del self._tx_buffer[: len(payload)]
                        sequence = self._tx_next
                        self._tx_next += 1
                        if self._tx_next > 0xFFFFFFFF:
                            raise ReliableStreamError("stream sequence space exhausted")
                        self._pending[sequence] = payload
                if not self._pending:
                    return 0
                window_bytes = sum(len(item) for item in self._pending.values())

            for attempt in range(self.max_retries + 1):
                with self._condition:
                    missing = [
                        seq for seq in sorted(self._pending) if not self._is_acked(seq)
                    ]
                    if not missing:
                        self._pending.clear()
                        self._condition.notify_all()
                        return window_bytes
                for index, sequence in enumerate(missing):
                    flags = FLAG_ACK_REQUIRED if index == len(missing) - 1 else 0
                    payload = self._pending[sequence]
                    self._send_frame(
                        self.peer_id,
                        StreamFrame(
                            FrameType.DATA,
                            self.session_id,
                            sequence=sequence,
                            flags=flags,
                            payload=payload,
                        ),
                    )
                    self.metrics.frames_sent += 1
                    self.metrics.data_bytes_sent += len(payload)
                    if attempt:
                        self.metrics.retransmitted_frames += 1

                deadline = time.monotonic() + self.ack_timeout_seconds
                with self._condition:
                    while not all(self._is_acked(seq) for seq in self._pending):
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            break
                        self._condition.wait(timeout=min(0.25, remaining))
                    if all(self._is_acked(seq) for seq in self._pending):
                        self._pending.clear()
                        self._condition.notify_all()
                        return window_bytes

            raise ReliableStreamError(
                f"peer {self.peer_id} did not acknowledge session {self.session_id} "
                f"after {self.max_retries + 1} attempts"
            )

    def receive(self, frame: StreamFrame) -> None:
        if frame.session_id != self.session_id:
            return
        if frame.frame_type == FrameType.ACK:
            self._receive_ack(frame)
        elif frame.frame_type == FrameType.DATA:
            self._receive_data(frame)

    def _receive_ack(self, frame: StreamFrame) -> None:
        with self._condition:
            if frame.ack > self._remote_ack or (
                frame.ack == self._remote_ack and frame.sack != self._remote_sack
            ):
                self._remote_ack = frame.ack
                self._remote_sack = frame.sack
            self.metrics.frames_received += 1
            self.metrics.acknowledgements_received += 1
            self._condition.notify_all()

    def _receive_data(self, frame: StreamFrame) -> None:
        deliver_chunks: list[bytes] = []
        should_ack = bool(frame.flags & FLAG_ACK_REQUIRED)
        with self._condition:
            self.metrics.frames_received += 1
            if frame.sequence < self._rx_next:
                self.metrics.duplicate_frames += 1
            elif frame.sequence >= self._rx_next + 32:
                self.metrics.out_of_order_frames += 1
            elif frame.sequence in self._rx_pending:
                self.metrics.duplicate_frames += 1
            else:
                if frame.sequence != self._rx_next:
                    self.metrics.out_of_order_frames += 1
                self._rx_pending[frame.sequence] = frame.payload
                while self._rx_next in self._rx_pending:
                    payload = self._rx_pending.pop(self._rx_next)
                    deliver_chunks.append(payload)
                    self._rx_next += 1
                    self.metrics.data_bytes_received += len(payload)
            ack = self._rx_next
            sack = self._sack_bitmap()

        for payload in deliver_chunks:
            self._deliver(payload)
        if should_ack:
            self._send_frame(
                self.peer_id,
                StreamFrame(
                    FrameType.ACK,
                    self.session_id,
                    ack=ack,
                    sack=sack,
                ),
            )
            self.metrics.frames_sent += 1
            self.metrics.acknowledgements_sent += 1

    def _sack_bitmap(self) -> int:
        bitmap = 0
        for sequence in self._rx_pending:
            delta = sequence - self._rx_next
            if 0 <= delta < 32:
                bitmap |= 1 << delta
        return bitmap

    def _is_acked(self, sequence: int) -> bool:
        if sequence < self._remote_ack:
            return True
        delta = sequence - self._remote_ack
        return 0 <= delta < 32 and bool(self._remote_sack & (1 << delta))
