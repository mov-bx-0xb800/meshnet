#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import math
import socket
import statistics
import struct
import sys
import time
import zipfile
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Iterable


DEFAULT_MODEL_BYTES = 48_712
DEFAULT_PAYLOAD_BYTES = 192
DEFAULT_WINDOW_SIZE = 8
DEFAULT_TARGET_GOODPUT_BPS = 700.0
MAX_HEADER_BYTES = 64 * 1024
FRAME_LEN = struct.Struct("!I")


class BenchmarkProtocolError(RuntimeError):
    pass


@dataclass(frozen=True)
class TransferPlan:
    model_bytes: int
    payload_bytes: int
    window_size: int
    data_frames: int
    windows: int
    ack_frames: int
    poll_frames: int
    poll_done_frames: int
    approx_packets: int
    payload_seconds_at_target: float


@dataclass(frozen=True)
class RoundPlan:
    model_bytes: int
    rounds: int
    logical_clients: int
    include_evaluate: bool
    full_model_transfers_per_round: int
    total_full_model_transfers: int
    bytes_per_round: int
    total_model_bytes: int
    data_frames_per_round: int
    windows_per_round: int
    approx_packets_per_round: int
    payload_seconds_per_round_at_target: float
    total_payload_seconds_at_target: float
    per_transfer: TransferPlan


@dataclass
class TransferResult:
    role: str
    round: int
    logical_client: int
    phase: str
    direction: str
    bytes: int
    ok: bool
    local_elapsed_seconds: float
    goodput_bytes_per_second: float
    peer_elapsed_seconds: float | None
    peer_goodput_bytes_per_second: float | None
    data_frames: int
    windows: int
    approx_packets: int
    sha256: str


class JsonlWriter:
    def __init__(self, path: str | None) -> None:
        self.path = Path(path).expanduser() if path else None

    def write(self, event: dict[str, Any]) -> None:
        if self.path is None:
            return
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, sort_keys=True) + "\n")


def ceildiv(value: int, divisor: int) -> int:
    return (value + divisor - 1) // divisor


def transfer_plan(
    model_bytes: int,
    *,
    payload_bytes: int = DEFAULT_PAYLOAD_BYTES,
    window_size: int = DEFAULT_WINDOW_SIZE,
    target_goodput_bps: float = DEFAULT_TARGET_GOODPUT_BPS,
) -> TransferPlan:
    if model_bytes < 1:
        raise ValueError("model bytes must be positive")
    if payload_bytes < 1:
        raise ValueError("payload bytes must be positive")
    if window_size < 1:
        raise ValueError("window size must be positive")
    if target_goodput_bps <= 0:
        raise ValueError("target goodput must be positive")

    data_frames = ceildiv(model_bytes, payload_bytes)
    windows = ceildiv(data_frames, window_size)
    ack_frames = windows
    poll_frames = windows
    poll_done_frames = windows * 2
    approx_packets = data_frames + ack_frames + poll_frames + poll_done_frames
    return TransferPlan(
        model_bytes=model_bytes,
        payload_bytes=payload_bytes,
        window_size=window_size,
        data_frames=data_frames,
        windows=windows,
        ack_frames=ack_frames,
        poll_frames=poll_frames,
        poll_done_frames=poll_done_frames,
        approx_packets=approx_packets,
        payload_seconds_at_target=model_bytes / target_goodput_bps,
    )


