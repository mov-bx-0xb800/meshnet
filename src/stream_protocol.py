from __future__ import annotations

import hashlib
import hmac
import struct
from dataclasses import dataclass
from enum import IntEnum

from .config import MeshConfig
from .radio import MAX_MESHTASTIC_DATA_BYTES


MAGIC = b"MF"
VERSION = 1
TAG_BYTES = 16
HEADER = struct.Struct("!2sBBBBIIIIH")
MAX_STREAM_PAYLOAD = MAX_MESHTASTIC_DATA_BYTES - HEADER.size - TAG_BYTES

FLAG_ACK_REQUIRED = 0x01
FLAG_EMPTY = 0x02


class FrameType(IntEnum):
    OPEN = 1
    OPEN_OK = 2
    DATA = 3
    ACK = 4
    POLL = 5
    POLL_DONE = 6
    HALF_CLOSE = 7
    CLOSE = 8
    RESET = 9
    PING = 10


@dataclass(frozen=True)
class StreamFrame:
    frame_type: FrameType
    session_id: int
    sequence: int = 0
    ack: int = 0
    sack: int = 0
    flags: int = 0
    payload: bytes = b""


class StreamProtocolError(ValueError):
    pass


def derive_stream_key(cfg: MeshConfig) -> bytes:
    material = (
        f"meshnet-flower-stream-v{VERSION}:"
        f"{cfg.network.network_id}:{cfg.network.network_password}"
    ).encode("utf-8")
    return hashlib.sha256(material).digest()


def is_stream_payload(payload: bytes) -> bool:
    return len(payload) >= len(MAGIC) and payload[: len(MAGIC)] == MAGIC


def encode_frame(frame: StreamFrame, key: bytes) -> bytes:
    if not 0 < frame.session_id <= 0xFFFFFFFF:
        raise StreamProtocolError("session_id must be a non-zero uint32")
    for name, value in (
        ("sequence", frame.sequence),
        ("ack", frame.ack),
        ("sack", frame.sack),
    ):
        if not 0 <= value <= 0xFFFFFFFF:
            raise StreamProtocolError(f"{name} must be a uint32")
    if not isinstance(frame.payload, bytes):
        raise StreamProtocolError("frame payload must be bytes")
    if len(frame.payload) > MAX_STREAM_PAYLOAD:
        raise StreamProtocolError(
            f"stream payload is too large: {len(frame.payload)} > {MAX_STREAM_PAYLOAD}"
        )
    header = HEADER.pack(
        MAGIC,
        VERSION,
        int(frame.frame_type),
        frame.flags & 0xFF,
        0,
        frame.session_id,
        frame.sequence,
        frame.ack,
        frame.sack,
        len(frame.payload),
    )
    body = header + frame.payload
    tag = hmac.new(key, body, hashlib.sha256).digest()[:TAG_BYTES]
    encoded = body + tag
    if len(encoded) > MAX_MESHTASTIC_DATA_BYTES:
        raise StreamProtocolError("encoded frame exceeds Meshtastic payload limit")
    return encoded


def decode_frame(encoded: bytes, key: bytes) -> StreamFrame:
    minimum = HEADER.size + TAG_BYTES
    if len(encoded) < minimum:
        raise StreamProtocolError("encoded frame is truncated")
    try:
        (
            magic,
            version,
            frame_type,
            flags,
            _reserved,
            session_id,
            sequence,
            ack,
            sack,
            payload_len,
        ) = HEADER.unpack(encoded[: HEADER.size])
    except struct.error as exc:
        raise StreamProtocolError("invalid stream frame header") from exc
    if magic != MAGIC:
        raise StreamProtocolError("not a Meshnet Flower stream frame")
    if version != VERSION:
        raise StreamProtocolError(f"unsupported stream protocol version: {version}")
    if session_id == 0:
        raise StreamProtocolError("session_id cannot be zero")
    expected_len = HEADER.size + payload_len + TAG_BYTES
    if len(encoded) != expected_len:
        raise StreamProtocolError(
            f"frame length mismatch: got {len(encoded)}, expected {expected_len}"
        )
    supplied_tag = encoded[-TAG_BYTES:]
    expected_tag = hmac.new(key, encoded[:-TAG_BYTES], hashlib.sha256).digest()[:TAG_BYTES]
    if not hmac.compare_digest(supplied_tag, expected_tag):
        raise StreamProtocolError("stream frame HMAC is invalid")
    try:
        kind = FrameType(frame_type)
    except ValueError as exc:
        raise StreamProtocolError(f"unknown stream frame type: {frame_type}") from exc
    payload = encoded[HEADER.size:-TAG_BYTES]
    return StreamFrame(
        frame_type=kind,
        session_id=session_id,
        sequence=sequence,
        ack=ack,
        sack=sack,
        flags=flags,
        payload=payload,
    )
