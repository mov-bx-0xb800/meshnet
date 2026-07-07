from __future__ import annotations

import base64
import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

import yaml


VALID_ROLES = {"master", "slave"}
VALID_PSK_MODES = {"derived", "base64", "none"}
VALID_DEVICE_ROLES = {
    "CLIENT",
    "CLIENT_MUTE",
    "CLIENT_HIDDEN",
    "CLIENT_BASE",
    "TRACKER",
    "LOST_AND_FOUND",
    "SENSOR",
    "TAK",
    "TAK_TRACKER",
    "REPEATER",
    "ROUTER",
    "ROUTER_CLIENT",
    "ROUTER_LATE",
}
VALID_REBROADCAST_MODES = {
    "ALL",
    "ALL_SKIP_DECODING",
    "LOCAL_ONLY",
    "KNOWN_ONLY",
    "NONE",
    "CORE_PORTNUMS_ONLY",
}
DEFAULT_REGION = "MY_919"
SUPPORTED_MALAYSIA_REGIONS = {"MY_919", "MY_433"}


@dataclass(frozen=True)
class AppConfig:
    node_id: str
    role: str
    node_name: str
    node_short_name: str
    log_level: str


@dataclass(frozen=True)
class RadioConfig:
    port: str
    region: str
    modem_preset: str
    hop_limit: int
    channel_index: int
    channel_name: str
    channel_psk_mode: str
    channel_psk_base64: str
    transmit_enabled: bool
    frequency_slot: int
    tx_power: int
    ignore_mqtt: bool
    ok_to_mqtt: bool


@dataclass(frozen=True)
class DeviceConfig:
    role: str
    rebroadcast_mode: str
    node_info_broadcast_secs: int
    is_power_saving: bool
    serial_enabled: bool


@dataclass(frozen=True)
class PeerConfig:
    app_id: str
    mesh_id: str


@dataclass(frozen=True)
class NetworkConfig:
    network_id: str
    network_password: str
    allow_broadcast: bool
    master_id: str
    slave_id: str
    peers: tuple[PeerConfig, ...]


@dataclass(frozen=True)
class RuntimeConfig:
    heartbeat_interval_seconds: int
    ping_interval_seconds: int
    ack_timeout_seconds: int
    radio_ack_timeout_seconds: int
    send_retries: int
    setup_retries: int
    connect_retries: int
    discovery_retries: int
    retry_backoff_seconds: int
    runtime_reconnect: bool
    reconnect_delay_seconds: int
    max_reconnect_attempts: int
    test_message_count: int
    max_payload_chars: int
    discovery_timeout_seconds: int
    compatibility_timeout_seconds: int
    allow_fast_ping_interval: bool = False


@dataclass(frozen=True)
class BridgeConfig:
    enabled: bool
    listen_host: str
    listen_port: int
    upstream_host: str
    upstream_port: int
    payload_bytes: int
    window_size: int
    ack_timeout_seconds: float
    control_timeout_seconds: float
    max_retries: int
    frame_interval_ms: int
    poll_interval_ms: int
    max_buffer_bytes: int
    metrics_interval_seconds: int


@dataclass(frozen=True)
class TelegramConfig:
    enabled: bool
    bot_token: str
    allowed_chat_id: str

    @property
    def configured(self) -> bool:
        return bool(self.bot_token and self.allowed_chat_id)


@dataclass(frozen=True)
class MeshConfig:
    path: Path
    app: AppConfig
    radio: RadioConfig
    device: DeviceConfig
    network: NetworkConfig
    runtime: RuntimeConfig
    telegram: TelegramConfig
    bridge: BridgeConfig

    @property
    def peer_node_id(self) -> str:
        if self.app.role == "master":
            return self.network.slave_id
        return self.network.master_id

    @property
    def peer_role(self) -> str:
        if self.app.role == "master":
            return "slave"
        return "master"

    @property
    def safe_ping_interval(self) -> int:
        interval = int(self.runtime.ping_interval_seconds)
        if interval < 5 and not self.runtime.allow_fast_ping_interval:
            return 5
        return interval

    @property
    def configured_peers(self) -> tuple[PeerConfig, ...]:
        return self.network.peers

    def mesh_id_for(self, app_id: str) -> str | None:
        for peer in self.network.peers:
            if peer.app_id == app_id and peer.mesh_id:
                return peer.mesh_id
        return None

    def app_id_for_mesh(self, mesh_id: str) -> str | None:
        normalized = mesh_id.lower()
        for peer in self.network.peers:
            if peer.mesh_id.lower() == normalized:
                return peer.app_id
        return None