def round_plan(
    model_bytes: int,
    *,
    rounds: int,
    logical_clients: int,
    include_evaluate: bool,
    payload_bytes: int = DEFAULT_PAYLOAD_BYTES,
    window_size: int = DEFAULT_WINDOW_SIZE,
    target_goodput_bps: float = DEFAULT_TARGET_GOODPUT_BPS,
) -> RoundPlan:
    if rounds < 1:
        raise ValueError("rounds must be positive")
    if logical_clients < 1:
        raise ValueError("logical clients must be positive")

    per_transfer = transfer_plan(
        model_bytes,
        payload_bytes=payload_bytes,
        window_size=window_size,
        target_goodput_bps=target_goodput_bps,
    )
    transfers_per_client = 3 if include_evaluate else 2
    full_model_transfers_per_round = logical_clients * transfers_per_client
    total_full_model_transfers = rounds * full_model_transfers_per_round
    bytes_per_round = model_bytes * full_model_transfers_per_round
    return RoundPlan(
        model_bytes=model_bytes,
        rounds=rounds,
        logical_clients=logical_clients,
        include_evaluate=include_evaluate,
        full_model_transfers_per_round=full_model_transfers_per_round,
        total_full_model_transfers=total_full_model_transfers,
        bytes_per_round=bytes_per_round,
        total_model_bytes=bytes_per_round * rounds,
        data_frames_per_round=per_transfer.data_frames * full_model_transfers_per_round,
        windows_per_round=per_transfer.windows * full_model_transfers_per_round,
        approx_packets_per_round=per_transfer.approx_packets
        * full_model_transfers_per_round,
        payload_seconds_per_round_at_target=bytes_per_round / target_goodput_bps,
        total_payload_seconds_at_target=bytes_per_round * rounds / target_goodput_bps,
        per_transfer=per_transfer,
    )


def flower_tensor_bytes_from_npz(path: str | Path) -> int:
    total = 0
    with zipfile.ZipFile(Path(path).expanduser()) as archive:
        for member in archive.infolist():
            if member.filename.endswith(".npy"):
                total += member.file_size
    if total < 1:
        raise ValueError(f"no .npy tensors found in {path}")
    return total


def resolve_model_bytes(args: argparse.Namespace) -> tuple[int, str]:
    if getattr(args, "weights_npz", None):
        path = Path(args.weights_npz).expanduser()
        return flower_tensor_bytes_from_npz(path), f"npz:{path}"
    if getattr(args, "model_bytes", None):
        return int(args.model_bytes), "cli"
    return DEFAULT_MODEL_BYTES, "default-current-model"


def payload_for(label: str, size: int) -> bytes:
    output = bytearray()
    seed = label.encode("utf-8")
    counter = 0
    while len(output) < size:
        output.extend(hashlib.sha256(seed + counter.to_bytes(8, "big")).digest())
        counter += 1
    return bytes(output[:size])


def payload_hash(label: str, size: int) -> str:
    return hashlib.sha256(payload_for(label, size)).hexdigest()


def send_message(sock: socket.socket, header: dict[str, Any], payload: bytes = b"") -> None:
    envelope = dict(header)
    envelope["body_bytes"] = len(payload)
    raw_header = json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(raw_header) > MAX_HEADER_BYTES:
        raise BenchmarkProtocolError("message header is too large")
    sock.sendall(FRAME_LEN.pack(len(raw_header)))
    sock.sendall(raw_header)
    if payload:
        sock.sendall(payload)


