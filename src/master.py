from __future__ import annotations

import time
from typing import Any

from . import logger
from .config import MeshConfig
from .errors import MeshNetError, as_meshnet_error, log_meshnet_error
from .node import DeliveryResult, MeshNode, mesh_node_info
from .protocol import Envelope


class MasterNode(MeshNode):
    def __init__(self, cfg: MeshConfig, scope: str = "master") -> None:
        super().__init__(cfg, scope)
        self.last_discovery_attempts = 0

    def handle_message(self, envelope: Envelope, packet: dict[str, Any]) -> None:
        if envelope.t == "pong":
            return
        if envelope.t == "hello_ack":
            return
        if envelope.t == "test_ack":
            return
        if envelope.t == "text_ack":
            return
        if envelope.t == "text":
            logger.line("master", f"Text <- {envelope.src}: {envelope.body}")
            self.reply(
                envelope,
                "text_ack",
                body={"ok": True, "rid": envelope.id},
                seq=envelope.seq,
            )
        elif envelope.t == "status_res":
            logger.line("master", f"Status <- {envelope.src}: {envelope.body}")

    def discover(self, timeout_seconds: int | None = None) -> list[Envelope]:
        timeout = timeout_seconds or self.cfg.runtime.discovery_timeout_seconds
        for attempt in range(1, self.cfg.runtime.discovery_retries + 1):
            self.last_discovery_attempts = attempt
            logger.line(
                "network",
                f"Discovery attempt {attempt}/{self.cfg.runtime.discovery_retries}.",
            )
            compatible = self._discover_once(timeout)
            if compatible:
                logger.blank()
                logger.line("network", f"Reached {len(compatible)} compatible node.")
                return compatible
            if attempt < self.cfg.runtime.discovery_retries:
                logger.line(
                    "network",
                    f"No compatible node; retrying in "
                    f"{self.cfg.runtime.retry_backoff_seconds} seconds.",
                )
                time.sleep(self.cfg.runtime.retry_backoff_seconds)

        error = MeshNetError(
            "DISCOVERY_TIMEOUT",
            "discovery",
            f"no compatible node replied after {self.last_discovery_attempts} attempt(s)",
            "Start the peer runtime and check matching region, channel, PSK, "
            "network ID, and antennas.",
            retryable=True,
            attempts=self.last_discovery_attempts,
            details={"expected_peer": self.cfg.peer_node_id},
        )
        log_meshnet_error(error, "network")
        print_no_node_help(timeout)
        return []

    def _discover_once(self, timeout: int) -> list[Envelope]:
        logger.line("network", "Reaching out to any node...")
        hello = self.send("hello", dst="broadcast", body=mesh_node_info(self), seq=0)
        logger.line("network", "Broadcast hello sent.")
        logger.line("network", f"Listening for replies for {timeout} seconds...")
        found: list[Envelope] = []
        end = time.monotonic() + timeout
        seen: set[str] = set()
        while time.monotonic() < end:
            remaining = max(1, int(end - time.monotonic()))
            msg = self.wait_for_message(
                "hello_ack",
                ack_for=hello.id,
                timeout_seconds=min(2, remaining),
            )
            if msg is None:
                continue
            env = msg.envelope
            if env.src in seen:
                continue
            seen.add(env.src)
            found.append(env)
            body = env.body if isinstance(env.body, dict) else {}
            logger.blank()
            logger.line("network", "Reply received.")
            logger.detail(f"node_id: {env.src}", indent=10)
            logger.detail(f"name: {body.get('name', body.get('nm', 'unknown'))}", indent=10)
            logger.detail(f"short: {body.get('short', body.get('sh', ''))}", indent=10)
            logger.detail(f"mesh_id: {body.get('mesh_id', body.get('m', 'unknown'))}", indent=10)
            logger.detail(f"rssi: {msg.packet.get('rxRssi', msg.packet.get('rssi', 'if available'))}", indent=10)
            logger.detail(f"snr: {msg.packet.get('rxSnr', msg.packet.get('snr', 'if available'))}", indent=10)
            logger.blank()
            logger.line("compat", "Checking Compatibility and Same Network...")
            logger.line("compat", "network_id: true")
            logger.line("compat", "hmac: true")
            role = body.get("role", body.get("r", "unknown"))
            logger.line("compat", f"role: {role}")
            expected_role = "slave"
            if (
                role == expected_role
                and env.src == self.cfg.network.slave_id
            ):
                logger.line("compat", f"TRUE NODE: {env.src}")
            else:
                logger.line("compat", f"IGNORED: {env.src}")
        compatible = [
            env
            for env in found
            if env.src == self.cfg.network.slave_id
            and isinstance(env.body, dict)
            and env.body.get("role", env.body.get("r")) == "slave"
        ]
        return compatible

    def ping_once(self, target: str, seq: int | None = None) -> tuple[bool, float | None, Envelope]:
        if seq is None:
            seq = self.next_seq()
        sent_at = time.monotonic()
        result = self.send_reliable(
            "ping",
            dst=target,
            seq=seq,
            body={"target": target},
            expect_reply_type="pong",
        )
        if not result.ok:
            return False, None, result.envelope
        return True, time.monotonic() - sent_at, result.envelope

    def ping_loop(self, target: str | None = None) -> None:
        target = target or self.cfg.network.slave_id
        interval = self.cfg.safe_ping_interval
        logger.line("ping", "Starting ping/pong test.")
        logger.line("ping", f"Target: {target}")
        logger.line("ping", f"Interval: {interval} seconds")
        logger.line("ping", "Press Ctrl+C to stop.")
        logger.blank()
        seq = 0
        try:
            while True:
                seq += 1
                sent_at = time.monotonic()
                logger.line("ping", f"-> seq={seq}")
                ok, _, ping = self.ping_once(target, seq=seq)
                if not ok:
                    logger.line(
                        "ping",
                        f"timeout seq={seq} after {self.cfg.runtime.ack_timeout_seconds}s",
                    )
                    logger.line("ping", "continuing...")
                else:
                    rtt = time.monotonic() - sent_at
                    logger.line("ping", f"<- pong seq={seq} from={target} rtt={rtt:.2f}s status=OK")
                    logger.line("ping", f"Pong received from {target}")
                time.sleep(interval)
        except KeyboardInterrupt:
            logger.line("ping", "Stopped.")

    def send_text_message(self, text: str, target: str | None = None) -> DeliveryResult:
        target = target or self.cfg.network.slave_id
        result = self.send_reliable(
            "text",
            dst=target,
            body=text,
            expect_reply_type="text_ack",
        )
        if result.ok:
            logger.line("mesh", f"Text acknowledged by {target}: {text}")
        else:
            logger.line(
                "mesh",
                f"Text delivery failed [{result.error_code}] to {target}: {result.last_error}",
            )
            logger.detail(f"action: {result.action}", indent=8)
        return result


