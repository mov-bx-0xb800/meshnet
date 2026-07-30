#!/usr/bin/env python3
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.config import channel_psk_for_cli, load_config
from src.radio import RadioClient, radio_config_mismatches, resolve_port


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
        description="Force Flower radio settings from YAML with one direct radio connection."
    )
    parser.add_argument("config", nargs="?", default="config.flower.yaml")
    parser.add_argument("--port", default=None)
    parser.add_argument("--no-reboot", action="store_true")
    return parser


def enum_number(message: Any, field_name: str, enum_name: str) -> int:
    field = message.DESCRIPTOR.fields_by_name[field_name]
    enum_value = field.enum_type.values_by_name.get(enum_name)
    if enum_value is None:
        choices = ", ".join(sorted(field.enum_type.values_by_name))
        raise ValueError(f"{field_name} has no enum {enum_name}; choices: {choices}")
    return int(enum_value.number)


def set_channel(node: Any, index: int, name: str, psk: str) -> None:
    import meshtastic.util

    channel = node.channels[index]
    channel.settings.name = name
    channel.settings.psk = meshtastic.util.fromPSK(psk)
    print(f"[force] write channel[{index}] name={name}")
    node.writeChannel(index)


def force_config(cfg, port: str, *, no_reboot: bool = False) -> int:
    radio = RadioClient(cfg, "force")
    radio.port = port
    try:
        radio.connect(no_nodes=True, timeout=60)
        interface = radio.interface
        node = interface.localNode

        print(f"[force] local_mesh_id={radio.local_mesh_id()}")
        print("[force] setting owner")
        node.setOwner(long_name=cfg.app.node_name, short_name=cfg.app.node_short_name)
        time.sleep(1)

        lora = node.localConfig.lora
        lora.region = enum_number(lora, "region", cfg.radio.region)
        lora.modem_preset = enum_number(lora, "modem_preset", cfg.radio.modem_preset)
        lora.hop_limit = int(cfg.radio.hop_limit)
        lora.channel_num = int(cfg.radio.frequency_slot)
        lora.tx_power = int(cfg.radio.tx_power)
        lora.ignore_mqtt = bool(cfg.radio.ignore_mqtt)
        lora.config_ok_to_mqtt = bool(cfg.radio.ok_to_mqtt)
        lora.tx_enabled = bool(cfg.radio.transmit_enabled)
        print(
            "[force] write lora "
            f"region={cfg.radio.region} modem={cfg.radio.modem_preset} "
            f"slot={cfg.radio.frequency_slot} tx_power={cfg.radio.tx_power}"
        )
        node.writeConfig("lora")
        time.sleep(1)

        device = node.localConfig.device
        device.role = enum_number(device, "role", cfg.device.role)
        device.rebroadcast_mode = enum_number(
            device, "rebroadcast_mode", cfg.device.rebroadcast_mode
        )
        device.node_info_broadcast_secs = int(cfg.device.node_info_broadcast_secs)
        device.serial_enabled = bool(cfg.device.serial_enabled)
        print(
            "[force] write device "
            f"role={cfg.device.role} rebroadcast={cfg.device.rebroadcast_mode}"
        )
        node.writeConfig("device")
        time.sleep(1)

        power = node.localConfig.power
        power.is_power_saving = bool(cfg.device.is_power_saving)
        print(f"[force] write power is_power_saving={cfg.device.is_power_saving}")
        node.writeConfig("power")
        time.sleep(1)

        set_channel(
            node,
            int(cfg.radio.channel_index),
            cfg.radio.channel_name,
            channel_psk_for_cli(cfg),
        )
        time.sleep(1)

        if not no_reboot:
            print("[force] rebooting radio")
            node.reboot()
            time.sleep(45)
    finally:
        radio.close()

    print("[force] verifying after reconnect")
    verifier = RadioClient(cfg, "force")
    verifier.port = port
    try:
        verifier.connect(no_nodes=True, timeout=60)
        actual = verifier.actual_config_summary()
        mismatches = radio_config_mismatches(cfg, actual)
        if mismatches:
            print("[force] mismatches remain:")
            for mismatch in mismatches:
                print(f"[force]   {mismatch}")
            return 1
        print("[force] radio matches config.flower.yaml")
        return 0
    finally:
        verifier.close()


def main() -> int:
    args = build_parser().parse_args()
    cfg = load_config(args.config)

    print(f"[force] config={cfg.path}")
    print(f"[force] role={cfg.app.role} node={cfg.app.node_id}")
    stop_services()

    port = args.port or resolve_port(cfg, "force", verbose=True)
    print(f"[force] port={port}")
    return force_config(cfg, port, no_reboot=args.no_reboot)


if __name__ == "__main__":
    raise SystemExit(main())
