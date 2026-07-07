from __future__ import annotations

import glob
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import base64
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import logger
from .config import MeshConfig, channel_psk_description, channel_psk_for_cli
from .errors import MeshNetError, as_meshnet_error, log_meshnet_error


BROADCAST_ADDR = "^all"
SERIAL_PATTERNS = ("/dev/ttyACM*", "/dev/ttyUSB*")
MAX_MESHTASTIC_DATA_BYTES = 233


@dataclass(frozen=True)
class KnownMeshNode:
    mesh_id: str
    long_name: str
    short_name: str
    snr: Any = None
    rssi: Any = None
    last_heard: Any = None


@dataclass
class RadioAck:
    received: bool = False
    error: str = ""
    packet: dict[str, Any] | None = None


@dataclass
class SentPacket:
    packet: Any
    packet_id: str
    destination_id: str
    ack: RadioAck | None = None
    ack_event: threading.Event | None = None


def detect_serial_ports() -> list[str]:
    ports: list[str] = []
    for pattern in SERIAL_PATTERNS:
        ports.extend(glob.glob(pattern))
    return sorted(set(ports))


def resolve_port(cfg: MeshConfig, scope: str = "radio", verbose: bool = True) -> str:
    if cfg.radio.port and cfg.radio.port != "auto":
        if verbose:
            logger.line(scope, f"Using configured serial port: {cfg.radio.port}")
        return cfg.radio.port

    if verbose:
        logger.line(scope, "Searching for RAK/Meshtastic serial device...")
    ports = detect_serial_ports()
    if not ports:
        if verbose:
            logger.line(scope, "No serial radio found.")
            logger.line(scope, "Checked:")
            logger.detail("- /dev/ttyACM*", indent=8)
            logger.detail("- /dev/ttyUSB*", indent=8)
            logger.line(scope, "Troubleshooting:")
            logger.detail("- Use a USB data cable, not charge-only.", indent=8)
            logger.detail("- Check RAK is powered.", indent=8)
            logger.detail("- Check user is in dialout group.", indent=8)
            logger.detail("- Try unplugging and plugging the RAK back in.", indent=8)
        raise RuntimeError("no serial radio found")
    if len(ports) > 1 and verbose:
        logger.line(scope, f"Multiple serial devices found; using first: {ports[0]}")
        for port in ports:
            logger.detail(f"- {port}", indent=8)
    elif verbose:
        logger.line(scope, f"Found device: {ports[0]}")
    return ports[0]


