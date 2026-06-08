from __future__ import annotations

import argparse
import sys

from dotenv import load_dotenv

from . import logger
from .config import channel_psk_description, load_config
from .master import MasterNode, run_master
from .preflight import PreflightError, preflight_check, require_preflight
from .radio import detect_serial_ports, setup_radio
from .slave import run_slave
from .telegram_bridge import run_telegram_bridge
from .tester import run_tests


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="meshnet", description="Two-node Meshtastic test network")
    sub = parser.add_subparsers(dest="command", required=True)

    detect = sub.add_parser("detect", help="detect Meshtastic USB serial port")
    detect.add_argument("--plain", action="store_true", help="print only the detected port")

    for name in ("preflight", "info", "setup-radio", "run", "discover", "ping", "test", "telegram"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--config", default="config.master.yaml")

    send = sub.add_parser("send")
    send.add_argument("--config", default="config.master.yaml")
    send.add_argument("--text", required=True)
    send.add_argument("--dst", default="")

    return parser


def load_for_args(args: argparse.Namespace):
    load_dotenv()
    cfg = load_config(args.config)
    logger.configure_logging(cfg.app.log_level)
    return cfg


def load_and_preflight(args: argparse.Namespace):
    cfg = load_for_args(args)
    require_preflight(cfg)
    logger.blank()
    return cfg


def command_detect(args: argparse.Namespace) -> int:
    ports = detect_serial_ports()
    if args.plain:
        if ports:
            print(ports[0])
            return 0
        return 1
    logger.line("radio", "Searching for RAK/Meshtastic serial device...")
    if not ports:
        logger.line("radio", "No serial radio found.")
        logger.line("radio", "Checked:")
        logger.detail("- /dev/ttyACM*", indent=8)
        logger.detail("- /dev/ttyUSB*", indent=8)
        logger.line("radio", "Troubleshooting:")
        logger.detail("- Use a USB data cable, not charge-only.", indent=8)
        logger.detail("- Check RAK is powered.", indent=8)
        logger.detail("- Check user is in dialout group.", indent=8)
        logger.detail("- Try unplugging and plugging the RAK back in.", indent=8)
        return 1
    logger.line("radio", f"Found device: {ports[0]}")
    if len(ports) > 1:
        logger.line("radio", "Other serial devices:")
        for port in ports[1:]:
            logger.detail(f"- {port}", indent=8)
    return 0


def command_info(args: argparse.Namespace) -> int:
    cfg = load_and_preflight(args)
    logger.line("meshnet", "Configuration summary")
    logger.line("meshnet", f"Role: {cfg.app.role}")
    logger.line("meshnet", f"Node ID: {cfg.app.node_id}")
    logger.line("meshnet", f"Node Name: {cfg.app.node_name}")
    logger.line("meshnet", f"Short Name: {cfg.app.node_short_name}")
    logger.line("meshnet", f"Network: {cfg.network.network_id}")
    logger.line("meshnet", f"Channel: {cfg.radio.channel_name}")
    logger.line("meshnet", f"Region: {cfg.radio.region}")
    logger.line("meshnet", f"Modem preset: {cfg.radio.modem_preset}")
    logger.line("meshnet", f"Channel PSK: {channel_psk_description(cfg)}")
    logger.blank()

    node = MasterNode(cfg, "radio")
    try:
        node.connect()
        known = node.radio.known_nodes()
        logger.line("network", f"Known Meshtastic node count: {len(known)}")
        for item in known:
            logger.detail(f"- {item.mesh_id} / {item.long_name} / {item.short_name}", indent=10)
    finally:
        node.close()
    return 0


def command_setup_radio(args: argparse.Namespace) -> int:
    cfg = load_and_preflight(args)
    setup_radio(cfg)
    return 0


def command_run(args: argparse.Namespace) -> int:
    cfg = load_and_preflight(args)
    if cfg.app.role == "master":
        run_master(cfg)
    else:
        run_slave(cfg)
    return 0


def command_discover(args: argparse.Namespace) -> int:
    cfg = load_and_preflight(args)
    node = MasterNode(cfg, "network")
    try:
        node.connect()
        found = node.discover(cfg.runtime.discovery_timeout_seconds)
        return 0 if found else 1
    finally:
        node.close()


def command_ping(args: argparse.Namespace) -> int:
    cfg = load_and_preflight(args)
    node = MasterNode(cfg, "ping")
    try:
        node.connect()
        node.ping_loop(cfg.peer_node_id)
    finally:
        node.close()
    return 0


def command_send(args: argparse.Namespace) -> int:
    cfg = load_and_preflight(args)
    node = MasterNode(cfg, "mesh")
    try:
        node.connect()
        node.send_text_message(args.text, args.dst or cfg.peer_node_id)
    finally:
        node.close()
    return 0


def command_test(args: argparse.Namespace) -> int:
    cfg = load_and_preflight(args)
    return 0 if run_tests(cfg) else 1


def command_telegram(args: argparse.Namespace) -> int:
    cfg = load_and_preflight(args)
    run_telegram_bridge(cfg)
    return 0


def command_preflight(args: argparse.Namespace) -> int:
    cfg = load_for_args(args)
    result = preflight_check(cfg)
    return 0 if result.ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "detect":
            return command_detect(args)
        if args.command == "preflight":
            return command_preflight(args)
        if args.command == "info":
            return command_info(args)
        if args.command == "setup-radio":
            return command_setup_radio(args)
        if args.command == "run":
            return command_run(args)
        if args.command == "discover":
            return command_discover(args)
        if args.command == "ping":
            return command_ping(args)
        if args.command == "send":
            return command_send(args)
        if args.command == "test":
            return command_test(args)
        if args.command == "telegram":
            return command_telegram(args)
        parser.error(f"unknown command: {args.command}")
        return 2
    except KeyboardInterrupt:
        logger.line("meshnet", "Interrupted.")
        return 130
    except PreflightError:
        return 1
    except Exception as exc:
        logger.line("error", str(exc))
        return 1


if __name__ == "__main__":
    sys.exit(main())