def recv_exact(sock: socket.socket, size: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < size:
        chunk = sock.recv(min(64 * 1024, size - len(chunks)))
        if not chunk:
            raise EOFError(f"socket closed with {size - len(chunks)} bytes left")
        chunks.extend(chunk)
    return bytes(chunks)


def recv_message(sock: socket.socket) -> tuple[dict[str, Any], bytes, float]:
    header_len = FRAME_LEN.unpack(recv_exact(sock, FRAME_LEN.size))[0]
    if header_len < 2 or header_len > MAX_HEADER_BYTES:
        raise BenchmarkProtocolError(f"invalid message header length: {header_len}")
    header = json.loads(recv_exact(sock, header_len).decode("utf-8"))
    payload_len = int(header.get("body_bytes", 0))
    if payload_len < 0:
        raise BenchmarkProtocolError("negative payload length")
    started = time.monotonic()
    payload = recv_exact(sock, payload_len) if payload_len else b""
    return header, payload, time.monotonic() - started


def hash_payload(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def goodput(size: int, elapsed: float) -> float:
    return size / elapsed if elapsed > 0 else 0.0


def result_plan_fields(size: int, payload_bytes: int, window_size: int) -> dict[str, int]:
    plan = transfer_plan(size, payload_bytes=payload_bytes, window_size=window_size)
    return {
        "data_frames": plan.data_frames,
        "windows": plan.windows,
        "approx_packets": plan.approx_packets,
    }


def transfer_result_event(result: TransferResult) -> dict[str, Any]:
    event = asdict(result)
    event["event"] = "transfer"
    event["local_elapsed_seconds"] = round(result.local_elapsed_seconds, 6)
    event["goodput_bytes_per_second"] = round(result.goodput_bytes_per_second, 1)
    if result.peer_elapsed_seconds is not None:
        event["peer_elapsed_seconds"] = round(result.peer_elapsed_seconds, 6)
    if result.peer_goodput_bytes_per_second is not None:
        event["peer_goodput_bytes_per_second"] = round(
            result.peer_goodput_bytes_per_second, 1
        )
    event["sha256"] = result.sha256[:16]
    return event


def print_event(event: dict[str, Any], writer: JsonlWriter) -> None:
    writer.write(event)
    if event.get("event") == "transfer":
        peer_elapsed = event.get("peer_elapsed_seconds")
        local_elapsed = float(event["local_elapsed_seconds"])
        peer_part = (
            f" peer_elapsed={float(peer_elapsed):.6f}s"
            if peer_elapsed is not None
            else ""
        )
        print(
            "[transfer] "
            f"role={event['role']} r={event['round']} c={event['logical_client']} "
            f"phase={event['phase']} dir={event['direction']} ok={event['ok']} "
            f"bytes={event['bytes']} elapsed={local_elapsed:.6f}s "
            f"goodput={event['goodput_bytes_per_second']}B/s{peer_part} "
            f"frames={event['data_frames']} windows={event['windows']} "
            f"approx_packets={event['approx_packets']} sha={event['sha256']}",
            flush=True,
        )
    elif event.get("event") in {"summary", "round_summary"}:
        label = "round_summary" if event.get("event") == "round_summary" else "summary"
        round_part = f" round={event['round']}" if "round" in event else ""
        print(
            f"[{label}] "
            f"role={event['role']}{round_part} ok={event['ok']} "
            f"transfers={event['transfers']} "
            f"bytes={event['bytes']} elapsed={event['elapsed_seconds']}s "
            f"goodput={event['goodput_bytes_per_second']}B/s "
            f"mean_transfer_goodput={event['mean_transfer_goodput_bytes_per_second']}B/s "
            f"p90_transfer_goodput={event['p90_transfer_goodput_bytes_per_second']}B/s",
            flush=True,
        )
    elif event.get("event") == "plan":
        print(
            "[plan] "
            f"model_bytes={event['model_bytes']} rounds={event['rounds']} "
            f"logical_clients={event['logical_clients']} "
            f"evaluate={event['include_evaluate']} "
            f"transfers_per_round={event['full_model_transfers_per_round']} "
            f"bytes_per_round={event['bytes_per_round']} "
            f"data_frames_per_round={event['data_frames_per_round']} "
            f"windows_per_round={event['windows_per_round']} "
            f"approx_packets_per_round={event['approx_packets_per_round']} "
            f"payload_only_seconds_at_{event['target_goodput_bytes_per_second']}Bps="
            f"{event['payload_seconds_per_round_at_target']}",
            flush=True,
        )
    elif event.get("event") == "client_config":
        print(
            "[config] "
            f"model_bytes={event['model_bytes']} rounds={event['rounds']} "
            f"logical_clients={event['logical_clients']} "
            f"evaluate={event['include_evaluate']} "
            f"payload_bytes={event['payload_bytes']} "
            f"window_size={event['window_size']}",
            flush=True,
        )
    elif event.get("event") == "eval_metrics":
        print(
            "[eval] "
            f"r={event['round']} c={event['logical_client']} "
            f"loss={event['loss']} examples={event['examples']}",
            flush=True,
        )
    else:
        print(json.dumps(event, sort_keys=True), flush=True)


def plan_event(plan: RoundPlan, source: str, target_goodput_bps: float) -> dict[str, Any]:
    return {
        "event": "plan",
        "model_bytes": plan.model_bytes,
        "model_bytes_source": source,
        "rounds": plan.rounds,
        "logical_clients": plan.logical_clients,
        "include_evaluate": plan.include_evaluate,
        "full_model_transfers_per_round": plan.full_model_transfers_per_round,
        "total_full_model_transfers": plan.total_full_model_transfers,
        "bytes_per_round": plan.bytes_per_round,
        "total_model_bytes": plan.total_model_bytes,
        "data_frames_per_transfer": plan.per_transfer.data_frames,
        "windows_per_transfer": plan.per_transfer.windows,
        "approx_packets_per_transfer": plan.per_transfer.approx_packets,
        "data_frames_per_round": plan.data_frames_per_round,
        "windows_per_round": plan.windows_per_round,
        "approx_packets_per_round": plan.approx_packets_per_round,
        "payload_seconds_per_transfer_at_target": round(
            plan.per_transfer.payload_seconds_at_target, 3
        ),
        "payload_seconds_per_round_at_target": round(
            plan.payload_seconds_per_round_at_target, 3
        ),
        "total_payload_seconds_at_target": round(
            plan.total_payload_seconds_at_target, 3
        ),
        "target_goodput_bytes_per_second": target_goodput_bps,
    }


def send_model(
    sock: socket.socket,
    *,
    role: str,
    round_index: int,
    logical_client: int,
    phase: str,
    direction: str,
    model_bytes: int,
    payload_bytes: int,
    window_size: int,
    writer: JsonlWriter,
) -> TransferResult:
    label = f"{round_index}:{logical_client}:{phase}:{direction}:{model_bytes}"
    payload = payload_for(label, model_bytes)
    sha256 = hash_payload(payload)
    started = time.monotonic()
    send_message(
        sock,
        {
            "kind": "model",
            "round": round_index,
            "logical_client": logical_client,
            "phase": phase,
            "direction": direction,
            "size": model_bytes,
            "sha256": sha256,
        },
        payload,
    )
    header, ack_payload, _receive_elapsed = recv_message(sock)
    elapsed = time.monotonic() - started
    if ack_payload:
        raise BenchmarkProtocolError("ack carried unexpected payload")
    if header.get("kind") != "ack":
        raise BenchmarkProtocolError(f"expected ack, got {header.get('kind')}")
    ok = bool(header.get("ok")) and header.get("sha256") == sha256
    plan_fields = result_plan_fields(model_bytes, payload_bytes, window_size)
    result = TransferResult(
        role=role,
        round=round_index,
        logical_client=logical_client,
        phase=phase,
        direction=direction,
        bytes=model_bytes,
        ok=ok,
        local_elapsed_seconds=elapsed,
        goodput_bytes_per_second=goodput(model_bytes, elapsed),
        peer_elapsed_seconds=float(header.get("receive_elapsed_seconds", 0.0)),
        peer_goodput_bytes_per_second=float(header.get("goodput_bytes_per_second", 0.0)),
        sha256=sha256,
        **plan_fields,
    )
    print_event(transfer_result_event(result), writer)
    return result


def receive_model(
    sock: socket.socket,
    *,
    role: str,
    expected_direction: str,
    payload_bytes: int,
    window_size: int,
    writer: JsonlWriter,
    emit: bool = True,
) -> TransferResult:
    header, payload, receive_elapsed = recv_message(sock)
    if header.get("kind") != "model":
        raise BenchmarkProtocolError(f"expected model, got {header.get('kind')}")
    size = int(header["size"])
    sha256 = hash_payload(payload)
    ok = len(payload) == size and sha256 == header.get("sha256")
    if header.get("direction") != expected_direction:
        ok = False
    send_message(
        sock,
        {
            "kind": "ack",
            "ok": ok,
            "round": int(header["round"]),
            "logical_client": int(header["logical_client"]),
            "phase": str(header["phase"]),
            "direction": str(header["direction"]),
            "size": size,
            "sha256": sha256,
            "receive_elapsed_seconds": receive_elapsed,
            "goodput_bytes_per_second": goodput(size, receive_elapsed),
        },
    )
    plan_fields = result_plan_fields(size, payload_bytes, window_size)
    result = TransferResult(
        role=role,
        round=int(header["round"]),
        logical_client=int(header["logical_client"]),
        phase=str(header["phase"]),
        direction=str(header["direction"]),
        bytes=size,
        ok=ok,
        local_elapsed_seconds=receive_elapsed,
        goodput_bytes_per_second=goodput(size, receive_elapsed),
        peer_elapsed_seconds=None,
        peer_goodput_bytes_per_second=None,
        sha256=sha256,
        **plan_fields,
    )
    if emit:
        print_event(transfer_result_event(result), writer)
    if not ok:
        raise BenchmarkProtocolError("model payload failed integrity check")
    return result


def request_client_model(
    sock: socket.socket,
    *,
    role: str,
    round_index: int,
    logical_client: int,
    phase: str,
    model_bytes: int,
    payload_bytes: int,
    window_size: int,
    writer: JsonlWriter,
) -> TransferResult:
    direction = "client_to_central"
    send_message(
        sock,
        {
            "kind": "request_model",
            "round": round_index,
            "logical_client": logical_client,
            "phase": phase,
            "direction": direction,
            "size": model_bytes,
            "sha256": payload_hash(
                f"{round_index}:{logical_client}:{phase}:{direction}:{model_bytes}",
                model_bytes,
            ),
        },
    )
    started = time.monotonic()
    result = receive_model(
        sock,
        role=role,
        expected_direction=direction,
        payload_bytes=payload_bytes,
        window_size=window_size,
        writer=JsonlWriter(None),
        emit=False,
    )
    elapsed = time.monotonic() - started
    server_result = TransferResult(
        role=role,
        round=result.round,
        logical_client=result.logical_client,
        phase=result.phase,
        direction=result.direction,
        bytes=result.bytes,
        ok=result.ok,
        local_elapsed_seconds=elapsed,
        goodput_bytes_per_second=goodput(result.bytes, elapsed),
        peer_elapsed_seconds=result.local_elapsed_seconds,
        peer_goodput_bytes_per_second=result.goodput_bytes_per_second,
        data_frames=result.data_frames,
        windows=result.windows,
        approx_packets=result.approx_packets,
        sha256=result.sha256,
    )
    print_event(transfer_result_event(server_result), writer)
    return server_result


def handle_request_model(
    sock: socket.socket,
    header: dict[str, Any],
    *,
    role: str,
    fit_sleep: float,
    payload_bytes: int,
    window_size: int,
    writer: JsonlWriter,
) -> TransferResult:
    if fit_sleep > 0:
        print(f"[sleep] fit_sleep={fit_sleep}s", flush=True)
        time.sleep(fit_sleep)
    direction = str(header["direction"])
    label = (
        f"{int(header['round'])}:{int(header['logical_client'])}:"
        f"{str(header['phase'])}:{direction}:{int(header['size'])}"
    )
    expected_sha = str(header["sha256"])
    actual_sha = payload_hash(label, int(header["size"]))
    if expected_sha != actual_sha:
        raise BenchmarkProtocolError("request_model sha256 does not match generated payload")
    return send_model(
        sock,
        role=role,
        round_index=int(header["round"]),
        logical_client=int(header["logical_client"]),
        phase=str(header["phase"]),
        direction=direction,
        model_bytes=int(header["size"]),
        payload_bytes=payload_bytes,
        window_size=window_size,
        writer=writer,
    )


def summarize(
    role: str,
    results: list[TransferResult],
    elapsed: float,
    *,
    event: str = "summary",
    round_index: int | None = None,
) -> dict[str, Any]:
    goodputs = [item.goodput_bytes_per_second for item in results if item.ok]
    total_bytes = sum(item.bytes for item in results)
    if goodputs:
        p90 = quantile(goodputs, 0.9)
        mean_goodput = statistics.fmean(goodputs)
        median_goodput = statistics.median(goodputs)
        min_goodput = min(goodputs)
        max_goodput = max(goodputs)
    else:
        p90 = mean_goodput = median_goodput = min_goodput = max_goodput = 0.0
    summary = {
        "event": event,
        "role": role,
        "ok": all(item.ok for item in results),
        "transfers": len(results),
        "bytes": total_bytes,
        "elapsed_seconds": round(elapsed, 3),
        "goodput_bytes_per_second": round(goodput(total_bytes, elapsed), 1),
        "mean_transfer_goodput_bytes_per_second": round(mean_goodput, 1),
        "median_transfer_goodput_bytes_per_second": round(median_goodput, 1),
        "p90_transfer_goodput_bytes_per_second": round(p90, 1),
        "minimum_transfer_goodput_bytes_per_second": round(min_goodput, 1),
        "maximum_transfer_goodput_bytes_per_second": round(max_goodput, 1),
        "total_data_frames": sum(item.data_frames for item in results),
        "total_windows": sum(item.windows for item in results),
        "total_approx_packets": sum(item.approx_packets for item in results),
    }
    if round_index is not None:
        summary["round"] = round_index
    return summary


def quantile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    if len(values) == 1:
        return values[0]
    ordered = sorted(values)
    index = (len(ordered) - 1) * q
    lower = math.floor(index)
    upper = math.ceil(index)
    if lower == upper:
        return ordered[int(index)]
    weight = index - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def run_server(args: argparse.Namespace) -> int:
    model_bytes, source = resolve_model_bytes(args)
    writer = JsonlWriter(args.jsonl)
    plan = round_plan(
        model_bytes,
        rounds=args.rounds,
        logical_clients=args.logical_clients,
        include_evaluate=not args.no_evaluate,
        payload_bytes=args.payload_bytes,
        window_size=args.window_size,
        target_goodput_bps=args.target_goodput,
    )
    print_event(plan_event(plan, source, args.target_goodput), writer)
    print(
        "central server waiting for the client benchmark through the bridge "
        f"on {args.host}:{args.port}",
        flush=True,
    )
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((args.host, args.port))
        server.listen(1)
        connection, address = server.accept()
    with connection:
        connection.settimeout(args.timeout)
        print(f"central accepted benchmark tunnel from {address[0]}:{address[1]}", flush=True)
        header, payload, _elapsed = recv_message(connection)
        if payload or header.get("kind") != "hello":
            raise BenchmarkProtocolError("client did not send hello")
        send_message(
            connection,
            {
                "kind": "server_config",
                "model_bytes": model_bytes,
                "rounds": args.rounds,
                "logical_clients": args.logical_clients,
                "include_evaluate": not args.no_evaluate,
                "payload_bytes": args.payload_bytes,
                "window_size": args.window_size,
                "target_goodput": args.target_goodput,
            },
        )
        results: list[TransferResult] = []
        started = time.monotonic()
        for round_index in range(1, args.rounds + 1):
            round_started = time.monotonic()
            round_result_start = len(results)
            send_message(connection, {"kind": "round_start", "round": round_index})
            for logical_client in range(1, args.logical_clients + 1):
                results.append(
                    send_model(
                        connection,
                        role="central",
                        round_index=round_index,
                        logical_client=logical_client,
                        phase="fit_down",
                        direction="central_to_client",
                        model_bytes=model_bytes,
                        payload_bytes=args.payload_bytes,
                        window_size=args.window_size,
                        writer=writer,
                    )
                )
                results.append(
                    request_client_model(
                        connection,
                        role="central",
                        round_index=round_index,
                        logical_client=logical_client,
                        phase="fit_up",
                        model_bytes=model_bytes,
                        payload_bytes=args.payload_bytes,
                        window_size=args.window_size,
                        writer=writer,
                    )
                )
            if args.aggregate_sleep > 0:
                print(f"[sleep] aggregate_sleep={args.aggregate_sleep}s", flush=True)
                time.sleep(args.aggregate_sleep)
            if not args.no_evaluate:
                for logical_client in range(1, args.logical_clients + 1):
                    results.append(
                        send_model(
                            connection,
                            role="central",
                            round_index=round_index,
                            logical_client=logical_client,
                            phase="eval_down",
                            direction="central_to_client",
                            model_bytes=model_bytes,
                            payload_bytes=args.payload_bytes,
                            window_size=args.window_size,
                            writer=writer,
                        )
                    )
                    header, payload, _elapsed = recv_message(connection)
                    if payload or header.get("kind") != "eval_metrics":
                        raise BenchmarkProtocolError("client did not return eval metrics")
                    print_event({"event": "eval_metrics", **header}, writer)
            send_message(connection, {"kind": "round_done", "round": round_index})
            print_event(
                summarize(
                    "central",
                    results[round_result_start:],
                    time.monotonic() - round_started,
                    event="round_summary",
                    round_index=round_index,
                ),
                writer,
            )
        send_message(connection, {"kind": "run_done"})
        summary = summarize("central", results, time.monotonic() - started)
        print_event(summary, writer)
    return 0 if summary["ok"] else 1


def receive_or_handle_model(
    connection: socket.socket,
    *,
    payload_bytes: int,
    window_size: int,
    writer: JsonlWriter,
    eval_sleep: float,
) -> dict[str, Any]:
    header, payload, receive_elapsed = recv_message(connection)
    if header.get("kind") != "model":
        return {"header": header, "payload": payload, "receive_elapsed": receive_elapsed}
    if str(header.get("direction")) != "central_to_client":
        raise BenchmarkProtocolError("client received model with wrong direction")
    size = int(header["size"])
    sha256 = hash_payload(payload)
    ok = len(payload) == size and sha256 == header.get("sha256")
    send_message(
        connection,
        {
            "kind": "ack",
            "ok": ok,
            "round": int(header["round"]),
            "logical_client": int(header["logical_client"]),
            "phase": str(header["phase"]),
            "direction": str(header["direction"]),
            "size": size,
            "sha256": sha256,
            "receive_elapsed_seconds": receive_elapsed,
            "goodput_bytes_per_second": goodput(size, receive_elapsed),
        },
    )
    plan_fields = result_plan_fields(size, payload_bytes, window_size)
    result = TransferResult(
        role="client",
        round=int(header["round"]),
        logical_client=int(header["logical_client"]),
        phase=str(header["phase"]),
        direction=str(header["direction"]),
        bytes=size,
        ok=ok,
        local_elapsed_seconds=receive_elapsed,
        goodput_bytes_per_second=goodput(size, receive_elapsed),
        peer_elapsed_seconds=None,
        peer_goodput_bytes_per_second=None,
        sha256=sha256,
        **plan_fields,
    )
    print_event(transfer_result_event(result), writer)
    if not ok:
        raise BenchmarkProtocolError("model payload failed integrity check")
    if str(header["phase"]) == "eval_down":
        if eval_sleep > 0:
            print(f"[sleep] eval_sleep={eval_sleep}s", flush=True)
            time.sleep(eval_sleep)
        send_message(
            connection,
            {
                "kind": "eval_metrics",
                "round": int(header["round"]),
                "logical_client": int(header["logical_client"]),
                "loss": 0.0,
                "examples": 0,
            },
        )
    return {
        "header": {"kind": "_handled_model"},
        "payload": b"",
        "receive_elapsed": receive_elapsed,
        "result": result,
    }


def run_client(args: argparse.Namespace) -> int:
    writer = JsonlWriter(args.jsonl)
    print(
        "client connecting to local bridge "
        f"at {args.host}:{args.port}",
        flush=True,
    )
    with socket.create_connection((args.host, args.port), timeout=args.timeout) as connection:
        connection.settimeout(args.timeout)
        send_message(
            connection,
            {
                "kind": "hello",
                "role": "client",
                "label": args.label,
                "version": 1,
            },
        )
        header, payload, _elapsed = recv_message(connection)
        if payload or header.get("kind") != "server_config":
            raise BenchmarkProtocolError("central did not return server_config")
        payload_bytes = int(header["payload_bytes"])
        window_size = int(header["window_size"])
        print_event(
            {
                "event": "client_config",
                "model_bytes": int(header["model_bytes"]),
                "rounds": int(header["rounds"]),
                "logical_clients": int(header["logical_clients"]),
                "include_evaluate": bool(header["include_evaluate"]),
                "payload_bytes": payload_bytes,
                "window_size": window_size,
            },
            writer,
        )
        results: list[TransferResult] = []
        started = time.monotonic()
        round_started = started
        round_result_start = 0
        while True:
            received = receive_or_handle_model(
                connection,
                payload_bytes=payload_bytes,
                window_size=window_size,
                writer=writer,
                eval_sleep=args.eval_sleep,
            )
            if received.get("result") is not None:
                results.append(received["result"])
                continue
            header = received["header"]
            payload = received["payload"]
            if payload:
                raise BenchmarkProtocolError("control message carried unexpected payload")
            kind = header.get("kind")
            if kind == "round_start":
                round_started = time.monotonic()
                round_result_start = len(results)
                print(f"[round] start r={header['round']}", flush=True)
            elif kind == "request_model":
                results.append(
                    handle_request_model(
                        connection,
                        header,
                        role="client",
                        fit_sleep=args.fit_sleep,
                        payload_bytes=payload_bytes,
                        window_size=window_size,
                        writer=writer,
                    )
                )
            elif kind == "round_done":
                print(f"[round] done r={header['round']}", flush=True)
                print_event(
                    summarize(
                        "client",
                        results[round_result_start:],
                        time.monotonic() - round_started,
                        event="round_summary",
                        round_index=int(header["round"]),
                    ),
                    writer,
                )
            elif kind == "run_done":
                summary = summarize("client", results, time.monotonic() - started)
                print_event(summary, writer)
                return 0 if summary["ok"] else 1
            else:
                raise BenchmarkProtocolError(f"unexpected control message: {kind}")


def run_plan(args: argparse.Namespace) -> int:
    model_bytes, source = resolve_model_bytes(args)
    writer = JsonlWriter(args.jsonl)
    plan = round_plan(
        model_bytes,
        rounds=args.rounds,
        logical_clients=args.logical_clients,
        include_evaluate=not args.no_evaluate,
        payload_bytes=args.payload_bytes,
        window_size=args.window_size,
        target_goodput_bps=args.target_goodput,
    )
    print_event(plan_event(plan, source, args.target_goodput), writer)
    return 0


def add_common_plan_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--model-bytes", type=int, default=None)
    parser.add_argument(
        "--weights-npz",
        default=None,
        help="sum uncompressed .npy tensor bytes from a Flower/Keras weights npz",
    )
    parser.add_argument("--rounds", type=int, default=1)
    parser.add_argument("--logical-clients", type=int, default=2)
    parser.add_argument("--no-evaluate", action="store_true")
    parser.add_argument("--payload-bytes", type=int, default=DEFAULT_PAYLOAD_BYTES)
    parser.add_argument("--window-size", type=int, default=DEFAULT_WINDOW_SIZE)
    parser.add_argument("--target-goodput", type=float, default=DEFAULT_TARGET_GOODPUT_BPS)
    parser.add_argument("--jsonl", default=None)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Full-round Flower-over-Meshnet benchmark. Run server on central and "
            "client through the client bridge."
        )
    )
    sub = parser.add_subparsers(dest="command", required=True)

    plan = sub.add_parser("plan", help="print expected transfer/frame counts")
    add_common_plan_args(plan)

    server = sub.add_parser("server", help="run on central behind the central bridge")
    add_common_plan_args(server)
    server.add_argument("--host", default="127.0.0.1")
    server.add_argument("--port", type=int, default=8081)
    server.add_argument("--timeout", type=float, default=1800.0)
    server.add_argument("--aggregate-sleep", type=float, default=0.0)

    client = sub.add_parser("client", help="run on client through the local bridge")
    client.add_argument("--host", default="127.0.0.1")
    client.add_argument("--port", type=int, default=8081)
    client.add_argument("--timeout", type=float, default=1800.0)
    client.add_argument("--fit-sleep", type=float, default=0.0)
    client.add_argument("--eval-sleep", type=float, default=0.0)
    client.add_argument("--jsonl", default=None)
    client.add_argument("--label", default=socket.gethostname())
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    try:
        if args.command == "plan":
            return run_plan(args)
        if args.command == "server":
            return run_server(args)
        if args.command == "client":
            return run_client(args)
        raise BenchmarkProtocolError(f"unknown command: {args.command}")
    except KeyboardInterrupt:
        print("benchmark stopped", flush=True)
        return 130
    except (OSError, EOFError, ValueError, BenchmarkProtocolError) as exc:
        print(f"benchmark failed: {exc}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
