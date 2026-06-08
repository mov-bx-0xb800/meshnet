from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any

from . import logger
from .config import MeshConfig
from .protocol import Envelope, decode_envelope, encode_envelope, make_message, verify_envelope
from .radio import BROADCAST_ADDR, RadioClient


@dataclass(frozen=True)
class AcceptedMessage:
    envelope: Envelope
    packet: dict[str, Any]


class MeshNode:
    def __init__(self, cfg: MeshConfig, scope: str | None = None) -> None:
        self.cfg = cfg
        self.scope = scope or cfg.app.role
        self.radio = RadioClient(cfg, self.scope)
        self._accepted: list[AcceptedMessage] = []
        self._condition = threading.Condition()
        self._seq = 0
        self._running = False

    def connect(self) -> None:
        self.radio.add_handler(self._on_radio_text)
        self.radio.connect(no_nodes=False)

    def close(self) -> None:
        self._running = False
        self.radio.close()

    def next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def send(
        self,
        message_type: str,
        dst: str = "broadcast",
        body: Any = "",
        seq: int | None = None,
        want_ack: bool = False,
    ) -> Envelope:
        if seq is None:
            seq = self.next_seq()
        envelope = make_message(self.cfg, message_type, dst=dst, seq=seq, body=body)
        destination = BROADCAST_ADDR
        self.radio.send_text(encode_envelope(envelope), destination_id=destination, want_ack=want_ack)
        return envelope

    def _on_radio_text(self, text: str, packet: dict[str, Any]) -> None:
        try:
            envelope = decode_envelope(text)
        except Exception:
            return
        if envelope.src == self.cfg.app.node_id:
            return
        if envelope.n != self.cfg.network.network_id:
            logger.line("security", "Message rejected.")
            logger.line("security", "Reason: wrong network_id.")
            logger.line("security", f"Expected: {self.cfg.network.network_id}")
            logger.line("security", f"Got: {envelope.n}")
            logger.line("security", "Action: ignored.")
            return
        ok, reason = verify_envelope(envelope, self.cfg)
        if not ok:
            logger.line("security", "Message rejected.")
            logger.line("security", f"Reason: {reason}.")
            logger.line("security", f"From: {envelope.src or 'unknown'}")
            logger.line("security", "Action: ignored.")
            return
        if envelope.dst not in {self.cfg.app.node_id, "broadcast"}:
            return
        if envelope.dst == "broadcast" and not self.cfg.network.allow_broadcast:
            return
        with self._condition:
            self._accepted.append(AcceptedMessage(envelope=envelope, packet=packet))
            self._condition.notify_all()
        self.handle_message(envelope, packet)

    def handle_message(self, envelope: Envelope, packet: dict[str, Any]) -> None:
        pass

    def wait_for_message(
        self,
        message_type: str | None = None,
        src: str | None = None,
        message_id: str | None = None,
        seq: int | None = None,
        timeout_seconds: int = 30,
    ) -> AcceptedMessage | None:
        deadline = time.monotonic() + timeout_seconds
        with self._condition:
            while True:
                for index, item in enumerate(self._accepted):
                    env = item.envelope
                    if message_type is not None and env.t != message_type:
                        continue
                    if src is not None and env.src != src:
                        continue
                    if message_id is not None and env.id != message_id:
                        continue
                    if seq is not None and env.seq != seq:
                        continue
                    return self._accepted.pop(index)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._condition.wait(timeout=min(0.5, remaining))

    def run_forever(self) -> None:
        self._running = True
        try:
            while self._running:
                time.sleep(1)
        except KeyboardInterrupt:
            logger.line(self.scope, "Stopping.")
        finally:
            self.close()


def mesh_node_info(node: MeshNode) -> dict[str, Any]:
    return {
        "r": node.cfg.app.role,
        "nm": node.cfg.app.node_name,
        "sh": node.cfg.app.node_short_name,
        "m": node.radio.local_mesh_id(),
    }
