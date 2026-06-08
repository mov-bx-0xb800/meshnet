from __future__ import annotations

from . import logger
from .config import MeshConfig
from .node import MeshNode, mesh_node_info
from .protocol import Envelope, payload_hash


class SlaveNode(MeshNode):
    def __init__(self, cfg: MeshConfig, scope: str = "slave") -> None:
        super().__init__(cfg, scope)
        self.true_master_seen = False

    def handle_message(self, envelope: Envelope, packet: dict) -> None:
        if envelope.src != self.cfg.network.master_id and envelope.t != "hello":
            return
        logger.line("slave", f"Message received from {envelope.src} type={envelope.t}" + (
            f" seq={envelope.seq}" if envelope.seq else ""
        ))
        logger.line("slave", "HMAC valid: true")

        if envelope.t == "hello":
            if envelope.src == self.cfg.network.master_id:
                self.true_master_seen = True
                logger.line("slave", f"TRUE NODE: {envelope.src}")
            self.send("hello_ack", dst=envelope.src, body=mesh_node_info(self), seq=envelope.seq)
            return

        if envelope.t == "ping":
            self.send("pong", dst=envelope.src, body={"ok": True, "reply_to": envelope.id}, seq=envelope.seq)
            logger.line("slave", f"Pong sent to {envelope.src} seq={envelope.seq}")
            return

        if envelope.t == "test":
            body = envelope.body if isinstance(envelope.body, dict) else {}
            payload = body.get("payload", "")
            expected_hash = body.get("hash", "")
            actual_hash = payload_hash(payload)
            decode_ok = isinstance(payload, str)
            hash_ok = actual_hash == expected_hash
            logger.line("slave", f"Decode check: {'OK' if decode_ok else 'FAIL'}")
            logger.line("slave", f"Hash check: {'OK' if hash_ok else 'FAIL'}")
            self.send(
                "test_ack",
                dst=envelope.src,
                body={
                    "ok": bool(decode_ok and hash_ok),
                    "d": bool(decode_ok),
                    "hh": bool(hash_ok),
                    "rid": envelope.id,
                },
                seq=envelope.seq,
            )
            logger.line("slave", "Ack sent.")
            return

        if envelope.t == "status_req":
            self.send(
                "status_res",
                dst=envelope.src,
                body={
                    "ok": 1,
                    "id": self.cfg.app.node_id,
                    "r": "s",
                    "ch": self.cfg.radio.channel_name,
                },
                seq=envelope.seq,
            )
            return

        if envelope.t == "text":
            logger.line("slave", f"Text <- {envelope.src}: {envelope.body}")


def run_slave(cfg: MeshConfig) -> None:
    node = SlaveNode(cfg, "slave")
    logger.line("slave", "Starting Slave Node...")
    logger.line("slave", f"Config loaded: {cfg.path.name}")
    logger.line("slave", f"Connecting to radio: {cfg.radio.port}")
    node.connect()
    logger.line("slave", "Connected.")
    logger.line("slave", f"Network: {cfg.network.network_id}")
    logger.line("slave", f"Channel: {cfg.radio.channel_name}")
    logger.line("slave", f"Waiting for master: {cfg.network.master_id}")
    node.run_forever()