class RadioClient:
    def __init__(self, cfg: MeshConfig, scope: str = "radio") -> None:
        self.cfg = cfg
        self.scope = scope
        self.port: str | None = None
        self.interface: Any = None
        self._queue: "queue.Queue[tuple[str, dict[str, Any]]]" = queue.Queue()
        self._handlers: list[Callable[[str, dict[str, Any]], None]] = []
        self._binary_handlers: list[Callable[[bytes, dict[str, Any]], None]] = []
        self._subscribed = False
        self._connection_subscribed = False
        self._connection_lost = threading.Event()
        self._lock_file: Any = None

    def connect(self, no_nodes: bool = False, timeout: int = 60) -> None:
        if self.port is None:
            self.port = resolve_port(self.cfg, self.scope, verbose=True)
        self._acquire_port_lock(self.port)
        logger.line(self.scope, "Connecting to local Meshtastic radio...")
        try:
            import meshtastic.serial_interface
            from pubsub import pub
        except Exception as exc:
            self._release_port_lock()
            raise RuntimeError(
                "Meshtastic Python package is not installed. Run ./install.sh first."
            ) from exc

        try:
            self.interface = meshtastic.serial_interface.SerialInterface(
                devPath=self.port,
                noNodes=no_nodes,
                timeout=timeout,
            )
        except Exception:
            self._release_port_lock()
            raise
        self._connection_lost.clear()
        if not self._subscribed:
            pub.subscribe(self._on_receive, "meshtastic.receive")
            self._subscribed = True
        if not self._connection_subscribed:
            pub.subscribe(self._on_connection_lost, "meshtastic.connection.lost")
            pub.subscribe(self._on_connection_established, "meshtastic.connection.established")
            self._connection_subscribed = True
        logger.line(self.scope, "Connected.")

    def close(self) -> None:
        if self.interface is not None:
            try:
                self.interface.close()
            except Exception:
                pass
        self.interface = None
        if self._subscribed:
            try:
                from pubsub import pub

                pub.unsubscribe(self._on_receive, "meshtastic.receive")
            except Exception:
                pass
            self._subscribed = False
        if self._connection_subscribed:
            try:
                from pubsub import pub

                pub.unsubscribe(self._on_connection_lost, "meshtastic.connection.lost")
                pub.unsubscribe(
                    self._on_connection_established,
                    "meshtastic.connection.established",
                )
            except Exception:
                pass
            self._connection_subscribed = False
        self._connection_lost.set()
        self._release_port_lock()

    def add_handler(self, handler: Callable[[str, dict[str, Any]], None]) -> None:
        if handler not in self._handlers:
            self._handlers.append(handler)

    def add_binary_handler(self, handler: Callable[[bytes, dict[str, Any]], None]) -> None:
        if handler not in self._binary_handlers:
            self._binary_handlers.append(handler)

    def _on_connection_lost(self, interface: Any = None, **_: Any) -> None:
        if interface is self.interface:
            self._connection_lost.set()
            logger.line(self.scope, "Radio connection lost.")

    def _on_connection_established(self, interface: Any = None, **_: Any) -> None:
        if interface is self.interface:
            self._connection_lost.clear()

    def is_connected(self) -> bool:
        if self.interface is None or self._connection_lost.is_set():
            return False
        connected = getattr(self.interface, "isConnected", None)
        if hasattr(connected, "is_set"):
            return bool(connected.is_set())
        return True

    def wait_for_disconnect(self, timeout_seconds: float) -> bool:
        return self._connection_lost.wait(timeout=max(0.0, timeout_seconds))

    def _acquire_port_lock(self, port: str) -> None:
        if self._lock_file is not None:
            return
        try:
            import fcntl
        except Exception:
            return
        safe = port.strip("/").replace("/", "_") or "auto"
        path = Path(tempfile.gettempdir()) / f"meshnet-{safe}.lock"
        fh = path.open("w", encoding="utf-8")
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            fh.close()
            raise RuntimeError(f"serial port is already in use by another MeshNet process: {port}") from exc
        fh.write(f"{os.getpid()}\n")
        fh.flush()
        self._lock_file = fh

    def _release_port_lock(self) -> None:
        if self._lock_file is None:
            return
        try:
            import fcntl

            fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        try:
            self._lock_file.close()
        except Exception:
            pass
        self._lock_file = None

    def _on_receive(self, packet: dict[str, Any] | None = None, interface: Any = None, **_: Any) -> None:
        if not packet:
            return
        payload = extract_payload(packet)
        if payload is not None:
            for handler in list(self._binary_handlers):
                try:
                    handler(payload, packet)
                except Exception as exc:
                    logger.exception_line(self.scope, "binary handler failed", exc)
        text = extract_text(packet)
        if text is None:
            return
        self._queue.put((text, packet))
        for handler in list(self._handlers):
            try:
                handler(text, packet)
            except Exception as exc:
                logger.exception_line(self.scope, "handler failed", exc)

    def send_text(
        self,
        text: str,
        destination_id: str = BROADCAST_ADDR,
        want_ack: bool = False,
        track_ack: bool = False,
    ) -> SentPacket:
        return self.send_bytes(
            text.encode("utf-8"),
            destination_id=destination_id,
            want_ack=want_ack,
            track_ack=track_ack,
        )

    def send_bytes(
        self,
        payload: bytes,
        destination_id: str = BROADCAST_ADDR,
        want_ack: bool = False,
        track_ack: bool = False,
        port_num: int | None = None,
    ) -> SentPacket:
        if not isinstance(payload, bytes):
            raise TypeError("payload must be bytes")
        if len(payload) > MAX_MESHTASTIC_DATA_BYTES:
            raise ValueError(
                f"binary payload is too large: {len(payload)} > {MAX_MESHTASTIC_DATA_BYTES}"
            )
        if not self.is_connected():
            raise MeshNetError(
                "RADIO_DISCONNECTED",
                "send",
                "the local Meshtastic radio is disconnected",
                "Check USB power/cable; the runtime will attempt to reconnect.",
                retryable=True,
            )
        ack = RadioAck() if track_ack else None
        ack_event = threading.Event() if track_ack else None

        def on_response(packet: dict[str, Any]) -> None:
            if ack is None or ack_event is None:
                return
            ack.packet = packet
            ack.received = True
            ack.error = extract_routing_error(packet)
            ack_event.set()

        if port_num is None:
            try:
                from meshtastic.protobuf import portnums_pb2

                port_num = int(portnums_pb2.PortNum.PRIVATE_APP)
            except Exception:
                port_num = 256

        packet = self.interface.sendData(
            payload,
            destinationId=destination_id,
            portNum=port_num,
            wantAck=want_ack,
            wantResponse=False,
            onResponse=on_response if track_ack else None,
            onResponseAckPermitted=track_ack,
            channelIndex=self.cfg.radio.channel_index,
            hopLimit=self.cfg.radio.hop_limit,
        )
        return SentPacket(
            packet=packet,
            packet_id=str(extract_packet_id(packet)),
            destination_id=destination_id,
            ack=ack,
            ack_event=ack_event,
        )

    def wait_for(
        self,
        predicate: Callable[[str, dict[str, Any]], bool],
        timeout_seconds: int,
    ) -> tuple[str, dict[str, Any]] | None:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            remaining = max(0.1, deadline - time.monotonic())
            try:
                text, packet = self._queue.get(timeout=min(0.5, remaining))
            except queue.Empty:
                continue
            if predicate(text, packet):
                return text, packet
        return None

    def known_nodes(self) -> list[KnownMeshNode]:
        nodes: list[KnownMeshNode] = []
        if self.interface is None:
            return nodes
        raw_nodes = getattr(self.interface, "nodes", None) or {}
        for node_id, node in raw_nodes.items():
            if not isinstance(node, dict):
                continue
            user = node.get("user", {}) if isinstance(node.get("user", {}), dict) else {}
            nodes.append(
                KnownMeshNode(
                    mesh_id=str(user.get("id") or node_id),
                    long_name=str(user.get("longName") or user.get("longname") or "unknown"),
                    short_name=str(user.get("shortName") or user.get("shortname") or ""),
                    snr=node.get("snr"),
                    rssi=node.get("rssi"),
                    last_heard=node.get("lastHeard"),
                )
            )
        return nodes

    def local_mesh_id(self) -> str:
        if self.interface is None:
            return "unknown"
        my_info = getattr(self.interface, "myInfo", None)
        if my_info is not None:
            node_num = getattr(my_info, "my_node_num", None) or getattr(my_info, "myNodeNum", None)
            if node_num is not None:
                return f"!{int(node_num):08x}"
        local_node = getattr(self.interface, "localNode", None)
        node_num = getattr(local_node, "nodeNum", None)
        if node_num is not None:
            return f"!{int(node_num):08x}"
        return "unknown"

    def actual_config_summary(self) -> dict[str, Any]:
        if self.interface is None:
            return {}
        local_node = getattr(self.interface, "localNode", None)
        local_config = getattr(local_node, "localConfig", None)
        lora = getattr(local_config, "lora", None)
        summary: dict[str, Any] = {}
        try:
            summary["owner.long_name"] = self.interface.getLongName()
            summary["owner.short_name"] = self.interface.getShortName()
        except Exception:
            pass
        if lora is not None:
            summary["lora.region"] = proto_field_value(lora, "region")
            summary["lora.modem_preset"] = proto_field_value(lora, "modem_preset")
            summary["lora.hop_limit"] = proto_field_value(lora, "hop_limit")
            summary["lora.tx_enabled"] = proto_field_value(lora, "tx_enabled")
            summary["lora.channel_num"] = proto_field_value(lora, "channel_num")
            summary["lora.tx_power"] = proto_field_value(lora, "tx_power")
            summary["lora.ignore_mqtt"] = proto_field_value(lora, "ignore_mqtt")
            summary["lora.config_ok_to_mqtt"] = proto_field_value(lora, "config_ok_to_mqtt")
        device = getattr(local_config, "device", None)
        if device is not None:
            summary["device.role"] = proto_field_value(device, "role")
            summary["device.rebroadcast_mode"] = proto_field_value(device, "rebroadcast_mode")
            summary["device.node_info_broadcast_secs"] = proto_field_value(
                device, "node_info_broadcast_secs"
            )
            summary["device.serial_enabled"] = proto_field_value(device, "serial_enabled")
        power = getattr(local_config, "power", None)
        if power is not None:
            summary["power.is_power_saving"] = proto_field_value(power, "is_power_saving")
        channels = getattr(local_node, "channels", None)
        channel = None
        if isinstance(channels, dict):
            channel = channels.get(self.cfg.radio.channel_index)
        elif channels is not None:
            try:
                channel = channels[self.cfg.radio.channel_index]
            except Exception:
                channel = None
        settings = getattr(channel, "settings", None)
        if settings is not None:
            summary["channel.name"] = getattr(settings, "name", "")
            psk = getattr(settings, "psk", b"")
            if isinstance(psk, str):
                psk_bytes = psk.encode("utf-8")
            else:
                psk_bytes = bytes(psk)
            summary["channel.psk_base64"] = base64.b64encode(psk_bytes).decode("ascii")
            summary["channel.psk_len"] = len(psk_bytes)
        return summary


