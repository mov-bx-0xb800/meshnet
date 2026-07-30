#!/usr/bin/env python3
from __future__ import annotations

import subprocess
import sys
import time
import argparse
import json
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.config import channel_psk_for_cli, load_config
from src.radio import resolve_port, run_meshtastic_cli


def stop_services() -> None:
    for service in ("meshnet-flower-bridge", "meshnet", "meshnet-telegram"):
        subprocess.run(
            ["sudo", "systemctl", "stop", service],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Factory-reset Meshtastic config and force Flower radio settings from YAML."
    )
    parser.add_argument("config", nargs="?", default="config.flower.yaml")
    parser.add_argument("--port", default=None)
    parser.add_argument("--no-reset", action="store_true")
    return parser


def wait_after_reboot(seconds: int = 45) -> None:
    print(f"[force] waiting {seconds} seconds")
    time.sleep(seconds)


def configure_payload(cfg) -> dict:
    # Keep booleans out of --configure. Some Meshtastic/protobuf paths reject
    # JSON bools for fields they internally represent as ints. Boolean fields
    # are still applied below through one-field CLI --set commands.
    return {
        "owner": cfg.app.node_name,
        "owner_short": cfg.app.node_short_name,
        "config": {
            "lora": {
                "region": cfg.radio.region,
                "modem_preset": cfg.radio.modem_preset,
                "hop_limit": cfg.radio.hop_limit,
                "channel_num": cfg.radio.frequency_slot,
                "tx_power": cfg.radio.tx_power,
            },
            "device": {
                "role": cfg.device.role,
                "rebroadcast_mode": cfg.device.rebroadcast_mode,
                "node_info_broadcast_secs": cfg.device.node_info_broadcast_secs,
            },
        },
    }


def resolve_requested_port(cfg, requested_port: str | None) -> str:
    if requested_port:
        return requested_port
    return resolve_port(cfg, "force", verbose=True)


def main() -> int:
    args = build_parser().parse_args()
    cfg = load_config(args.config)

    print(f"[force] config={cfg.path}")
    print(f"[force] role={cfg.app.role} node={cfg.app.node_id}")
    stop_services()

    port = resolve_requested_port(cfg, args.port)
    psk = channel_psk_for_cli(cfg)
    print(f"[force] port={port}")
    if not args.no_reset:
        print("[force] factory-resetting Meshtastic config")
        run_meshtastic_cli(["--port", port, "--factory-reset"], "force")
        wait_after_reboot()
        port = resolve_requested_port(cfg, args.port)
        print(f"[force] port_after_reset={port}")

    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
        json.dump(configure_payload(cfg), handle, indent=2)
        configure_path = handle.name
    print(f"[force] applying generated Meshtastic configure file: {configure_path}")
    run_meshtastic_cli(["--port", port, "--configure", configure_path], "force")
    wait_after_reboot(10)

    print("[force] applying critical settings one field at a time")

    field_updates = [
        ("lora.region", cfg.radio.region),
        ("lora.modem_preset", cfg.radio.modem_preset),
        ("lora.hop_limit", cfg.radio.hop_limit),
        ("lora.channel_num", cfg.radio.frequency_slot),
        ("lora.tx_power", cfg.radio.tx_power),
        ("lora.ignore_mqtt", cfg.radio.ignore_mqtt),
        ("lora.config_ok_to_mqtt", cfg.radio.ok_to_mqtt),
        ("lora.tx_enabled", cfg.radio.transmit_enabled),
        ("device.role", cfg.device.role),
        ("device.rebroadcast_mode", cfg.device.rebroadcast_mode),
        ("device.node_info_broadcast_secs", cfg.device.node_info_broadcast_secs),
        ("device.serial_enabled", cfg.device.serial_enabled),
        ("power.is_power_saving", cfg.device.is_power_saving),
    ]

    for field, value in field_updates:
        text_value = str(value).lower() if isinstance(value, bool) else str(value)
        print(f"[force] set {field}={text_value}", flush=True)
        run_meshtastic_cli(["--port", port, "--set", field, text_value], "force")

    print(f"[force] set channel[{cfg.radio.channel_index}].name={cfg.radio.channel_name}")
    run_meshtastic_cli(
        [
            "--port",
            port,
            "--ch-index",
            str(cfg.radio.channel_index),
            "--ch-set",
            "name",
            cfg.radio.channel_name,
        ],
        "force",
    )
    print(f"[force] set channel[{cfg.radio.channel_index}].psk")
    run_meshtastic_cli(
        [
            "--port",
            port,
            "--ch-index",
            str(cfg.radio.channel_index),
            "--ch-set",
            "psk",
            psk,
        ],
        "force",
    )
    print(f"[force] set owner.long_name={cfg.app.node_name}")
    run_meshtastic_cli(["--port", port, "--set-owner", cfg.app.node_name], "force")
    print(f"[force] set owner.short_name={cfg.app.node_short_name}")
    run_meshtastic_cli(["--port", port, "--set-owner-short", cfg.app.node_short_name], "force")

    print("[force] rebooting radio")
    run_meshtastic_cli(["--port", port, "--reboot"], "force")
    wait_after_reboot()

    print("[force] running doctor")
    return subprocess.call([str(ROOT / "meshnet"), "doctor", "--config", str(cfg.path)])


if __name__ == "__main__":
    raise SystemExit(main())
