#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import random
import statistics
import struct
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.config import MeshConfig, load_config
from src.radio import MAX_MESHTASTIC_DATA_BYTES, RadioClient, packet_from_mesh_id


MAGIC = b"MLA1"
TYPE_PROBE = 1
TYPE_ECHO = 2
HEADER = struct.Struct("!4sBHHQ")
MIN_FRAME_BYTES = HEADER.size


@dataclass(frozen=True)
class AuditFrame:
    kind: int
    seq: int
    body_len: int
    token: int
    body: bytes


def encode_frame(kind: int, seq: int, body_len: int, token: int, body: bytes) -> bytes:
    return HEADER.pack(MAGIC, kind, seq, body_len, token) + body


def decode_frame(payload: bytes) -> AuditFrame | None:
    if len(payload) < MIN_FRAME_BYTES:
        return None
    try:
        magic, kind, seq, body_len, token = HEADER.unpack(payload[: HEADER.size])
    except struct.error:
        return None
    if magic != MAGIC or kind not in {TYPE_PROBE, TYPE_ECHO}:
        return None
    body = payload[HEADER.size:]
    if len(body) != body_len:
        return None
    return AuditFrame(kind=kind, seq=seq, body_len=body_len, token=token, body=body)


def make_payload(kind: int, seq: int, total_bytes: int, token: int) -> bytes:
    if total_bytes < MIN_FRAME_BYTES or total_bytes > MAX_MESHTASTIC_DATA_BYTES:
        raise ValueError(
            f"bytes must be between {MIN_FRAME_BYTES} and {MAX_MESHTASTIC_DATA_BYTES}"
        )
    body_len = total_bytes - HEADER.size
    body = bytes(((seq * 31 + index * 17 + token) & 0xFF) for index in range(body_len))
    return encode_frame(kind, seq, body_len, token, body)


def packet_metric(packet: dict[str, Any], *names: str) -> Any:
    for name in names:
        if name in packet and packet[name] is not None:
            return packet[name]
    rx_metadata = packet.get("rxMetadata")
    if isinstance(rx_metadata, list) and rx_metadata:
        first = rx_metadata[0]
        if isinstance(first, dict):
            for name in names:
                if name in first and first[name] is not None:
                    return first[name]
    return ""


class EchoWaiter:
    def __init__(self, expected_mesh: str, token: int) -> None:
        self.expected_mesh = expected_mesh.lower()
        self.token = token
        self.condition = threading.Condition()
        self.responses: dict[int, tuple[float, AuditFrame, dict[str, Any]]] = {}
        self.invalid = 0
        self.foreign = 0

    def handler(self, payload: bytes, packet: dict[str, Any]) -> None:
        mesh_id = packet_from_mesh_id(packet).lower()
        if mesh_id != self.expected_mesh:
            self.foreign += 1
            return
        frame = decode_frame(payload)
        if frame is None or frame.kind != TYPE_ECHO or frame.token != self.token:
            self.invalid += 1
            return
        with self.condition:
            self.responses[frame.seq] = (time.monotonic(), frame, packet)
            self.condition.notify_all()

    def wait_for(self, seq: int, timeout: float) -> tuple[float, AuditFrame, dict[str, Any]] | None:
        deadline = time.monotonic() + timeout
        with self.condition:
            while seq not in self.responses:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self.condition.wait(timeout=min(0.25, remaining))
            return self.responses.pop(seq)


class EchoServer:
    def __init__(self, cfg: MeshConfig, radio: RadioClient) -> None:
        self.cfg = cfg
        self.radio = radio
        self.lock = threading.Lock()
        self.started = time.monotonic()
        self.rx_probe = 0
        self.tx_echo = 0
        self.invalid = 0
        self.foreign = 0
        self.rx_bytes = 0
        self.tx_bytes = 0
        self.last_seq = ""
        self.last_peer = ""
        self.last_rssi = ""
        self.last_snr = ""

    def handler(self, payload: bytes, packet: dict[str, Any]) -> None:
        mesh_id = packet_from_mesh_id(packet).lower()
        peer_id = self.cfg.app_id_for_mesh(mesh_id)
        if peer_id is None:
            with self.lock:
                self.foreign += 1
            return
        frame = decode_frame(payload)
        if frame is None or frame.kind != TYPE_PROBE:
            with self.lock:
                self.invalid += 1
            return
        echo = encode_frame(TYPE_ECHO, frame.seq, frame.body_len, frame.token, frame.body)
        try:
            self.radio.send_bytes(echo, destination_id=mesh_id, want_ack=False)
            sent = True
        except Exception as exc:
            sent = False
            print(f"[echo] seq={frame.seq} rx_bytes={len(payload)} tx_failed={exc}", flush=True)
        with self.lock:
            self.rx_probe += 1
            self.rx_bytes += len(payload)
            self.last_seq = str(frame.seq)
            self.last_peer = mesh_id
            self.last_rssi = str(packet_metric(packet, "rxRssi", "rx_rssi", "rssi"))
            self.last_snr = str(packet_metric(packet, "rxSnr", "rx_snr", "snr"))
            if sent:
                self.tx_echo += 1
                self.tx_bytes += len(echo)
        if sent:
            print(
                "[echo] "
                f"seq={frame.seq} from={mesh_id} rx_bytes={len(payload)} "
                f"tx_bytes={len(echo)} rssi={self.last_rssi or '?'} snr={self.last_snr or '?'}",
                flush=True,
            )

    def print_status(self) -> None:
        with self.lock:
            print(
                "[status] "
                f"up={round(time.monotonic() - self.started, 1)}s "
                f"rx_probe={self.rx_probe} tx_echo={self.tx_echo} "
                f"rx_bytes={self.rx_bytes}B tx_bytes={self.tx_bytes}B "
                f"invalid={self.invalid} foreign={self.foreign} "
                f"last_seq={self.last_seq or '-'} last_peer={self.last_peer or '-'} "
                f"rssi={self.last_rssi or '?'} snr={self.last_snr or '?'}",
                flush=True,
            )


