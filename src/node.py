from __future__ import annotations

import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

from . import logger
from .config import MeshConfig
from .errors import MeshNetError, as_meshnet_error, log_meshnet_error
from .protocol import Envelope, decode_envelope, encode_envelope, make_message, verify_envelope
from .radio import BROADCAST_ADDR, RadioClient, packet_from_mesh_id, packet_to_mesh_id
from .state import SEEN_TTL_SECONDS, StateStore, config_fingerprint


MAX_ACCEPTED_MESSAGES = 200
MAX_RESPONSE_CACHE_ENTRIES = 4096


@dataclass(frozen=True)
class AcceptedMessage:
    envelope: Envelope
    packet: dict[str, Any]


@dataclass(frozen=True)
class DeliveryResult:
    ok: bool
    envelope: Envelope
    reply: AcceptedMessage | None
    attempts: int
    status: str
    last_error: str = ""
    error_code: str = ""
    action: str = ""
    retryable: bool = False


class MeshNode:
    def __init__(self, cfg: MeshConfig, scope: str | None = None) -> None:
        self.cfg = cfg
        self.scope = scope or cfg.app.role
        self.radio = RadioClient(cfg, self.scope)
        self.state = StateStore.for_config(cfg)
        self._accepted: list[AcceptedMessage] = []
        self._condition = threading.Condition()
        self._seq = 0
        self._running = False
        self._response_cache: OrderedDict[
            tuple[str, str], tuple[float, Envelope]
        ] = OrderedDict()
        self._response_cache_lock = threading.Lock()
        self._last_transport_error = ""
        self._last_transport_code = ""
        self._last_transport_action = ""

    def connect(self) -> None:
        self.radio.add_handler(self._on_radio_text)
        last_error: MeshNetError | None = None
        max_attempts = self.cfg.runtime.connect_retries
        for attempt in range(1, max_attempts + 1):
            try:
                logger.line(self.scope, f"Connection attempt {attempt}/{max_attempts}.")
                self.radio.connect(no_nodes=False)
                return
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                self.radio.close()
                error = as_meshnet_error(exc, "connect", attempts=attempt)
                last_error = error
                log_meshnet_error(error, self.scope)
                if not error.retryable or attempt >= max_attempts:
                    raise error from exc
                logger.line(
                    self.scope,
                    f"Retrying connection in {self.cfg.runtime.retry_backoff_seconds} seconds.",
                )
                time.sleep(self.cfg.runtime.retry_backoff_seconds)
        if last_error is not None:
            raise last_error

    def close(self) -> None:
        self._running = False
        self.radio.close()
        self.state.close()

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
        wait_radio_ack: bool = False,
        message_id: str | None = None,
        attempt: int = 1,
        ack_for: str = "",
    ) -> Envelope:
        self._last_transport_error = ""
        self._last_transport_code = ""
        self._last_transport_action = ""
        if seq is None:
            seq = self.next_seq()
        envelope = make_message(
            self.cfg,
            message_type,
            dst=dst,
            seq=seq,
            body=body,
            message_id=message_id,
            attempt=attempt,
            ack_for=ack_for,
        )
        destination = self.radio_destination_for(dst)
        effective_want_ack = want_ack and destination != BROADCAST_ADDR
        sent = self.radio.send_text(
            encode_envelope(envelope),
            destination_id=destination,
            want_ack=effective_want_ack,
            track_ack=effective_want_ack,
        )
        self.state.record_outbound(
            message_id=envelope.id,
            message_type=envelope.t,
            src=envelope.src,
            dst=envelope.dst,
            ack_for=envelope.ack_for,
            seq=envelope.seq,
            status="sent",
            attempts=attempt,
            radio_dest=sent.destination_id,
            radio_packet_id=sent.packet_id,
            body=envelope.body,
        )
        if wait_radio_ack and effective_want_ack and sent.ack_event is not None:
            if sent.ack_event.wait(timeout=self.cfg.runtime.radio_ack_timeout_seconds):
                error = sent.ack.error if sent.ack is not None else ""
                self.state.record_outbound(
                    message_id=envelope.id,
                    message_type=envelope.t,
                    src=envelope.src,
                    dst=envelope.dst,
                    ack_for=envelope.ack_for,
                    seq=envelope.seq,
                    status="radio_nak" if error else "radio_ack",
                    attempts=attempt,
                    radio_dest=sent.destination_id,
                    radio_packet_id=sent.packet_id,
                    last_error=error,
                    body=envelope.body,
                )
                if error:
                    self._last_transport_error = f"radio rejected the packet: {error}"
                    self._last_transport_code = "RADIO_NAK"
                    self._last_transport_action = (
                        "Check destination reachability, channel settings, "
                        "hop limit, and radio logs."
                    )
                    logger.line(self.scope, f"Radio NAK from {dst}: {error}")
            else:
                self._last_transport_error = "radio acknowledgement timed out"
                self._last_transport_code = "RADIO_ACK_TIMEOUT"
                self._last_transport_action = (
                    "Check antennas, range, channel settings, and whether "
                    "the destination radio is online."
                )
                self.state.record_outbound(
                    message_id=envelope.id,
                    message_type=envelope.t,
                    src=envelope.src,
                    dst=envelope.dst,
                    ack_for=envelope.ack_for,
                    seq=envelope.seq,
                    status="radio_ack_timeout",
                    attempts=attempt,
                    radio_dest=sent.destination_id,
                    radio_packet_id=sent.packet_id,
                    last_error="radio ack timeout",
                    body=envelope.body,
                )
        return envelope

    def reply(
        self,
        request: Envelope,
        message_type: str,
        body: Any = "",
        *,
        seq: int | None = None,
        want_ack: bool = True,
    ) -> Envelope:
        response = self.send(
            message_type,
            dst=request.src,
            body=body,
            seq=request.seq if seq is None else seq,
            want_ack=want_ack,
            ack_for=request.id,
        )
        self._cache_response(request.src, request.id, response)
        return response

    def send_reliable(
        self,
        message_type: str,
        dst: str,
        body: Any = "",
        *,
        expect_reply_type: str | None = None,
        seq: int | None = None,
    ) -> DeliveryResult:
        if seq is None:
            seq = self.next_seq()
        message_id: str | None = None
        last_envelope: Envelope | None = None
        last_error = ""
        last_error_code = ""
        last_action = ""
        for attempt in range(1, self.cfg.runtime.send_retries + 1):
            try:
                envelope = self.send(
                    message_type,
                    dst=dst,
                    body=body,
                    seq=seq,
                    want_ack=True,
                    wait_radio_ack=True,
                    message_id=message_id,
                    attempt=attempt,
                )
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                error = as_meshnet_error(exc, "delivery", attempts=attempt)
                last_error = error.message
                last_error_code = error.code
                last_action = error.action
                log_meshnet_error(error, self.scope)
                if not error.retryable or attempt >= self.cfg.runtime.send_retries:
                    raise error from exc
                logger.line(
                    self.scope,
                    f"Retrying {message_type} -> {dst} in "
                    f"{self.cfg.runtime.retry_backoff_seconds} seconds.",
                )
                time.sleep(self.cfg.runtime.retry_backoff_seconds)
                continue
            message_id = envelope.id
            last_envelope = envelope
            if expect_reply_type is None:
                return DeliveryResult(True, envelope, None, attempt, "sent")
            reply = self.wait_for_message(
                expect_reply_type,
                src=dst,
                ack_for=envelope.id,
                timeout_seconds=self.cfg.runtime.ack_timeout_seconds,
            )
            if reply is not None:
                self.state.record_outbound(
                    message_id=envelope.id,
                    message_type=envelope.t,
                    src=envelope.src,
                    dst=envelope.dst,
                    ack_for=envelope.ack_for,
                    seq=envelope.seq,
                    status="app_ack",
                    attempts=attempt,
                    radio_dest=self.radio_destination_for(dst),
                    body=envelope.body,
                )
                return DeliveryResult(True, envelope, reply, attempt, "app_ack")
            if self._last_transport_error:
                last_error = (
                    f"{self._last_transport_error}; {expect_reply_type} was not received"
                )
                last_error_code = self._last_transport_code
                last_action = self._last_transport_action
            else:
                last_error = f"{expect_reply_type} timeout"
                last_error_code = "APP_ACK_TIMEOUT"
                last_action = (
                    "Confirm the receiving MeshNet runtime is running and both configs match."
                )
            self.state.record_outbound(
                message_id=envelope.id,
                message_type=envelope.t,
                src=envelope.src,
                dst=envelope.dst,
                ack_for=envelope.ack_for,
                seq=envelope.seq,
                status="app_ack_timeout",
                attempts=attempt,
                radio_dest=self.radio_destination_for(dst),
                last_error=last_error,
                body=envelope.body,
            )
            if attempt < self.cfg.runtime.send_retries:
                logger.line(
                    self.scope,
                    f"Retrying {message_type} -> {dst} after {last_error} "
                    f"({attempt}/{self.cfg.runtime.send_retries})",
                )
                time.sleep(self.cfg.runtime.retry_backoff_seconds)
        if last_envelope is None:
            raise RuntimeError("send_reliable did not send")
        return DeliveryResult(
            False,
            last_envelope,
            None,
            self.cfg.runtime.send_retries,
            "failed",
            last_error,
            last_error_code,
            last_action,
            True,
        )

    def radio_destination_for(self, dst: str) -> str:
        if dst == "broadcast":
            return BROADCAST_ADDR
        if dst.startswith("!"):
            return dst
        configured_mesh_id = self.cfg.mesh_id_for(dst)
        if configured_mesh_id and configured_mesh_id != "unknown":
            return configured_mesh_id
        mesh_id = self.state.get_mesh_id(dst)
        if mesh_id and mesh_id != "unknown":
            return mesh_id
        logger.line(self.scope, f"No trusted mesh ID for {dst}; using broadcast until discovered.")
        return BROADCAST_ADDR

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
        if not self._remember_sender(envelope, packet):
            return
        if envelope.dst not in {self.cfg.app.node_id, "broadcast"}:
            return
        if envelope.dst == "broadcast" and not self.cfg.network.allow_broadcast:
            return
        local_mesh_id = self.radio.local_mesh_id()
        packet_to = packet_to_mesh_id(packet)
        if envelope.dst == self.cfg.app.node_id and packet_to not in {
            "",
            local_mesh_id,
            BROADCAST_ADDR,
            "!ffffffff",
        }:
            logger.line("security", "Message rejected.")
            logger.line("security", f"Reason: radio destination mismatch ({packet_to}).")
            logger.line("security", "Action: ignored.")
            return
        if not self.state.mark_seen(envelope.src, envelope.id, envelope.t):
            self._resend_cached_response(envelope)
            return
        with self._condition:
            self._accepted.append(AcceptedMessage(envelope=envelope, packet=packet))
            if len(self._accepted) > MAX_ACCEPTED_MESSAGES:
                del self._accepted[: len(self._accepted) - MAX_ACCEPTED_MESSAGES]
            self._condition.notify_all()
        self.handle_message(envelope, packet)

    def _remember_sender(self, envelope: Envelope, packet: dict[str, Any]) -> bool:
        body = envelope.body if isinstance(envelope.body, dict) else {}
        mesh_id = str(body.get("m") or body.get("mesh_id") or packet_from_mesh_id(packet) or "")
        if not mesh_id or mesh_id == "unknown":
            return True
        status = self.state.upsert_node(
            app_id=envelope.src,
            mesh_id=mesh_id,
            role=str(body.get("r") or body.get("role") or ""),
            name=str(body.get("nm") or body.get("name") or ""),
            short_name=str(body.get("sh") or body.get("short") or ""),
            config_fingerprint=str(body.get("fp") or ""),
            rssi=packet.get("rxRssi", packet.get("rssi")),
            snr=packet.get("rxSnr", packet.get("snr")),
            raw={"body": body, "packet_from": packet_from_mesh_id(packet)},
        )
        if status == "identity_changed":
            logger.line("security", f"Node identity changed for {envelope.src}.")
            logger.line("security", f"New mesh ID seen: {mesh_id}")
            logger.line("security", "Action: ignored until trusted.")
            return False
        remote_fp = str(body.get("fp") or "")
        if remote_fp and remote_fp != config_fingerprint(self.cfg):
            logger.line("compat", f"Config fingerprint mismatch from {envelope.src}: {remote_fp}")
        return True

    def _resend_cached_response(self, envelope: Envelope) -> None:
        cached = self._cached_response(envelope.src, envelope.id)
        if cached is None:
            return
        logger.line(self.scope, f"Duplicate {envelope.t} from {envelope.src}; resending cached {cached.t}.")
        self.send(
            cached.t,
            dst=cached.dst,
            body=cached.body,
            seq=cached.seq,
            want_ack=True,
            message_id=cached.id,
            ack_for=cached.ack_for,
        )

    def _cache_response(self, src: str, request_id: str, response: Envelope) -> None:
        now = time.monotonic()
        key = (src, request_id)
        with self._response_cache_lock:
            self._prune_response_cache_locked(now)
            self._response_cache[key] = (now, response)
            self._response_cache.move_to_end(key)
            while len(self._response_cache) > MAX_RESPONSE_CACHE_ENTRIES:
                self._response_cache.popitem(last=False)

    def _cached_response(self, src: str, request_id: str) -> Envelope | None:
        now = time.monotonic()
        key = (src, request_id)
        with self._response_cache_lock:
            self._prune_response_cache_locked(now)
            cached = self._response_cache.get(key)
            if cached is None:
                return None
            return cached[1]

    def _prune_response_cache_locked(self, now: float) -> None:
        cutoff = now - SEEN_TTL_SECONDS
        while self._response_cache:
            _key, (created_at, _response) = next(iter(self._response_cache.items()))
            if created_at >= cutoff:
                return
            self._response_cache.popitem(last=False)

    def handle_message(self, envelope: Envelope, packet: dict[str, Any]) -> None:
        pass

    def wait_for_message(
        self,
        message_type: str | None = None,
        src: str | None = None,
        message_id: str | None = None,
        seq: int | None = None,
        ack_for: str | None = None,
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
                    if ack_for is not None and env.ack_for != ack_for:
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
                if self.radio.wait_for_disconnect(1):
                    raise MeshNetError(
                        "RADIO_DISCONNECTED",
                        "runtime",
                        "the Meshtastic USB connection was lost",
                        "Reconnect the USB radio; MeshNet will retry automatically.",
                        retryable=True,
                    )
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
        "fp": config_fingerprint(node.cfg),
    }