def extract_text(packet: dict[str, Any]) -> str | None:
    decoded = packet.get("decoded")
    if not isinstance(decoded, dict):
        return None
    text = decoded.get("text")
    if isinstance(text, str):
        return text
    payload = decoded.get("payload")
    if isinstance(payload, bytes):
        try:
            return payload.decode("utf-8")
        except UnicodeDecodeError:
            return None
    if isinstance(payload, str):
        return payload
    return None


def extract_payload(packet: dict[str, Any]) -> bytes | None:
    decoded = packet.get("decoded")
    if not isinstance(decoded, dict):
        return None
    payload = decoded.get("payload")
    if isinstance(payload, bytes):
        return payload
    if isinstance(payload, bytearray):
        return bytes(payload)
    if isinstance(payload, str):
        return payload.encode("utf-8")
    return None


def proto_field_value(message: Any, field_name: str) -> Any:
    value = getattr(message, field_name, None)
    descriptor = getattr(message, "DESCRIPTOR", None)
    fields = getattr(descriptor, "fields_by_name", {}) if descriptor is not None else {}
    field = fields.get(field_name) if isinstance(fields, dict) else None
    enum_type = getattr(field, "enum_type", None)
    if enum_type is not None and value is not None:
        enum_value = enum_type.values_by_number.get(int(value))
        if enum_value is not None:
            return enum_value.name
    return value


