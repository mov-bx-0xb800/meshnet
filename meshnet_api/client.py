from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

import yaml
from dotenv import load_dotenv

from src import logger
from src.config import MeshConfig, channel_psk_description, load_config
from src.errors import MeshNetError, as_meshnet_error
from src.master import MasterNode, run_master
from src.preflight import preflight_check, require_preflight
from src.radio import detect_serial_ports, setup_radio_reliably
from src.slave import run_slave
from src.state import StateStore, config_fingerprint, state_path_for_config
from src.tester import run_tests

from .models import (
    ApiResult,
    DeliveryReport,
    DiscoveryReport,
    JsonDict,
    format_age,
    to_jsonable,
)


class MeshNetClient:
    """Stable Python API for MeshNet.

    Radio-using calls open the configured serial device for the duration of the
    call. Do not call them while another MeshNet runtime owns the same USB port.
    """

    def __init__(self, config_path: str | Path = "config.master.yaml") -> None:
        load_dotenv()
        self.config_path = Path(config_path).expanduser()
        self.cfg = self.load()
        logger.configure_logging(self.cfg.app.log_level)

    def load(self) -> MeshConfig:
        try:
            self.cfg = load_config(self.config_path)
        except Exception as exc:
            raise as_meshnet_error(exc, "config") from exc
        return self.cfg

    def reload(self) -> MeshConfig:
        return self.load()

    def config_summary(self) -> JsonDict:
        cfg = self.reload()
        return {
            "config_path": str(cfg.path),
            "state_path": str(state_path_for_config(cfg)),
            "fingerprint": config_fingerprint(cfg),
            "app": to_jsonable(cfg.app),
            "radio": {
                **to_jsonable(cfg.radio),
                "channel_psk": channel_psk_description(cfg),
            },
            "device": to_jsonable(cfg.device),
            "network": {
                "network_id": cfg.network.network_id,
                "allow_broadcast": cfg.network.allow_broadcast,
                "master_id": cfg.network.master_id,
                "slave_id": cfg.network.slave_id,
            },
            "runtime": to_jsonable(cfg.runtime),
            "telegram": {
                "configured": cfg.telegram.configured,
                "enabled": cfg.telegram.enabled,
                "allowed_chat_id_set": bool(cfg.telegram.allowed_chat_id),
                "bot_token_set": bool(cfg.telegram.bot_token),
            },
        }

    def read_config_file(self) -> JsonDict:
        try:
            with self.config_path.open("r", encoding="utf-8") as fh:
                raw = yaml.safe_load(fh) or {}
            if not isinstance(raw, dict):
                raise ValueError("config file must contain a YAML object")
            return raw
        except Exception as exc:
            raise as_meshnet_error(exc, "config") from exc

    def update_config(self, patch: Mapping[str, Mapping[str, Any]]) -> JsonDict:
        try:
            raw = self.read_config_file()
            for section, values in patch.items():
                if not isinstance(values, Mapping):
                    raise ValueError(f"patch section must be an object: {section}")
                current = raw.setdefault(section, {})
                if not isinstance(current, dict):
                    raise ValueError(f"config section is not an object: {section}")
                current.update(dict(values))
            with self.config_path.open("w", encoding="utf-8") as fh:
                yaml.safe_dump(raw, fh, sort_keys=False)
            self.reload()
            return self.config_summary()
        except MeshNetError:
            raise
        except Exception as exc:
            raise as_meshnet_error(exc, "config") from exc

    def preflight(self, *, verify_radio: bool = True) -> JsonDict:
        cfg = self.reload()
        result = preflight_check(cfg, verify_radio=verify_radio)
        error = None
        if not result.ok:
            error = as_meshnet_error(
                RuntimeError("; ".join(result.errors) or "preflight failed"),
                "preflight",
            )
        return {
            "ok": result.ok,
            "port": result.port,
            "meshtastic_cli": result.meshtastic_cli,
            "errors": list(result.errors),
            "error_code": error.code if error else "",
            "action": error.action if error else "",
            "retryable": error.retryable if error else False,
        }

    def setup_radio(self, *, run_preflight: bool = True) -> ApiResult:
        cfg = self.reload()
        try:
            attempts = setup_radio_reliably(cfg, run_preflight=run_preflight)
            return ApiResult(
                True,
                {"configured": True, "config": self.config_summary()},
                attempts=attempts,
            )
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            return ApiResult.failure(as_meshnet_error(exc, "setup"))

    def status(self, *, recent_limit: int = 5) -> JsonDict:
        cfg = self.reload()
        store = StateStore.for_config(cfg)
        try:
            peer = store.get_node(cfg.peer_node_id)
            recent = store.recent_outbound(recent_limit)
            return {
                "role": cfg.app.role,
                "node_id": cfg.app.node_id,
                "peer_node_id": cfg.peer_node_id,
                "fingerprint": config_fingerprint(cfg),
                "telegram_configured": cfg.telegram.configured,
                "peer": _node_to_dict(peer) if peer else None,
                "recent_outbound": [_outbound_to_dict(row) for row in recent],
            }
        finally:
            store.close()

    def registry(self) -> list[JsonDict]:
        cfg = self.reload()
        store = StateStore.for_config(cfg)
        try:
            return [_node_to_dict(node) for node in store.list_nodes()]
        finally:
            store.close()

    def recent_outbound(self, limit: int = 20) -> list[JsonDict]:
        cfg = self.reload()
        store = StateStore.for_config(cfg)
        try:
            return [_outbound_to_dict(row) for row in store.recent_outbound(limit)]
        finally:
            store.close()

    def trust_node(self, app_id: str, mesh_id: str) -> JsonDict:
        cfg = self.reload()
        store = StateStore.for_config(cfg)
        try:
            store.trust_node(app_id, mesh_id)
            node = store.get_node(app_id)
            return _node_to_dict(node) if node else {"app_id": app_id, "mesh_id": mesh_id}
        finally:
            store.close()

    def unpair_node(self, app_id: str) -> JsonDict:
        cfg = self.reload()
        store = StateStore.for_config(cfg)
        try:
            store.remove_node(app_id)
            return {"removed": app_id}
        finally:
            store.close()

    def detect_ports(self) -> list[str]:
        return detect_serial_ports()

    def discover(
        self,
        timeout_seconds: int | None = None,
        *,
        run_preflight: bool = True,
    ) -> list[JsonDict]:
        return self.discover_report(
            timeout_seconds,
            run_preflight=run_preflight,
        ).nodes

    def discover_report(
        self,
        timeout_seconds: int | None = None,
        *,
        run_preflight: bool = True,
    ) -> DiscoveryReport:
        cfg = self.reload()
        node: MasterNode | None = None
        try:
            node = MasterNode(cfg, "api")
            if run_preflight:
                require_preflight(cfg)
            node.connect()
            found = node.discover(timeout_seconds or cfg.runtime.discovery_timeout_seconds)
            nodes = [_envelope_to_dict(env) for env in found]
            if nodes:
                return DiscoveryReport(True, nodes, node.last_discovery_attempts, "found")
            error = MeshNetError(
                "DISCOVERY_TIMEOUT",
                "discovery",
                "no compatible node replied",
                "Start the peer and check matching region, channel, PSK, network ID, and antennas.",
                retryable=True,
                attempts=node.last_discovery_attempts,
            )
            return DiscoveryReport(
                False,
                [],
                error.attempts,
                "not_found",
                error.message,
                error.code,
                error.action,
                error.retryable,
            )
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            error = as_meshnet_error(exc, "discovery")
            return DiscoveryReport(
                False,
                [],
                error.attempts,
                "failed",
                error.message,
                error.code,
                error.action,
                error.retryable,
            )
        finally:
            if node is not None:
                node.close()

    def send_message(
        self,
        text: str,
        dst: str | None = None,
        *,
        run_preflight: bool = True,
    ) -> DeliveryReport:
        cfg = self.reload()
        target = dst or cfg.peer_node_id
        node: MasterNode | None = None
        try:
            node = MasterNode(cfg, "api")
            if run_preflight:
                require_preflight(cfg)
            node.connect()
            result = node.send_reliable(
                "text",
                dst=target,
                body=text,
                expect_reply_type="text_ack",
            )
            return _delivery_to_report(result)
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            return _failed_delivery(cfg, "text", target, exc)
        finally:
            if node is not None:
                node.close()

    def ping(self, dst: str | None = None, *, run_preflight: bool = True) -> DeliveryReport:
        cfg = self.reload()
        target = dst or cfg.peer_node_id
        node: MasterNode | None = None
        try:
            node = MasterNode(cfg, "api")
            if run_preflight:
                require_preflight(cfg)
            node.connect()
            result = node.send_reliable(
                "ping",
                dst=target,
                body={"target": target},
                expect_reply_type="pong",
            )
            return _delivery_to_report(result)
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            return _failed_delivery(cfg, "ping", target, exc)
        finally:
            if node is not None:
                node.close()

    def send_raw(
        self,
        message_type: str,
        dst: str,
        body: Any = "",
        *,
        expect_reply_type: str | None = None,
        run_preflight: bool = True,
    ) -> DeliveryReport:
        cfg = self.reload()
        node: MasterNode | None = None
        try:
            node = MasterNode(cfg, "api")
            if run_preflight:
                require_preflight(cfg)
            node.connect()
            result = node.send_reliable(
                message_type,
                dst=dst,
                body=body,
                expect_reply_type=expect_reply_type,
            )
            return _delivery_to_report(result)
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            return _failed_delivery(cfg, message_type, dst, exc)
        finally:
            if node is not None:
                node.close()

    def run_tests(self, *, run_preflight: bool = True) -> ApiResult:
        cfg = self.reload()
        try:
            if run_preflight:
                require_preflight(cfg)
            passed = run_tests(cfg)
            if passed:
                return ApiResult(True, {"tested": True, "passed": True})
            error = MeshNetError(
                "TEST_FAILED",
                "test",
                "one or more MeshNet tests failed",
                "Review the failed test logs and correct the reported radio or peer problem.",
            )
            return ApiResult.failure(error, {"tested": True, "passed": False})
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            return ApiResult.failure(as_meshnet_error(exc, "test"))

    def run_master(self) -> None:
        cfg = self.reload()
        run_master(cfg)

    def run_slave(self) -> None:
        cfg = self.reload()
        run_slave(cfg)

    def run_runtime(self) -> None:
        cfg = self.reload()
        if cfg.app.role == "master":
            self.run_master()
        else:
            self.run_slave()


