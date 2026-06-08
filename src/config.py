from __future__ import annotations

import base64
import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import yaml


VALID_ROLES = {"master", "slave"}
VALID_PSK_MODES = {"derived", "base64", "none"}
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


@dataclass(frozen=True)
class NetworkConfig:
    network_id: str
    network_password: str
    allow_broadcast: bool
    master_id: str
    slave_id: str


@dataclass(frozen=True)
class RuntimeConfig:
    heartbeat_interval_seconds: int
    ping_interval_seconds: int
    ack_timeout_seconds: int
    test_message_count: int
    max_payload_chars: int
    discovery_timeout_seconds: int
    compatibility_timeout_seconds: int
    allow_fast_ping_interval: bool = False


@dataclass(frozen=True)
class TelegramConfig:
    enabled: bool
    bot_token: str
    allowed_chat_id: str


@dataclass(frozen=True)
class MeshConfig:
    path: Path
    app: AppConfig
    radio: RadioConfig
    network: NetworkConfig
    runtime: RuntimeConfig
    telegram: TelegramConfig

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


def _section(data: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    value = data.get(name)
    if not isinstance(value, Mapping):
        raise ValueError(f"missing or invalid config section: {name}")
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


def load_config(path: str | os.PathLike[str]) -> MeshConfig:
    config_path = Path(path).expanduser().resolve()
    with config_path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}

    if not isinstance(raw, Mapping):
        raise ValueError("config file must contain a YAML object")

    app = _section(raw, "app")
    radio = _section(raw, "radio")
    network = _section(raw, "network")
    runtime = _section(raw, "runtime")
    telegram = _section(raw, "telegram")

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
        ),
        network=NetworkConfig(
            network_id=_str(network, "network_id"),
            network_password=_str(network, "network_password"),
            allow_broadcast=_bool(network, "allow_broadcast", True),
            master_id=_str(network, "master_id"),
            slave_id=_str(network, "slave_id"),
        ),
        runtime=RuntimeConfig(
            heartbeat_interval_seconds=_int(runtime, "heartbeat_interval_seconds", 60),
            ping_interval_seconds=_int(runtime, "ping_interval_seconds", 10),
            ack_timeout_seconds=_int(runtime, "ack_timeout_seconds", 25),
            test_message_count=_int(runtime, "test_message_count", 5),
            max_payload_chars=_int(runtime, "max_payload_chars", 160),
            discovery_timeout_seconds=_int(runtime, "discovery_timeout_seconds", 30),
            compatibility_timeout_seconds=_int(runtime, "compatibility_timeout_seconds", 30),
            allow_fast_ping_interval=_bool(runtime, "allow_fast_ping_interval", False),
        ),
        telegram=TelegramConfig(
            enabled=_env_bool_or_config(telegram, "enabled", "TELEGRAM_ENABLED", False),
            bot_token=_env_str_if_blank(telegram, "bot_token", "TELEGRAM_BOT_TOKEN"),
            allowed_chat_id=_env_str_if_blank(
                telegram, "allowed_chat_id", "TELEGRAM_ALLOWED_CHAT_ID"
            ),
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
    if cfg.runtime.max_payload_chars < 1:
        raise ValueError("runtime.max_payload_chars must be positive")


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