def peer_for(cfg: MeshConfig) -> tuple[str, str]:
    if not cfg.network.peers:
        raise ValueError("network.peers must contain the peer mesh_id")
    peer = cfg.network.peers[0]
    return peer.app_id, peer.mesh_id


def run_master(cfg: MeshConfig, args: argparse.Namespace) -> int:
    peer_id, peer_mesh = peer_for(cfg)
    token = random.getrandbits(64)
    waiter = EchoWaiter(peer_mesh, token)
    radio = RadioClient(cfg, "lora-audit")
    radio.add_binary_handler(waiter.handler)
    radio.connect(no_nodes=False, timeout=60)
    rtts: list[float] = []
    sent = 0
    echoed = 0
    tx_bytes = 0
    rx_bytes = 0
    try:
        print(
            "[audit] "
            f"role=master peer={peer_id} mesh={peer_mesh} "
            f"count={args.count} bytes={args.bytes} timeout={args.timeout}s",
            flush=True,
        )
        for seq in range(1, args.count + 1):
            payload = make_payload(TYPE_PROBE, seq, args.bytes, token)
            started = time.monotonic()
            try:
                radio.send_bytes(payload, destination_id=peer_mesh, want_ack=False)
                sent += 1
                tx_bytes += len(payload)
            except Exception as exc:
                print(f"[attempt] seq={seq} tx_failed={exc}", flush=True)
                continue
            response = waiter.wait_for(seq, args.timeout)
            if response is None:
                print(f"[attempt] seq={seq} tx_bytes={len(payload)} echo=no timeout={args.timeout}s", flush=True)
            else:
                received_at, frame, packet = response
                rtt = received_at - started
                rtts.append(rtt)
                echoed += 1
                rx_bytes += MIN_FRAME_BYTES + frame.body_len
                print(
                    "[attempt] "
                    f"seq={seq} tx_bytes={len(payload)} echo=yes "
                    f"rtt={rtt:.3f}s rx_bytes={MIN_FRAME_BYTES + frame.body_len} "
                    f"rssi={packet_metric(packet, 'rxRssi', 'rx_rssi', 'rssi') or '?'} "
                    f"snr={packet_metric(packet, 'rxSnr', 'rx_snr', 'snr') or '?'}",
                    flush=True,
                )
            if args.interval_ms > 0:
                time.sleep(args.interval_ms / 1000.0)
    finally:
        radio.close()
    lost = sent - echoed
    loss_pct = (lost / sent * 100.0) if sent else 100.0
    if rtts:
        mean = statistics.fmean(rtts)
        median = statistics.median(rtts)
        minimum = min(rtts)
        maximum = max(rtts)
    else:
        mean = median = minimum = maximum = 0.0
    print(
        "[summary] "
        f"sent={sent} echoed={echoed} lost={lost} loss={loss_pct:.1f}% "
        f"tx_bytes={tx_bytes}B rx_bytes={rx_bytes}B "
        f"rtt_min={minimum:.3f}s rtt_mean={mean:.3f}s "
        f"rtt_median={median:.3f}s rtt_max={maximum:.3f}s "
        f"invalid={waiter.invalid} foreign={waiter.foreign}",
        flush=True,
    )
    return 0 if echoed == sent else 1


def run_slave(cfg: MeshConfig, args: argparse.Namespace) -> int:
    radio = RadioClient(cfg, "lora-audit")
    server = EchoServer(cfg, radio)
    radio.add_binary_handler(server.handler)
    radio.connect(no_nodes=False, timeout=60)
    print(
        "[audit] "
        f"role=slave echo_server duration={args.duration}s "
        f"local_mesh_id={radio.local_mesh_id()}",
        flush=True,
    )
    deadline = time.monotonic() + args.duration
    try:
        while time.monotonic() < deadline:
            time.sleep(args.status_interval)
            server.print_status()
    except KeyboardInterrupt:
        pass
    finally:
        radio.close()
    server.print_status()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Raw LoRa packet echo audit over Meshtastic.")
    parser.add_argument("config", nargs="?", default="config.flower.yaml")
    parser.add_argument("--count", type=int, default=int(os.getenv("COUNT", "10")))
    parser.add_argument("--bytes", type=int, default=int(os.getenv("BYTES", "64")))
    parser.add_argument("--timeout", type=float, default=float(os.getenv("TIMEOUT", "20")))
    parser.add_argument("--interval-ms", type=int, default=int(os.getenv("INTERVAL_MS", "1000")))
    parser.add_argument("--duration", type=float, default=float(os.getenv("DURATION", "300")))
    parser.add_argument(
        "--status-interval",
        type=float,
        default=float(os.getenv("STATUS_INTERVAL", "10")),
    )
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    cfg = load_config(args.config)
    if cfg.app.role == "master":
        return run_master(cfg, args)
    if cfg.app.role == "slave":
        return run_slave(cfg, args)
    raise ValueError("app.role must be master or slave")


if __name__ == "__main__":
    raise SystemExit(main())