def radio_config_mismatches(cfg: MeshConfig, actual: dict[str, Any]) -> list[str]:
    expected_psk = channel_psk_for_cli(cfg)
    expected: dict[str, Any] = {
        "owner.long_name": cfg.app.node_name,
        "owner.short_name": cfg.app.node_short_name,
        "lora.region": cfg.radio.region,
        "lora.modem_preset": cfg.radio.modem_preset,
        "lora.hop_limit": cfg.radio.hop_limit,
        "lora.tx_enabled": cfg.radio.transmit_enabled,
        "lora.channel_num": cfg.radio.frequency_slot,
        "lora.tx_power": cfg.radio.tx_power,
        "lora.ignore_mqtt": cfg.radio.ignore_mqtt,
        "lora.config_ok_to_mqtt": cfg.radio.ok_to_mqtt,
        "device.role": cfg.device.role,
        "device.rebroadcast_mode": cfg.device.rebroadcast_mode,
        "device.node_info_broadcast_secs": cfg.device.node_info_broadcast_secs,
        "device.serial_enabled": cfg.device.serial_enabled,
        "power.is_power_saving": cfg.device.is_power_saving,
        "channel.name": cfg.radio.channel_name,
        "channel.psk_base64": (
            expected_psk.removeprefix("base64:") if expected_psk != "none" else ""
        ),
    }
    mismatches: list[str] = []
    for name, expected_value in expected.items():
        actual_value = actual.get(name)
        if str(expected_value).lower() != str(actual_value).lower():
            mismatches.append(
                f"{name} expected={expected_value!s} actual={actual_value!s}"
            )
    return mismatches


def verify_radio_configuration(cfg: MeshConfig) -> dict[str, Any]:
    radio = RadioClient(cfg, "verify")
    try:
        radio.connect(no_nodes=False, timeout=60)
        actual = radio.actual_config_summary()
    finally:
        radio.close()
    if not actual:
        raise MeshNetError(
            "SETUP_VERIFY_UNAVAILABLE",
            "setup",
            "the radio did not return readable configuration after setup",
            "Keep the radio attached and retry setup; run meshnet doctor if it repeats.",
            retryable=True,
        )
    mismatches = radio_config_mismatches(cfg, actual)
    if mismatches:
        raise MeshNetError(
            "SETUP_VERIFY_FAILED",
            "setup",
            "radio settings do not match the YAML config after setup",
            "Review the mismatched fields, then rerun setup on this node.",
            retryable=True,
            details={"mismatches": mismatches},
        )
    return actual