def _delivery_to_report(result: Any) -> DeliveryReport:
    env = result.envelope
    reply = None
    if result.reply is not None:
        reply = {
            "envelope": _envelope_to_dict(result.reply.envelope),
            "packet": to_jsonable(result.reply.packet),
        }
    return DeliveryReport(
        ok=bool(result.ok),
        message_id=env.id,
        message_type=env.t,
        src=env.src,
        dst=env.dst,
        ack_for=env.ack_for,
        seq=env.seq,
        attempts=int(result.attempts),
        status=str(result.status),
        last_error=str(result.last_error or ""),
        reply=reply,
        error_code=str(result.error_code or ""),
        action=str(result.action or ""),
        retryable=bool(result.retryable),
    )


def _failed_delivery(
    cfg: MeshConfig,
    message_type: str,
    dst: str,
    exc: BaseException,
) -> DeliveryReport:
    error = as_meshnet_error(exc, "delivery")
    return DeliveryReport(
        ok=False,
        message_id="",
        message_type=message_type,
        src=cfg.app.node_id,
        dst=dst,
        ack_for="",
        seq=0,
        attempts=error.attempts,
        status="failed",
        last_error=error.message,
        error_code=error.code,
        action=error.action,
        retryable=error.retryable,
    )


def _envelope_to_dict(env: Any) -> JsonDict:
    return {
        "version": env.v,
        "network_id": env.n,
        "type": env.t,
        "id": env.id,
        "src": env.src,
        "dst": env.dst,
        "seq": env.seq,
        "attempt": env.attempt,
        "ack_for": env.ack_for,
        "timestamp": env.ts,
        "body": to_jsonable(env.body),
        "hmac": env.h,
    }


def _node_to_dict(node: Any) -> JsonDict:
    if node is None:
        return {}
    return {
        "app_id": node.app_id,
        "mesh_id": node.mesh_id,
        "role": node.role,
        "name": node.name,
        "short_name": node.short_name,
        "config_fingerprint": node.config_fingerprint,
        "first_seen": node.first_seen,
        "last_seen": node.last_seen,
        "last_seen_age": format_age(node.last_seen),
        "rssi": node.rssi,
        "snr": node.snr,
        "trusted": node.trusted,
        "identity_changed": node.identity_changed,
    }


def _outbound_to_dict(row: Any) -> JsonDict:
    return {
        "message_id": row["message_id"],
        "message_type": row["message_type"],
        "src": row["src"],
        "dst": row["dst"],
        "ack_for": row["ack_for"],
        "seq": row["seq"],
        "status": row["status"],
        "attempts": row["attempts"],
        "radio_dest": row["radio_dest"],
        "radio_packet_id": row["radio_packet_id"],
        "last_error": row["last_error"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "updated_age": format_age(float(row["updated_at"])),
        "body_json": row["body_json"],
    }
