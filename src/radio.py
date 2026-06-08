from __future__ import annotations

import glob
import os
import queue
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import logger
from .config import MeshConfig, channel_psk_description, channel_psk_for_cli


BROADCAST_ADDR = "^all"
SERIAL_PATTERNS = ("/dev/ttyACM*", "/dev/ttyUSB*")


@dataclass(frozen=True)
class KnownMeshNode:
    mesh_id: str
    long_name: str
    short_name: str
    snr: Any = None
    rssi: Any = None
    last_heard: Any = None


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
        self._subscribed = False

    def connect(self, no_nodes: bool = False, timeout: int = 60) -> None:
        if self.port is None:
            self.port = resolve_port(self.cfg, self.scope, verbose=True)
        logger.line(self.scope, "Connecting to local Meshtastic radio...")
        try:
            import meshtastic.serial_interface
            from pubsub import pub
        except Exception as exc:
            raise RuntimeError(
                "Meshtastic Python package is not installed. Run ./install.sh first."
            ) from exc

        self.interface = meshtastic.serial_interface.SerialInterface(
            devPath=self.port,
            noNodes=no_nodes,
            timeout=timeout,
        )
        if not self._subscribed:
            pub.subscribe(self._on_receive, "meshtastic.receive")
            self._subscribed = True
        logger.line(self.scope, "Connected.")

    def close(self) -> None:
        if self.interface is not None:
            try:
                self.interface.close()
            except Exception:
                pass
        self.interface = None

    def add_handler(self, handler: Callable[[str, dict[str, Any]], None]) -> None:
        self._handlers.append(handler)

    def _on_receive(self, packet: dict[str, Any] | None = None, interface: Any = None, **_: Any) -> None:
        if not packet:
            return
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
    ) -> Any:
        if self.interface is None:
            raise RuntimeError("radio is not connected")
        return self.interface.sendText(
            text,
            destinationId=destination_id,
            wantAck=want_ack,
            channelIndex=self.cfg.radio.channel_index,
            hopLimit=self.cfg.radio.hop_limit,
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
    logger.line("setup", f"Setting lora.tx_enabled = {str(cfg.radio.transmit_enabled).lower()}")
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
        "lora.tx_enabled",
        str(cfg.radio.transmit_enabled).lower(),
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
    logger.line("setup", "Set up node: DONE.")
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
