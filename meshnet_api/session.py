from __future__ import annotations

from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from src import logger
from src.config import MeshConfig, load_config
from src.errors import MeshNetError, as_meshnet_error
from src.master import MasterNode
from src.node import MeshNode
from src.preflight import require_preflight
from src.slave import SlaveNode

from .client import _delivery_to_report, _envelope_to_dict, _failed_delivery
from .models import DeliveryReport, JsonDict, to_jsonable


class MeshNetSession:
    """Long-lived package API for apps that keep the radio open."""

    def __init__(
        self,
        config_path: str | Path = "config.master.yaml",
        *,
        run_preflight: bool = True,
        scope: str = "api",
    ) -> None:
        load_dotenv()
        self.config_path = Path(config_path).expanduser()
        try:
            self.cfg: MeshConfig = load_config(self.config_path)
        except Exception as exc:
            raise as_meshnet_error(exc, "config") from exc
        logger.configure_logging(self.cfg.app.log_level)
        self.run_preflight = run_preflight
        self.scope = scope
        self.node: MeshNode | None = None

    def __enter__(self) -> "MeshNetSession":
        self.connect()
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    @property
    def connected(self) -> bool:
        return self.node is not None and self.node.radio.is_connected()

    def connect(self) -> None:
        if self.node is not None:
            return
        try:
            self.cfg = load_config(self.config_path)
        except Exception as exc:
            raise as_meshnet_error(exc, "config") from exc
        if self.run_preflight:
            require_preflight(self.cfg)
        node: MeshNode | None = None
        try:
            if self.cfg.app.role == "slave":
                node = SlaveNode(self.cfg, self.scope)
            else:
                node = MasterNode(self.cfg, self.scope)
            node.connect()
        except Exception as exc:
            if node is not None:
                node.close()
            raise as_meshnet_error(exc, "connect") from exc
        self.node = node

    def close(self) -> None:
        if self.node is not None:
            self.node.close()
            self.node = None

    def reload_config(self) -> MeshConfig:
        try:
            self.cfg = load_config(self.config_path)
        except Exception as exc:
            raise as_meshnet_error(exc, "config") from exc
        return self.cfg

    def reconnect(self) -> None:
        self.close()
        self.connect()

    def local_mesh_id(self) -> str:
        return self._node().radio.local_mesh_id()

    def known_radio_nodes(self) -> list[JsonDict]:
        return [to_jsonable(node) for node in self._node().radio.known_nodes()]

    def discover(self, timeout_seconds: int | None = None) -> list[JsonDict]:
        node = self._node()
        if not isinstance(node, MasterNode):
            raise MeshNetError(
                "ROLE_NOT_SUPPORTED",
                "discovery",
                "discovery must be started from a master session",
                "Use config.master.yaml for discovery.",
            )
        found = node.discover(timeout_seconds or self.cfg.runtime.discovery_timeout_seconds)
        return [_envelope_to_dict(env) for env in found]

    def send_message(self, text: str, dst: str | None = None) -> DeliveryReport:
        target = dst or self.cfg.peer_node_id
        try:
            result = self._node().send_reliable(
                "text",
                dst=target,
                body=text,
                expect_reply_type="text_ack",
            )
            return _delivery_to_report(result)
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            return _failed_delivery(self.cfg, "text", target, exc)

    def ping(self, dst: str | None = None) -> DeliveryReport:
        target = dst or self.cfg.peer_node_id
        try:
            result = self._node().send_reliable(
                "ping",
                dst=target,
                body={"target": target},
                expect_reply_type="pong",
            )
            return _delivery_to_report(result)
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            return _failed_delivery(self.cfg, "ping", target, exc)

    def send_raw(
        self,
        message_type: str,
        dst: str,
        body: Any = "",
        *,
        expect_reply_type: str | None = None,
    ) -> DeliveryReport:
        try:
            result = self._node().send_reliable(
                message_type,
                dst=dst,
                body=body,
                expect_reply_type=expect_reply_type,
            )
            return _delivery_to_report(result)
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            return _failed_delivery(self.cfg, message_type, dst, exc)

    def wait_for_message(
        self,
        *,
        message_type: str | None = None,
        src: str | None = None,
        message_id: str | None = None,
        ack_for: str | None = None,
        timeout_seconds: int = 30,
    ) -> JsonDict | None:
        item = self._node().wait_for_message(
            message_type=message_type,
            src=src,
            message_id=message_id,
            ack_for=ack_for,
            timeout_seconds=timeout_seconds,
        )
        if item is None:
            return None
        return {
            "envelope": _envelope_to_dict(item.envelope),
            "packet": to_jsonable(item.packet),
        }

    def _node(self) -> MeshNode:
        if self.node is None:
            raise RuntimeError("MeshNetSession is not connected")
        if not self.node.radio.is_connected():
            self.reconnect()
            if self.node is None:
                raise MeshNetError(
                    "RADIO_DISCONNECTED",
                    "connect",
                    "the radio could not be reconnected",
                    "Check USB power and cable, then retry.",
                    retryable=True,
                )
        return self.node
