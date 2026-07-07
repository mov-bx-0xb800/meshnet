from __future__ import annotations

import time
from dataclasses import asdict, dataclass, is_dataclass
from pathlib import Path
from typing import Any

from src.errors import MeshNetError


JsonDict = dict[str, Any]


@dataclass(frozen=True)
class ApiResult:
    ok: bool
    data: JsonDict
    error: str = ""
    error_code: str = ""
    stage: str = ""
    action: str = ""
    retryable: bool = False
    attempts: int = 0

    def to_dict(self) -> JsonDict:
        return to_jsonable(asdict(self))

    @classmethod
    def failure(cls, error: MeshNetError, data: JsonDict | None = None) -> "ApiResult":
        payload = dict(data or {})
        if error.details:
            payload["details"] = to_jsonable(error.details)
        return cls(
            False,
            payload,
            error.message,
            error.code,
            error.stage,
            error.action,
            error.retryable,
            error.attempts,
        )


@dataclass(frozen=True)
class DeliveryReport:
    ok: bool
    message_id: str
    message_type: str
    src: str
    dst: str
    ack_for: str
    seq: int
    attempts: int
    status: str
    last_error: str = ""
    reply: JsonDict | None = None
    error_code: str = ""
    action: str = ""
    retryable: bool = False

    def to_dict(self) -> JsonDict:
        return to_jsonable(asdict(self))


@dataclass(frozen=True)
class DiscoveryReport:
    ok: bool
    nodes: list[JsonDict]
    attempts: int
    status: str
    last_error: str = ""
    error_code: str = ""
    action: str = ""
    retryable: bool = False

    def to_dict(self) -> JsonDict:
        return to_jsonable(asdict(self))


def to_jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value):
        return to_jsonable(asdict(value))
    if isinstance(value, dict):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [to_jsonable(v) for v in value]
    if hasattr(value, "keys") and hasattr(value, "__getitem__"):
        return {str(k): to_jsonable(value[k]) for k in value.keys()}
    return str(value)


def format_age(timestamp: float) -> str:
    seconds = max(0, int(time.time() - timestamp))
    if seconds < 60:
        return f"{seconds}s ago"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {seconds}s ago"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes}m ago"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h ago"