def _section(data: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    value = data.get(name)
    if not isinstance(value, Mapping):
        raise ValueError(f"missing or invalid config section: {name}")
    return value


def _optional_section(data: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    value = data.get(name, {})
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError(f"invalid config section: {name}")
    return value


def _str(section: Mapping[str, Any], key: str, default: str = "") -> str:
    value = section.get(key, default)
    if value is None:
        return default
    return str(value)


def _int(section: Mapping[str, Any], key: str, default: int) -> int:
    value = section.get(key, default)
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{key} must be an integer") from exc


def _float(section: Mapping[str, Any], key: str, default: float) -> float:
    value = section.get(key, default)
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{key} must be a number") from exc


def _bool(section: Mapping[str, Any], key: str, default: bool) -> bool:
    value = section.get(key, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _env_str_if_blank(section: Mapping[str, Any], key: str, env_key: str) -> str:
    value = _str(section, key)
    if value:
        return value
    return os.getenv(env_key, "")


def _env_bool_or_config(section: Mapping[str, Any], key: str, env_key: str, default: bool) -> bool:
    if os.getenv(env_key):
        return os.getenv(env_key, "").strip().lower() in {"1", "true", "yes", "on"}
    return _bool(section, key, default)


def _load_peers(network: Mapping[str, Any], app: Mapping[str, Any]) -> tuple[PeerConfig, ...]:
    raw_peers = network.get("peers")
    if raw_peers is None:
        role = _str(app, "role")
        legacy_peer = _str(network, "slave_id") if role == "master" else _str(network, "master_id")
        return (PeerConfig(legacy_peer, ""),) if legacy_peer else ()
    if not isinstance(raw_peers, Sequence) or isinstance(raw_peers, (str, bytes)):
        raise ValueError("network.peers must be a YAML list")
    peers: list[PeerConfig] = []
    for index, raw_peer in enumerate(raw_peers):
        if not isinstance(raw_peer, Mapping):
            raise ValueError(f"network.peers[{index}] must be an object")
        peers.append(
            PeerConfig(
                app_id=_str(raw_peer, "app_id"),
                mesh_id=_str(raw_peer, "mesh_id").lower(),
            )
        )
    return tuple(peers)


def load_config(path: str | os.PathLike[str]) -> MeshConfig:
    config_path = Path(path).expanduser().resolve()
    with config_path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}

    if not isinstance(raw, Mapping):
        raise ValueError("config file must contain a YAML object")

    app = _section(raw, "app")
    radio = _section(raw, "radio")
    device = _optional_section(raw, "device")
    network = _section(raw, "network")
    runtime = _section(raw, "runtime")
    telegram = _section(raw, "telegram")
    bridge = _optional_section(raw, "bridge")

    cfg = MeshConfig(
        path=config_path,
        app=AppConfig(
            node_id=_str(app, "node_id"),
            role=_str(app, "role"),
            node_name=_str(app, "node_name"),
            node_short_name=_str(app, "node_short_name"),
            log_level=_str(app, "log_level", "INFO"),
        ),
        radio=RadioConfig(
            port=_str(radio, "port", "auto"),
            region=_str(radio, "region", DEFAULT_REGION),
            modem_preset=_str(radio, "modem_preset", "LONG_FAST"),
            hop_limit=_int(radio, "hop_limit", 3),
            channel_index=_int(radio, "channel_index", 0),
            channel_name=_str(radio, "channel_name"),
            channel_psk_mode=_str(radio, "channel_psk_mode", "derived"),
            channel_psk_base64=_str(radio, "channel_psk_base64", ""),
            transmit_enabled=_bool(radio, "transmit_enabled", True),
            frequency_slot=_int(radio, "frequency_slot", 0),
            tx_power=_int(radio, "tx_power", 0),
            ignore_mqtt=_bool(radio, "ignore_mqtt", True),
            ok_to_mqtt=_bool(radio, "ok_to_mqtt", False),
        ),
        device=DeviceConfig(
            role=_str(device, "role", "CLIENT").upper(),
            rebroadcast_mode=_str(device, "rebroadcast_mode", "LOCAL_ONLY").upper(),
            node_info_broadcast_secs=_int(device, "node_info_broadcast_secs", 3600),
            is_power_saving=_bool(device, "is_power_saving", False),
            serial_enabled=_bool(device, "serial_enabled", True),
        ),
        network=NetworkConfig(
            network_id=_str(network, "network_id"),
            network_password=_str(network, "network_password"),
            allow_broadcast=_bool(network, "allow_broadcast", True),
            master_id=_str(network, "master_id"),
            slave_id=_str(network, "slave_id"),
            peers=_load_peers(network, app),
        ),
        runtime=RuntimeConfig(
            heartbeat_interval_seconds=_int(runtime, "heartbeat_interval_seconds", 60),
            ping_interval_seconds=_int(runtime, "ping_interval_seconds", 10),
            ack_timeout_seconds=_int(runtime, "ack_timeout_seconds", 25),
            radio_ack_timeout_seconds=_int(runtime, "radio_ack_timeout_seconds", 15),
            send_retries=_int(runtime, "send_retries", 3),
            setup_retries=_int(runtime, "setup_retries", 3),
            connect_retries=_int(runtime, "connect_retries", 3),
            discovery_retries=_int(runtime, "discovery_retries", 3),
            retry_backoff_seconds=_int(runtime, "retry_backoff_seconds", 5),
            runtime_reconnect=_bool(runtime, "runtime_reconnect", True),
            reconnect_delay_seconds=_int(runtime, "reconnect_delay_seconds", 5),
            max_reconnect_attempts=_int(runtime, "max_reconnect_attempts", 0),
            test_message_count=_int(runtime, "test_message_count", 5),
            max_payload_chars=_int(runtime, "max_payload_chars", 60),
            discovery_timeout_seconds=_int(runtime, "discovery_timeout_seconds", 30),
            compatibility_timeout_seconds=_int(runtime, "compatibility_timeout_seconds", 30),
            allow_fast_ping_interval=_bool(runtime, "allow_fast_ping_interval", False),
        ),
        telegram=TelegramConfig(
            enabled=_env_bool_or_config(telegram, "enabled", "TELEGRAM_ENABLED", True),
            bot_token=_env_str_if_blank(telegram, "bot_token", "TELEGRAM_BOT_TOKEN"),
            allowed_chat_id=_env_str_if_blank(
                telegram, "allowed_chat_id", "TELEGRAM_ALLOWED_CHAT_ID"
            ),
        ),
        bridge=BridgeConfig(
            enabled=_bool(bridge, "enabled", False),
            listen_host=_str(bridge, "listen_host", "127.0.0.1"),
            listen_port=_int(bridge, "listen_port", 8081),
            upstream_host=_str(bridge, "upstream_host", "127.0.0.1"),
            upstream_port=_int(bridge, "upstream_port", 8081),
            payload_bytes=_int(bridge, "payload_bytes", 192),
            window_size=_int(bridge, "window_size", 8),
            ack_timeout_seconds=_float(bridge, "ack_timeout_seconds", 5.0),
            control_timeout_seconds=_float(bridge, "control_timeout_seconds", 10.0),
            max_retries=_int(bridge, "max_retries", 8),
            frame_interval_ms=_int(bridge, "frame_interval_ms", 210),
            poll_interval_ms=_int(bridge, "poll_interval_ms", 500),
            max_buffer_bytes=_int(bridge, "max_buffer_bytes", 65536),
            metrics_interval_seconds=_int(bridge, "metrics_interval_seconds", 60),
        ),
    )
    validate_config(cfg)
    return cfg


def validate_config(cfg: MeshConfig) -> None:
    if cfg.app.role not in VALID_ROLES:
        raise ValueError("app.role must be 'master' or 'slave'")
    if not cfg.app.node_id:
        raise ValueError("app.node_id is required")
    if not cfg.app.node_name:
        raise ValueError("app.node_name is required")
    if not cfg.app.node_short_name:
        raise ValueError("app.node_short_name is required")
    if len(cfg.app.node_short_name) > 4:
        raise ValueError("app.node_short_name should be 4 characters or fewer")
    if cfg.radio.region not in SUPPORTED_MALAYSIA_REGIONS:
        raise ValueError("radio.region must be MY_919 or MY_433 for this Malaysia setup")
    if cfg.radio.channel_psk_mode not in VALID_PSK_MODES:
        raise ValueError("radio.channel_psk_mode must be derived, base64, or none")
    if cfg.device.role not in VALID_DEVICE_ROLES:
        raise ValueError(
            "device.role must be one of: " + ", ".join(sorted(VALID_DEVICE_ROLES))
        )
    if cfg.device.rebroadcast_mode not in VALID_REBROADCAST_MODES:
        raise ValueError(
            "device.rebroadcast_mode must be one of: "
            + ", ".join(sorted(VALID_REBROADCAST_MODES))
        )
    if cfg.device.rebroadcast_mode == "ALL_SKIP_DECODING" and cfg.device.role != "REPEATER":
        raise ValueError("device.rebroadcast_mode ALL_SKIP_DECODING is only valid with device.role REPEATER")
    if cfg.device.rebroadcast_mode == "NONE" and cfg.device.role not in {
        "SENSOR",
        "TRACKER",
        "TAK_TRACKER",
    }:
        raise ValueError("device.rebroadcast_mode NONE is only valid with SENSOR, TRACKER, or TAK_TRACKER")
    if cfg.device.node_info_broadcast_secs < 3600:
        raise ValueError("device.node_info_broadcast_secs must be at least 3600")
    if cfg.device.role in {"ROUTER", "REPEATER", "ROUTER_CLIENT"} and cfg.app.role in VALID_ROLES:
        raise ValueError(
            "device.role ROUTER/REPEATER/ROUTER_CLIENT is not suitable for a Pi USB-serial MeshNet runtime; use CLIENT or CLIENT_MUTE"
        )
    if not cfg.device.serial_enabled:
        raise ValueError("device.serial_enabled must be true for a Pi USB-serial MeshNet runtime")
    if not cfg.radio.channel_name:
        raise ValueError("radio.channel_name is required")
    if not cfg.network.network_id:
        raise ValueError("network.network_id is required")
    if not cfg.network.network_password:
        raise ValueError("network.network_password is required")
    if cfg.network.network_password == "change-this-password":
        # This is allowed for a private test network, but the README tells users to change it.
        pass
    if cfg.app.role == "master" and cfg.app.node_id != cfg.network.master_id:
        raise ValueError("master config app.node_id must match network.master_id")
    if cfg.app.role == "slave" and cfg.app.node_id != cfg.network.slave_id:
        raise ValueError("slave config app.node_id must match network.slave_id")
    if cfg.runtime.ack_timeout_seconds < 1:
        raise ValueError("runtime.ack_timeout_seconds must be positive")
    if cfg.runtime.radio_ack_timeout_seconds < 1:
        raise ValueError("runtime.radio_ack_timeout_seconds must be positive")
    if cfg.runtime.send_retries < 1:
        raise ValueError("runtime.send_retries must be positive")
    if cfg.runtime.setup_retries < 1:
        raise ValueError("runtime.setup_retries must be positive")
    if cfg.runtime.connect_retries < 1:
        raise ValueError("runtime.connect_retries must be positive")
    if cfg.runtime.discovery_retries < 1:
        raise ValueError("runtime.discovery_retries must be positive")
    if cfg.runtime.retry_backoff_seconds < 0:
        raise ValueError("runtime.retry_backoff_seconds cannot be negative")
    if cfg.runtime.reconnect_delay_seconds < 0:
        raise ValueError("runtime.reconnect_delay_seconds cannot be negative")
    if cfg.runtime.max_reconnect_attempts < 0:
        raise ValueError("runtime.max_reconnect_attempts cannot be negative")
    if cfg.runtime.max_payload_chars < 1:
        raise ValueError("runtime.max_payload_chars must be positive")
    if not 0 <= cfg.radio.channel_index <= 7:
        raise ValueError("radio.channel_index must be between 0 and 7")
    if not 1 <= cfg.radio.hop_limit <= 7:
        raise ValueError("radio.hop_limit must be between 1 and 7")
    if not 0 <= cfg.radio.frequency_slot <= 255:
        raise ValueError("radio.frequency_slot must be between 0 and 255")
    if not 0 <= cfg.radio.tx_power <= 30:
        raise ValueError("radio.tx_power must be between 0 and 30 dBm")

    peer_ids = [peer.app_id for peer in cfg.network.peers]
    peer_mesh_ids = [peer.mesh_id for peer in cfg.network.peers if peer.mesh_id]
    if any(not peer_id for peer_id in peer_ids):
        raise ValueError("every network.peers entry requires app_id")
    if cfg.app.node_id in peer_ids:
        raise ValueError("network.peers must not include this node's own app.node_id")
    if len(peer_ids) != len(set(peer_ids)):
        raise ValueError("network.peers app_id values must be unique")
    if len(peer_mesh_ids) != len(set(peer_mesh_ids)):
        raise ValueError("network.peers mesh_id values must be unique")
    for mesh_id in peer_mesh_ids:
        if not mesh_id.startswith("!") or len(mesh_id) != 9:
            raise ValueError("network.peers mesh_id must look like !a1b2c3d4")

    if cfg.bridge.enabled:
        if not cfg.network.peers:
            raise ValueError("bridge.enabled requires at least one network.peers entry")
        if any(not peer.mesh_id for peer in cfg.network.peers):
            raise ValueError("bridge.enabled requires a pinned mesh_id for every peer")
        if cfg.app.role == "slave" and len(cfg.network.peers) != 1:
            raise ValueError("a bridge client must configure exactly one central peer")
        if not 1 <= cfg.bridge.listen_port <= 65535:
            raise ValueError("bridge.listen_port must be between 1 and 65535")
        if not 1 <= cfg.bridge.upstream_port <= 65535:
            raise ValueError("bridge.upstream_port must be between 1 and 65535")
        if not 32 <= cfg.bridge.payload_bytes <= 193:
            raise ValueError("bridge.payload_bytes must be between 32 and 193")
        if not 1 <= cfg.bridge.window_size <= 32:
            raise ValueError("bridge.window_size must be between 1 and 32")
        if cfg.bridge.ack_timeout_seconds <= 0 or cfg.bridge.control_timeout_seconds <= 0:
            raise ValueError("bridge timeouts must be positive")
        if cfg.bridge.max_retries < 1:
            raise ValueError("bridge.max_retries must be positive")
        if cfg.bridge.frame_interval_ms < 0 or cfg.bridge.poll_interval_ms < 50:
            raise ValueError("bridge frame/poll intervals are invalid")
        if cfg.bridge.max_buffer_bytes < cfg.bridge.payload_bytes * cfg.bridge.window_size * 2:
            raise ValueError("bridge.max_buffer_bytes is too small for two protocol windows")
        if cfg.radio.region != "MY_919":
            raise ValueError("the Tasik Chini Flower bridge requires radio.region MY_919")
        if cfg.radio.modem_preset != "SHORT_FAST":
            raise ValueError("the Flower bridge requires radio.modem_preset SHORT_FAST")
        if cfg.radio.hop_limit != 1:
            raise ValueError("the direct Flower star requires radio.hop_limit 1")
        if not 1 <= cfg.radio.frequency_slot <= 15:
            raise ValueError(
                "the Flower bridge requires an explicit MY_919 SHORT_FAST frequency_slot "
                "between 1 and 15 to remain inside the 919-923 MHz planning band with margin"
            )
        if cfg.radio.tx_power == 0:
            raise ValueError(
                "the Flower bridge requires an explicit radio.tx_power derived from EIRP"
            )
        if cfg.device.role != "CLIENT_MUTE":
            raise ValueError("the direct Flower star requires device.role CLIENT_MUTE")
        if cfg.device.is_power_saving:
            raise ValueError("the Flower bridge requires power saving to be disabled")
        if cfg.network.allow_broadcast:
            raise ValueError("the Flower bridge requires network.allow_broadcast false")
        if not cfg.radio.ignore_mqtt or cfg.radio.ok_to_mqtt:
            raise ValueError("the Flower bridge requires MQTT ingress/egress to be disabled")


def derive_channel_psk_bytes(cfg: MeshConfig) -> bytes:
    seed = f"{cfg.network.network_id}:{cfg.network.network_password}".encode("utf-8")
    return hashlib.sha256(seed).digest()


def channel_psk_for_cli(cfg: MeshConfig) -> str:
    mode = cfg.radio.channel_psk_mode
    if mode == "none":
        return "none"
    if mode == "base64":
        try:
            raw = base64.b64decode(cfg.radio.channel_psk_base64, validate=True)
        except Exception as exc:
            raise ValueError("radio.channel_psk_base64 is not valid base64") from exc
        if len(raw) not in {16, 32}:
            raise ValueError("radio.channel_psk_base64 must decode to 16 or 32 bytes")
        return f"base64:{cfg.radio.channel_psk_base64}"
    psk = base64.b64encode(derive_channel_psk_bytes(cfg)).decode("ascii")
    return f"base64:{psk}"


def channel_psk_description(cfg: MeshConfig) -> str:
    if cfg.radio.channel_psk_mode == "derived":
        return "derived from network password"
    if cfg.radio.channel_psk_mode == "base64":
        return "base64 from config"
    return "none"
