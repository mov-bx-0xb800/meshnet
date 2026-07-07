from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import MeshConfig, channel_psk_for_cli


STATE_SCHEMA_VERSION = 1
SEEN_TTL_SECONDS = 24 * 60 * 60


@dataclass(frozen=True)
class RegistryNode:
    app_id: str
    mesh_id: str
    role: str
    name: str
    short_name: str
    config_fingerprint: str
    first_seen: float
    last_seen: float
    rssi: Any = None
    snr: Any = None
    trusted: bool = True
    identity_changed: bool = False


def state_path_for_config(cfg: MeshConfig) -> Path:
    return cfg.path.with_suffix(".state.sqlite")


def config_fingerprint(cfg: MeshConfig) -> str:
    material = {
        "region": cfg.radio.region,
        "modem": cfg.radio.modem_preset,
        "channel_index": cfg.radio.channel_index,
        "channel": cfg.radio.channel_name,
        "psk": channel_psk_for_cli(cfg),
        "hop_limit": cfg.radio.hop_limit,
        "frequency_slot": cfg.radio.frequency_slot,
        "tx_power": cfg.radio.tx_power,
        "ignore_mqtt": cfg.radio.ignore_mqtt,
        "ok_to_mqtt": cfg.radio.ok_to_mqtt,
        "device_role": cfg.device.role,
        "rebroadcast_mode": cfg.device.rebroadcast_mode,
        "serial_enabled": cfg.device.serial_enabled,
        "network": cfg.network.network_id,
    }
    encoded = json.dumps(material, sort_keys=True, separators=(",", ":")).encode("utf-8")
    import hashlib

    return hashlib.sha256(encoded).hexdigest()[:16]


class StateStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    @classmethod
    def for_config(cls, cfg: MeshConfig) -> "StateStore":
        return cls(state_path_for_config(cfg))

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    def _init_schema(self) -> None:
        with self._lock:
            self.conn.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS nodes (
                    app_id TEXT PRIMARY KEY,
                    mesh_id TEXT NOT NULL,
                    role TEXT NOT NULL DEFAULT '',
                    name TEXT NOT NULL DEFAULT '',
                    short_name TEXT NOT NULL DEFAULT '',
                    config_fingerprint TEXT NOT NULL DEFAULT '',
                    first_seen REAL NOT NULL,
                    last_seen REAL NOT NULL,
                    rssi TEXT,
                    snr TEXT,
                    trusted INTEGER NOT NULL DEFAULT 1,
                    identity_changed INTEGER NOT NULL DEFAULT 0,
                    raw_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE TABLE IF NOT EXISTS seen_messages (
                    src TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    message_type TEXT NOT NULL,
                    first_seen REAL NOT NULL,
                    PRIMARY KEY (src, message_id)
                );
                CREATE TABLE IF NOT EXISTS outbound_messages (
                    message_id TEXT PRIMARY KEY,
                    message_type TEXT NOT NULL,
                    src TEXT NOT NULL,
                    dst TEXT NOT NULL,
                    ack_for TEXT NOT NULL DEFAULT '',
                    seq INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    radio_dest TEXT NOT NULL DEFAULT '',
                    radio_packet_id TEXT NOT NULL DEFAULT '',
                    last_error TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    body_json TEXT NOT NULL DEFAULT 'null'
                );
                """
            )
            self.conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
                (str(STATE_SCHEMA_VERSION),),
            )
            self.conn.commit()

    def upsert_node(
        self,
        *,
        app_id: str,
        mesh_id: str,
        role: str = "",
        name: str = "",
        short_name: str = "",
        config_fingerprint: str = "",
        rssi: Any = None,
        snr: Any = None,
        raw: dict[str, Any] | None = None,
    ) -> str:
        with self._lock:
            now = time.time()
            existing = self.conn.execute(
                "SELECT mesh_id, identity_changed FROM nodes WHERE app_id = ?",
                (app_id,),
            ).fetchone()
            raw_json = json.dumps(raw or {}, sort_keys=True, default=str)
            if existing is not None and existing["mesh_id"] != mesh_id:
                self.conn.execute(
                    """
                    UPDATE nodes
                       SET identity_changed = 1,
                           last_seen = ?,
                           rssi = ?,
                           snr = ?,
                           raw_json = ?
                     WHERE app_id = ?
                    """,
                    (now, _json_scalar(rssi), _json_scalar(snr), raw_json, app_id),
                )
                self.conn.commit()
                return "identity_changed"

            if existing is None:
                self.conn.execute(
                    """
                    INSERT INTO nodes (
                        app_id, mesh_id, role, name, short_name, config_fingerprint,
                        first_seen, last_seen, rssi, snr, trusted, identity_changed, raw_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 0, ?)
                    """,
                    (
                        app_id,
                        mesh_id,
                        role,
                        name,
                        short_name,
                        config_fingerprint,
                        now,
                        now,
                        _json_scalar(rssi),
                        _json_scalar(snr),
                        raw_json,
                    ),
                )
                self.conn.commit()
                return "new"

            self.conn.execute(
                """
                UPDATE nodes
                   SET role = COALESCE(NULLIF(?, ''), role),
                       name = COALESCE(NULLIF(?, ''), name),
                       short_name = COALESCE(NULLIF(?, ''), short_name),
                       config_fingerprint = COALESCE(NULLIF(?, ''), config_fingerprint),
                       last_seen = ?,
                       rssi = ?,
                       snr = ?,
                       raw_json = ?
                 WHERE app_id = ?
                """,
                (
                    role,
                    name,
                    short_name,
                    config_fingerprint,
                    now,
                    _json_scalar(rssi),
                    _json_scalar(snr),
                    raw_json,
                    app_id,
                ),
            )
            self.conn.commit()
            return "updated"

    def get_node(self, app_id: str) -> RegistryNode | None:
        with self._lock:
            row = self.conn.execute("SELECT * FROM nodes WHERE app_id = ?", (app_id,)).fetchone()
        return _node_from_row(row) if row else None

    def get_mesh_id(self, app_id: str) -> str | None:
        node = self.get_node(app_id)
        if node is None or node.identity_changed or not node.trusted:
            return None
        return node.mesh_id

    def list_nodes(self) -> list[RegistryNode]:
        with self._lock:
            rows = self.conn.execute("SELECT * FROM nodes ORDER BY last_seen DESC").fetchall()
        return [_node_from_row(row) for row in rows]

    def trust_node(self, app_id: str, mesh_id: str) -> None:
        now = time.time()
        with self._lock:
            self.conn.execute(
                """
                INSERT INTO nodes(app_id, mesh_id, first_seen, last_seen, trusted, identity_changed)
                VALUES(?, ?, ?, ?, 1, 0)
                ON CONFLICT(app_id) DO UPDATE SET
                    mesh_id = excluded.mesh_id,
                    trusted = 1,
                    identity_changed = 0,
                    last_seen = excluded.last_seen
                """,
                (app_id, mesh_id, now, now),
            )
            self.conn.commit()

    def remove_node(self, app_id: str) -> None:
        with self._lock:
            self.conn.execute("DELETE FROM nodes WHERE app_id = ?", (app_id,))
            self.conn.commit()

    def mark_seen(self, src: str, message_id: str, message_type: str) -> bool:
        now = time.time()
        self.prune_seen(now - SEEN_TTL_SECONDS)
        try:
            with self._lock:
                self.conn.execute(
                    """
                    INSERT INTO seen_messages(src, message_id, message_type, first_seen)
                    VALUES(?, ?, ?, ?)
                    """,
                    (src, message_id, message_type, now),
                )
                self.conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def prune_seen(self, older_than: float) -> None:
        with self._lock:
            self.conn.execute("DELETE FROM seen_messages WHERE first_seen < ?", (older_than,))
            self.conn.commit()

    def record_outbound(
        self,
        *,
        message_id: str,
        message_type: str,
        src: str,
        dst: str,
        ack_for: str,
        seq: int,
        status: str,
        attempts: int,
        radio_dest: str,
        radio_packet_id: str = "",
        last_error: str = "",
        body: Any = None,
    ) -> None:
        now = time.time()
        body_json = json.dumps(body, sort_keys=True, default=str)
        with self._lock:
            self.conn.execute(
                """
                INSERT INTO outbound_messages(
                    message_id, message_type, src, dst, ack_for, seq, status,
                    attempts, radio_dest, radio_packet_id, last_error, created_at,
                    updated_at, body_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(message_id) DO UPDATE SET
                    status = excluded.status,
                    attempts = excluded.attempts,
                    radio_dest = excluded.radio_dest,
                    radio_packet_id = excluded.radio_packet_id,
                    last_error = excluded.last_error,
                    updated_at = excluded.updated_at
                """,
                (
                    message_id,
                    message_type,
                    src,
                    dst,
                    ack_for,
                    seq,
                    status,
                    attempts,
                    radio_dest,
                    radio_packet_id,
                    last_error,
                    now,
                    now,
                    body_json,
                ),
            )
            self.conn.commit()

    def recent_outbound(self, limit: int = 20) -> list[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(
                "SELECT * FROM outbound_messages ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()


def _json_scalar(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, default=str)


def _load_json_scalar(value: str | None) -> Any:
    if value is None:
        return None
    try:
        return json.loads(value)
    except Exception:
        return value


def _node_from_row(row: sqlite3.Row) -> RegistryNode:
    return RegistryNode(
        app_id=row["app_id"],
        mesh_id=row["mesh_id"],
        role=row["role"],
        name=row["name"],
        short_name=row["short_name"],
        config_fingerprint=row["config_fingerprint"],
        first_seen=float(row["first_seen"]),
        last_seen=float(row["last_seen"]),
        rssi=_load_json_scalar(row["rssi"]),
        snr=_load_json_scalar(row["snr"]),
        trusted=bool(row["trusted"]),
        identity_changed=bool(row["identity_changed"]),
    )