def extract_packet_id(packet: Any) -> str:
    if isinstance(packet, dict):
        for key in ("id", "packetId", "packet_id"):
            value = packet.get(key)
            if value is not None:
                return str(value)
    value = getattr(packet, "id", None)
    if value is not None:
        return str(value)
    return ""


def extract_routing_error(packet: dict[str, Any] | None) -> str:
    if not packet:
        return ""
    decoded = packet.get("decoded")
    if isinstance(decoded, dict):
        routing = decoded.get("routing")
        if isinstance(routing, dict):
            for key in ("errorReason", "error_reason", "error"):
                value = routing.get(key)
                if value:
                    return str(value)
    for key in ("errorReason", "error_reason", "error"):
        value = packet.get(key)
        if value:
            return str(value)
    return ""


def packet_from_mesh_id(packet: dict[str, Any]) -> str:
    value = packet.get("fromId") or packet.get("from_id")
    if value:
        return str(value)
    value = packet.get("from")
    if isinstance(value, int):
        return f"!{value:08x}"
    return ""


def packet_to_mesh_id(packet: dict[str, Any]) -> str:
    value = packet.get("toId") or packet.get("to_id")
    if value:
        return str(value)
    value = packet.get("to")
    if isinstance(value, int):
        return f"!{value:08x}"
    return ""


def find_meshtastic_cli() -> str | None:
    venv_exe = Path(sys.executable).with_name("meshtastic")
    return str(venv_exe) if venv_exe.exists() else shutil.which("meshtastic")


def run_meshtastic_cli(args: list[str], scope: str = "setup") -> subprocess.CompletedProcess[str]:
    exe = find_meshtastic_cli()
    if exe is None:
        raise RuntimeError("meshtastic CLI not found in PATH. Run ./install.sh first.")
    env = os.environ.copy()
    env.setdefault("PYTHONUNBUFFERED", "1")
    proc = subprocess.run(
        [exe, *args],
        text=True,
        capture_output=True,
        env=env,
        timeout=180,
        check=False,
    )
    if proc.stdout.strip():
        for raw in proc.stdout.strip().splitlines():
            logger.line(scope, raw)
    if proc.stderr.strip():
        for raw in proc.stderr.strip().splitlines():
            logger.line(scope, raw)
    if proc.returncode != 0:
        raise RuntimeError(f"meshtastic CLI failed with exit code {proc.returncode}")
    return proc


