#!/usr/bin/env python3
from __future__ import annotations

import subprocess
import sys
import time
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


def main() -> int:
    config_path = sys.argv[1] if len(sys.argv) > 1 else "config.flower.yaml"
    cfg = load_config(config_path)

    print(f"[force] config={cfg.path}")
    print(f"[force] role={cfg.app.role} node={cfg.app.node_id}")
    stop_services()

    port = resolve_port(cfg, "force", verbose=True)
    psk = channel_psk_for_cli(cfg)
    command = [
        "--port",
        port,
        "--set",
        "lora.region",
        cfg.radio.region,
        "--set",
        "lora.modem_preset",
        cfg.radio.modem_preset,
        "--set",
        "lora.hop_limit",
        str(cfg.radio.hop_limit),
        "--set",
        "lora.channel_num",
        str(cfg.radio.frequency_slot),
        "--set",
        "lora.tx_power",
        str(cfg.radio.tx_power),
        "--set",
        "lora.ignore_mqtt",
        str(cfg.radio.ignore_mqtt).lower(),
        "--set",
        "lora.config_ok_to_mqtt",
        str(cfg.radio.ok_to_mqtt).lower(),
        "--set",
        "lora.tx_enabled",
        str(cfg.radio.transmit_enabled).lower(),
        "--set",
        "device.role",
        cfg.device.role,
        "--set",
        "device.rebroadcast_mode",
        cfg.device.rebroadcast_mode,
        "--set",
        "device.node_info_broadcast_secs",
        str(cfg.device.node_info_broadcast_secs),
        "--set",
        "device.serial_enabled",
        str(cfg.device.serial_enabled).lower(),
        "--set",
        "power.is_power_saving",
        str(cfg.device.is_power_saving).lower(),
        "--ch-index",
        str(cfg.radio.channel_index),
        "--ch-set",
        "name",
        cfg.radio.channel_name,
        "--ch-set",
        "psk",
        psk,
        "--set-owner",
        cfg.app.node_name,
        "--set-owner-short",
        cfg.app.node_short_name,
    ]

    print(f"[force] port={port}")
    print("[force] applying radio settings from YAML")
    run_meshtastic_cli(command, "force")

    print("[force] rebooting radio")
    run_meshtastic_cli(["--port", port, "--reboot"], "force")
    print("[force] waiting 45 seconds")
    time.sleep(45)

    print("[force] running doctor")
    return subprocess.call([str(ROOT / "meshnet"), "doctor", "--config", str(cfg.path)])


if __name__ == "__main__":
    raise SystemExit(main())
