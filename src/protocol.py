from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass
from typing import Any

from .config import MeshConfig


PROTOCOL_VERSION = 2
HMAC_HEX_CHARS = 16
MAX_ENCODED_CHARS = 230
MESSAGE_TYPES = {
    "hello",
    "hello_ack",
    "heartbeat",
    "ping",
    "pong",
    "text",
    "text_ack",
    "test",
    "test_ack",
    "status_req",
    "status_res",
    "error",
}


@dataclass(frozen=True)
class Envelope:
    v: int
    n: str
    t: str
    id: str
    src: str
    dst: str
    seq: int
    attempt: int
    ack_for: str
    ts: int
    body: Any
    h: str

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Envelope":
        return cls(
            v=int(data["v"]),
            n=str(data["n"]),
            t=str(data["t"]),
            id=str(data.get("i", data.get("id", ""))),
            src=str(data.get("s", data.get("src", ""))),
            dst=str(data.get("d", data.get("dst", ""))),
            seq=int(data.get("q", data.get("seq", 0))),
            attempt=int(data.get("a", data.get("attempt", 1))),
            ack_for=str(data.get("af", data.get("ack_for", ""))),
            ts=int(data["ts"]),
            body=data.get("b", data.get("body", "")),
            h=str(data["h"]),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "v": self.v,
            "n": self.n,
            "t": self.t,
            "i": self.id,
            "s": self.src,
            "d": self.dst,
            "q": self.seq,
            "a": self.attempt,
            "af": self.ack_for,
            "ts": self.ts,
            "b": self.body,
            "h": self.h,
        }


def canonical_json(data: dict[str, Any]) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def payload_hash(body: Any) -> str:
    if isinstance(body, str):
        encoded = body.encode("utf-8")
    else:
        encoded = canonical_json({"body": body}).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:16]


def sign_dict(data_without_h: dict[str, Any], password: str) -> str:
    encoded = canonical_json(data_without_h).encode("utf-8")
    key = hashlib.sha256(f"meshnet-hmac:{password}".encode("utf-8")).digest()
    digest = hmac.new(key, encoded, hashlib.sha256).hexdigest()
    return digest[:HMAC_HEX_CHARS]


def make_message(
    cfg: MeshConfig,
    message_type: str,
    dst: str = "broadcast",
    seq: int = 0,
    body: Any = "",
    message_id: str | None = None,
    attempt: int = 1,
    ack_for: str = "",
) -> Envelope:
    if message_type not in MESSAGE_TYPES:
        raise ValueError(f"unsupported message type: {message_type}")
    if isinstance(body, str) and len(body) > cfg.runtime.max_payload_chars:
        raise ValueError(
            f"body is too long: {len(body)} > {cfg.runtime.max_payload_chars} chars"
        )
    data = {
        "v": PROTOCOL_VERSION,
        "n": cfg.network.network_id,
        "t": message_type,
        "i": message_id or secrets.token_hex(3),
        "s": cfg.app.node_id,
        "d": dst,
        "q": int(seq),
        "a": int(attempt),
        "af": ack_for,
        "ts": int(time.time()),
        "b": body,
    }
    data["h"] = sign_dict(data, cfg.network.network_password)
    return Envelope.from_dict(data)


def encode_envelope(envelope: Envelope) -> str:
    encoded = canonical_json(envelope.as_dict())
    if len(encoded) > MAX_ENCODED_CHARS:
        raise ValueError(
            f"encoded message is too long: {len(encoded)} > {MAX_ENCODED_CHARS} chars"
        )
    return encoded


def decode_envelope(text: str) -> Envelope:
    raw = json.loads(text)
    if not isinstance(raw, dict):
        raise ValueError("message is not a JSON object")
    required = {"v", "n", "t", "i", "s", "d", "q", "a", "af", "ts", "b", "h"}
    legacy_required = {
        "v",
        "n",
        "t",
        "id",
        "src",
        "dst",
        "seq",
        "attempt",
        "ack_for",
        "ts",
        "body",
        "h",
    }
    if required <= set(raw):
        missing: list[str] = []
    else:
        missing = sorted(legacy_required - set(raw))
    if missing:
        raise ValueError(f"missing fields: {', '.join(missing)}")
    if raw["t"] not in MESSAGE_TYPES:
        raise ValueError(f"unsupported message type: {raw['t']}")
    return Envelope.from_dict(raw)


def verify_envelope(envelope: Envelope, cfg: MeshConfig) -> tuple[bool, str]:
    if envelope.v != PROTOCOL_VERSION:
        return False, "unsupported protocol version"
    if envelope.n != cfg.network.network_id:
        return False, "wrong network_id"
    data = envelope.as_dict()
    sent_hmac = data.pop("h", "")
    expected = sign_dict(data, cfg.network.network_password)
    if not hmac.compare_digest(sent_hmac, expected):
        return False, "invalid HMAC"
    return True, "ok"


def locally_rejects_fake_hmac(cfg: MeshConfig) -> bool:
    msg = make_message(cfg, "hello", body="local validation")
    data = msg.as_dict()
    data["h"] = "0000000000000000"
    forged = Envelope.from_dict(data)
    ok, reason = verify_envelope(forged, cfg)
    return (not ok) and reason == "invalid HMAC"