def setup_radio(cfg: MeshConfig) -> None:
    logger.line("meshnet", "Starting setup-radio...")
    logger.line("config", f"Loaded: {cfg.path.name}")
    logger.line("config", f"Role: {cfg.app.role}")
    logger.line("config", f"Node ID: {cfg.app.node_id}")
    logger.line("config", f"Node Name: {cfg.app.node_name}")
    logger.blank()

    logger.line("radio", "Detecting serial port...")
    port = resolve_port(cfg, "radio", verbose=False)
    logger.line("radio", f"Found: {port}")
    logger.line("radio", "Connecting...")
    radio = RadioClient(cfg, "radio")
    radio.port = port
    try:
        radio.connect(no_nodes=True, timeout=60)
    finally:
        radio.close()
    logger.blank()

    psk = channel_psk_for_cli(cfg)
    logger.line("setup", "Applying radio configuration...")
    logger.line("setup", f"Setting lora.region = {cfg.radio.region}")
    logger.line("setup", f"Setting lora.modem_preset = {cfg.radio.modem_preset}")
    logger.line("setup", f"Setting lora.hop_limit = {cfg.radio.hop_limit}")
    logger.line("setup", f"Setting lora.channel_num = {cfg.radio.frequency_slot}")
    logger.line("setup", f"Setting lora.tx_power = {cfg.radio.tx_power}")
    logger.line("setup", f"Setting lora.ignore_mqtt = {str(cfg.radio.ignore_mqtt).lower()}")
    logger.line("setup", f"Setting lora.config_ok_to_mqtt = {str(cfg.radio.ok_to_mqtt).lower()}")
    logger.line("setup", f"Setting lora.tx_enabled = {str(cfg.radio.transmit_enabled).lower()}")
    logger.line("setup", f"Setting device.role = {cfg.device.role}")
    logger.line("setup", f"Setting device.rebroadcast_mode = {cfg.device.rebroadcast_mode}")
    logger.line("setup", f"Setting device.node_info_broadcast_secs = {cfg.device.node_info_broadcast_secs}")
    logger.line("setup", f"Setting device.serial_enabled = {str(cfg.device.serial_enabled).lower()}")
    logger.line("setup", f"Setting power.is_power_saving = {str(cfg.device.is_power_saving).lower()}")
    logger.line("setup", f"Setting channel[{cfg.radio.channel_index}].name = {cfg.radio.channel_name}")
    logger.line("setup", f"Setting channel[{cfg.radio.channel_index}].psk = {cfg.radio.channel_psk_mode}")
    logger.line("setup", f"Setting owner long name = {cfg.app.node_name}")
    logger.line("setup", f"Setting owner short name = {cfg.app.node_short_name}")

    command = [
        "--port",
        port,
        "--set",
        "lora.region",
        cfg.radio.region,
        "--set",
        "lora.modem_preset",
        cfg.radio.modem_preset,
        "--set",
        "lora.hop_limit",
        str(cfg.radio.hop_limit),
        "--set",
        "lora.channel_num",
        str(cfg.radio.frequency_slot),
        "--set",
        "lora.tx_power",
        str(cfg.radio.tx_power),
        "--set",
        "lora.ignore_mqtt",
        str(cfg.radio.ignore_mqtt).lower(),
        "--set",
        "lora.config_ok_to_mqtt",
        str(cfg.radio.ok_to_mqtt).lower(),
        "--set",
        "lora.tx_enabled",
        str(cfg.radio.transmit_enabled).lower(),
        "--set",
        "device.role",
        cfg.device.role,
        "--set",
        "device.rebroadcast_mode",
        cfg.device.rebroadcast_mode,
        "--set",
        "device.node_info_broadcast_secs",
        str(cfg.device.node_info_broadcast_secs),
        "--set",
        "device.serial_enabled",
        str(cfg.device.serial_enabled).lower(),
        "--set",
        "power.is_power_saving",
        str(cfg.device.is_power_saving).lower(),
        "--ch-index",
        str(cfg.radio.channel_index),
        "--ch-set",
        "name",
        cfg.radio.channel_name,
        "--ch-set",
        "psk",
        psk,
        "--set-owner",
        cfg.app.node_name,
        "--set-owner-short",
        cfg.app.node_short_name,
    ]
    run_meshtastic_cli(command, "setup")

    logger.line("setup", "Rebooting radio...")
    try:
        run_meshtastic_cli(["--port", port, "--reboot"], "setup")
    except Exception as exc:
        logger.line("setup", f"Reboot command warning: {exc}")

    logger.blank()
    logger.line("setup", "Waiting for radio to return...")
    wait_for_radio_return(cfg, port)
    logger.line("setup", "Radio online.")
    logger.line("setup", "Radio settings applied; verifying...")
    logger.blank()
    logger.line("setup", f"channel_psk = {channel_psk_description(cfg)}")


def wait_for_radio_return(cfg: MeshConfig, port: str, timeout_seconds: int = 60) -> None:
    deadline = time.monotonic() + timeout_seconds
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        if Path(port).exists():
            try:
                probe = RadioClient(cfg, "radio")
                probe.connect(no_nodes=True, timeout=20)
                probe.close()
                return
            except Exception as exc:
                last_error = exc
        time.sleep(2)
    if last_error:
        raise RuntimeError(f"radio did not return: {last_error}") from last_error
    raise RuntimeError("radio did not return")


def setup_radio_reliably(cfg: MeshConfig, *, run_preflight: bool = True) -> int:
    from .preflight import require_preflight

    last_error: MeshNetError | None = None
    max_attempts = cfg.runtime.setup_retries
    for attempt in range(1, max_attempts + 1):
        logger.line("setup", f"Setup attempt {attempt}/{max_attempts}.")
        try:
            if run_preflight:
                require_preflight(cfg, attempts=1)
            setup_radio(cfg)
            verify_radio_configuration(cfg)
            logger.line("setup", f"Set up node: DONE ({attempt} attempt(s)).")
            return attempt
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            error = as_meshnet_error(exc, "setup", attempts=attempt)
            last_error = error
            log_meshnet_error(error, "setup")
            if not error.retryable or attempt >= max_attempts:
                raise error from exc
            logger.line(
                "setup",
                f"Retrying setup in {cfg.runtime.retry_backoff_seconds} seconds.",
            )
            time.sleep(cfg.runtime.retry_backoff_seconds)
    if last_error is not None:
        raise last_error
    raise MeshNetError(
        "SETUP_FAILED",
        "setup",
        "radio setup did not run",
        "Retry setup and review the preceding logs.",
    )