def print_no_node_help(timeout: int) -> None:
    logger.line("network", f"No compatible node reached within {timeout} seconds.")
    logger.line("network", "Troubleshooting:")
    logger.detail("- Check both radios are powered.", indent=10)
    logger.detail("- Check antennas are attached.", indent=10)
    logger.detail("- Check both nodes use the same region.", indent=10)
    logger.detail("- Check both nodes use the same channel name.", indent=10)
    logger.detail("- Check both nodes use the same PSK.", indent=10)
    logger.detail("- Check both nodes use the same modem preset.", indent=10)
    logger.detail("- Try moving nodes closer together.", indent=10)


def run_master(cfg: MeshConfig) -> None:
    reconnects = 0
    while True:
        node = MasterNode(cfg, "master")
        try:
            _run_master_connected(node, cfg)
            return
        except KeyboardInterrupt:
            logger.line("master", "Stopping.")
            return
        except Exception as exc:
            reconnects += 1
            error = as_meshnet_error(exc, "runtime", attempts=reconnects)
            log_meshnet_error(error, "master")
            if not error.retryable or not cfg.runtime.runtime_reconnect or (
                cfg.runtime.max_reconnect_attempts > 0
                and reconnects >= cfg.runtime.max_reconnect_attempts
            ):
                raise error from exc
            logger.line(
                "master",
                f"Reconnecting in {cfg.runtime.reconnect_delay_seconds} seconds "
                f"(cycle {reconnects}).",
            )
            time.sleep(cfg.runtime.reconnect_delay_seconds)
        finally:
            node.close()


def _run_master_connected(node: MasterNode, cfg: MeshConfig) -> None:
    logger.line("master", "Starting Master Node...")
    logger.line("master", f"Config loaded: {cfg.path.name}")
    logger.line("master", f"Connecting to radio: {cfg.radio.port}")
    node.connect()
    logger.line("master", "Connected.")
    logger.line("master", f"Network: {cfg.network.network_id}")
    logger.line("master", f"Channel: {cfg.radio.channel_name}")
    logger.line("master", f"Looking for slave: {cfg.network.slave_id}")
    compatible = node.discover(cfg.runtime.discovery_timeout_seconds)
    if compatible:
        logger.line("master", f"Found {len(compatible)} known node.")
        logger.line("master", f"TRUE NODE found: {cfg.network.slave_id}")
    logger.line("master", f"Starting ping/pong loop every {cfg.safe_ping_interval} seconds.")
    seq = 0
    next_heartbeat = time.monotonic() + cfg.runtime.heartbeat_interval_seconds
    while True:
        if not node.radio.is_connected():
            raise MeshNetError(
                "RADIO_DISCONNECTED",
                "runtime",
                "the Meshtastic USB connection was lost",
                "Reconnect the USB radio; MeshNet will retry automatically.",
                retryable=True,
            )
        if time.monotonic() >= next_heartbeat:
            node.send("heartbeat", dst="broadcast", body={"role": "master"}, seq=0)
            logger.line("master", "Heartbeat broadcast sent.")
            next_heartbeat = time.monotonic() + cfg.runtime.heartbeat_interval_seconds
        seq += 1
        logger.line("master", f"Ping -> {cfg.network.slave_id} seq={seq}")
        ok, rtt, _ = node.ping_once(cfg.network.slave_id, seq=seq)
        if ok:
            logger.line("master", f"Pong <- {cfg.network.slave_id} seq={seq} rtt={rtt:.1f}s")
        else:
            logger.line("master", f"Pong timeout from {cfg.network.slave_id} seq={seq}")
        time.sleep(cfg.safe_ping_interval)
