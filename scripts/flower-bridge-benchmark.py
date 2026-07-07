#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import socket
import threading
import time


DEFAULT_MODEL_BYTES = 47_164


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="End-to-end integrity/goodput benchmark for the Meshnet Flower bridge"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    server = sub.add_parser("server", help="run on central instead of Flower")
    server.add_argument("--host", default="127.0.0.1")
    server.add_argument("--port", type=int, default=8081)
    client = sub.add_parser("client", help="run through the client bridge")
    client.add_argument("--host", default="127.0.0.1")
    client.add_argument("--port", type=int, default=8081)
    client.add_argument("--bytes", type=int, default=DEFAULT_MODEL_BYTES)
    client.add_argument("--count", type=int, default=10)
    client.add_argument("--timeout", type=float, default=300.0)
    return parser


def payload_for(iteration: int, size: int) -> bytes:
    return bytes(((offset * 31) + (iteration * 17)) % 256 for offset in range(size))


def run_server(host: str, port: int) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((host, port))
        server.listen(8)
        print(f"benchmark echo server listening on {host}:{port}", flush=True)
        while True:
            connection, address = server.accept()
            print(f"connection from {address[0]}:{address[1]}", flush=True)
            threading.Thread(
                target=echo_connection,
                args=(connection,),
                daemon=True,
            ).start()


def echo_connection(connection: socket.socket) -> None:
    with connection:
        while True:
            data = connection.recv(4096)
            if not data:
                return
            connection.sendall(data)


def run_client(host: str, port: int, size: int, count: int, timeout: float) -> int:
    if size < 1 or count < 1:
        raise ValueError("--bytes and --count must be positive")
    results: list[dict[str, object]] = []
    with socket.create_connection((host, port), timeout=timeout) as connection:
        connection.settimeout(timeout)
        for iteration in range(1, count + 1):
            payload = payload_for(iteration, size)
            expected_hash = hashlib.sha256(payload).hexdigest()
            started = time.monotonic()
            connection.sendall(payload)
            received = receive_exact(connection, size)
            elapsed = time.monotonic() - started
            actual_hash = hashlib.sha256(received).hexdigest()
            ok = len(received) == size and actual_hash == expected_hash
            result = {
                "iteration": iteration,
                "ok": ok,
                "bytes": size,
                "elapsed_seconds": round(elapsed, 3),
                "goodput_bytes_per_second": round(size / elapsed, 1) if elapsed else 0,
                "sha256": actual_hash,
            }
            results.append(result)
            print(json.dumps(result, sort_keys=True), flush=True)
            if not ok:
                return 1
    summary = {
        "ok": all(bool(item["ok"]) for item in results),
        "count": len(results),
        "minimum_goodput_bytes_per_second": min(
            float(item["goodput_bytes_per_second"]) for item in results
        ),
        "maximum_elapsed_seconds": max(float(item["elapsed_seconds"]) for item in results),
    }
    print(json.dumps(summary, sort_keys=True), flush=True)
    return 0 if summary["ok"] else 1


def receive_exact(connection: socket.socket, size: int) -> bytes:
    received = bytearray()
    while len(received) < size:
        data = connection.recv(min(4096, size - len(received)))
        if not data:
            break
        received.extend(data)
    return bytes(received)


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.command == "server":
            run_server(args.host, args.port)
            return 0
        return run_client(args.host, args.port, args.bytes, args.count, args.timeout)
    except KeyboardInterrupt:
        print("benchmark stopped", flush=True)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
