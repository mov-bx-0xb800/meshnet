from __future__ import annotations

import argparse
import sys
import time

from dotenv import load_dotenv

from . import logger
from .config import channel_psk_description, channel_psk_for_cli, load_config
from .errors import MeshNetError, as_meshnet_error, log_meshnet_error
from .flower_bridge import run_flower_bridge
from .master import MasterNode, run_master
from .preflight import preflight_check, require_preflight
from .radio import detect_serial_ports, radio_config_mismatches, setup_radio_reliably
from .slave import run_slave
from .state import StateStore, config_fingerprint
from .telegram_bridge import print_telegram_chat_ids, run_telegram_bridge
from .tester import run_tests


ROLE_CONFIGS = {
    "master": "config.master.yaml",
    "slave": "config.slave.yaml",
}
TELEGRAM_START_ATTEMPTS = 3
TELEGRAM_RETRY_DELAY_SECONDS = 10


def add_role_shortcut(
    parser: argparse._SubParsersAction[argparse.ArgumentParser],
    name: str,
    help_text: str,
    role: str,
) -> None:
    cmd = parser.add_parser(name, help=help_text)
    cmd.add_argument("--config", default=ROLE_CONFIGS[role])


def add_role_command(
    parser: argparse._SubParsersAction[argparse.ArgumentParser],
    name: str,
    help_text: str,
    *,
    required: bool = False,
) -> None:
    cmd = parser.add_parser(name, help=help_text)
    cmd.add_argument("role", nargs=None if required else "?", choices=ROLE_CONFIGS, default=None)
    cmd.add_argument("--config", default=None)


def apply_role_config(args: argparse.Namespace, default_role: str = "master") -> None:
    if getattr(args, "config", None):
        return
    role = getattr(args, "role", None) or default_role
    args.config = ROLE_CONFIGS[role]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="meshnet", description="Reliable Meshtastic application network")
    parser.add_argument("--how-to", action="store_true", help="show the concise setup and run guide")
    sub = parser.add_subparsers(dest="command", required=False)

    sub.add_parser("how-to", help="show the concise setup and run guide")

    detect = sub.add_parser("detect", help="detect Meshtastic USB serial port")
    detect.add_argument("--plain", action="store_true", help="print only the detected port")

    telegram_id = sub.add_parser("telegram-id", help="print recent Telegram chat IDs")
    telegram_id.add_argument("--config", default="config.master.yaml")

    add_role_shortcut(sub, "master", "start the master node", "master")
    add_role_shortcut(sub, "slave", "start the slave node", "slave")
    add_role_command(sub, "start", "start the selected node")
    add_role_command(sub, "check", "run preflight checks")
    add_role_command(sub, "setup", "apply radio setup for the selected node", required=True)
    add_role_command(sub, "nodes", "discover compatible mesh nodes")

    for name in (
        "preflight",
        "info",
        "setup-radio",
        "run",
        "discover",
        "ping",
        "test",
        "telegram",
        "status",
        "registry",
        "doctor",
        "bridge",
    ):
        cmd = sub.add_parser(name)
        cmd.add_argument("--config", default="config.master.yaml")

    monitor = sub.add_parser("monitor")
    monitor.add_argument("--config", default="config.master.yaml")
    monitor.add_argument("--interval", type=int, default=10)

    trust = sub.add_parser("trust", help="trust or repair an app-id to mesh-id binding")
    trust.add_argument("app_id")
    trust.add_argument("mesh_id")
    trust.add_argument("--config", default="config.master.yaml")

    unpair = sub.add_parser("unpair", help="remove a node from the local registry")
    unpair.add_argument("app_id")
    unpair.add_argument("--config", default="config.master.yaml")

    send = sub.add_parser("send")
    send.add_argument("--config", default="config.master.yaml")
    send.add_argument("message", nargs="?", help="message text")
    send.add_argument("--text", help="message text")
    send.add_argument("--dst", default="")

    return parser


def load_for_args(args: argparse.Namespace):
    load_dotenv()
    try:
        cfg = load_config(args.config)
    except Exception as exc:
        raise as_meshnet_error(exc, "config") from exc
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
    logger.line("meshnet", f"Frequency slot: {cfg.radio.frequency_slot}")
    logger.line("meshnet", f"TX power: {cfg.radio.tx_power} dBm (0 means firmware default)")
    logger.line("meshnet", f"Flower bridge: {'enabled' if cfg.bridge.enabled else 'disabled'}")
    for peer in cfg.network.peers:
        logger.line("meshnet", f"Peer: {peer.app_id} -> {peer.mesh_id or 'discovery required'}")
    logger.line("meshnet", f"Channel PSK: {channel_psk_description(cfg)}")
    logger.line("meshnet", f"Telegram: {'configured' if cfg.telegram.configured else 'not configured'}")
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
    cfg = load_for_args(args)
    setup_radio_reliably(cfg)
    return 0


def command_run(args: argparse.Namespace) -> int:
    cfg = load_for_args(args)
    if cfg.bridge.enabled:
        run_flower_bridge(cfg)
        return 0
    if cfg.app.role == "master":
        run_master_runtime(cfg)
    else:
        run_slave(cfg)
    return 0


def command_bridge(args: argparse.Namespace) -> int:
    cfg = load_for_args(args)
    if not cfg.bridge.enabled:
        raise MeshNetError(
            "BRIDGE_DISABLED",
            "bridge",
            "bridge.enabled is false in this config",
            "Use a Flower bridge config and set bridge.enabled: true.",
        )
    run_flower_bridge(cfg)
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
    text = args.text or args.message
    if not text:
        logger.line("meshnet", "Missing message text.")
        logger.detail('Use: meshnet send "hello"', indent=8)
        return 2
    node = MasterNode(cfg, "mesh")
    try:
        node.connect()
        result = node.send_text_message(text, args.dst or cfg.peer_node_id)
        return 0 if result.ok else 1
    finally:
        node.close()


def command_test(args: argparse.Namespace) -> int:
    cfg = load_and_preflight(args)
    return 0 if run_tests(cfg) else 1


def command_registry(args: argparse.Namespace) -> int:
    cfg = load_for_args(args)
    store = StateStore.for_config(cfg)
    try:
        nodes = store.list_nodes()
        if not nodes:
            logger.line("registry", "No paired or discovered nodes yet.")
            return 0
        logger.line("registry", "Known nodes")
        for node in nodes:
            flags = []
            if node.identity_changed:
                flags.append("IDENTITY_CHANGED")
            if not node.trusted:
                flags.append("UNTRUSTED")
            suffix = f" [{' '.join(flags)}]" if flags else ""
            logger.detail(
                f"{node.app_id} -> {node.mesh_id} role={node.role or '?'} "
                f"last_seen={format_age(node.last_seen)} fp={node.config_fingerprint or '?'}{suffix}",
                indent=8,
            )
    finally:
        store.close()
    return 0


def command_status(args: argparse.Namespace) -> int:
    cfg = load_for_args(args)
    store = StateStore.for_config(cfg)
    try:
        logger.line("status", f"Role: {cfg.app.role}")
        logger.line("status", f"Node ID: {cfg.app.node_id}")
        logger.line(
            "status",
            "Peers: " + ", ".join(
                f"{peer.app_id}->{peer.mesh_id or '?'}" for peer in cfg.network.peers
            ),
        )
        logger.line("status", f"Config fingerprint: {config_fingerprint(cfg)}")
        logger.line("status", f"Telegram: {'configured' if cfg.telegram.configured else 'not configured'}")
        for configured_peer in cfg.network.peers:
            peer = store.get_node(configured_peer.app_id)
            if peer is None:
                logger.line("status", f"Peer registry {configured_peer.app_id}: unknown")
            else:
                logger.line(
                    "status",
                    f"Peer registry {configured_peer.app_id}: {peer.mesh_id} "
                    f"last_seen={format_age(peer.last_seen)} "
                    f"{'IDENTITY_CHANGED' if peer.identity_changed else 'trusted'}",
                )
        recent = store.recent_outbound(5)
        if recent:
            logger.line("status", "Recent outbound")
            for row in recent:
                logger.detail(
                    f"{row['message_type']} -> {row['dst']} status={row['status']} "
                    f"attempts={row['attempts']} updated={format_age(float(row['updated_at']))}",
                    indent=8,
                )
    finally:
        store.close()
    return 0


def command_doctor(args: argparse.Namespace) -> int:
    cfg = load_and_preflight(args)
    logger.line("doctor", "Runtime configuration")
    logger.detail(f"role={cfg.app.role} node_id={cfg.app.node_id}", indent=8)
    logger.detail(f"region={cfg.radio.region} modem={cfg.radio.modem_preset}", indent=8)
    logger.detail(
        f"frequency_slot={cfg.radio.frequency_slot} tx_power={cfg.radio.tx_power}dBm",
        indent=8,
    )
    logger.detail(f"channel_index={cfg.radio.channel_index} channel={cfg.radio.channel_name}", indent=8)
    logger.detail(f"hop_limit={cfg.radio.hop_limit} tx_enabled={cfg.radio.transmit_enabled}", indent=8)
    logger.detail(
        f"device_role={cfg.device.role} rebroadcast={cfg.device.rebroadcast_mode} serial={cfg.device.serial_enabled}",
        indent=8,
    )
    logger.detail(f"fingerprint={config_fingerprint(cfg)}", indent=8)
    logger.blank()
    logger.line("doctor", "Radio probe")
    node = MasterNode(cfg, "doctor")
    config_ok = False
    config_mismatches: list[str] = []
    try:
        node.connect()
        logger.detail(f"local_mesh_id={node.radio.local_mesh_id()}", indent=8)
        actual = node.radio.actual_config_summary()
        if actual:
            config_mismatches = radio_config_mismatches(cfg, actual)
            config_ok = not config_mismatches
            logger.line("doctor", "Radio config comparison")
            print_config_check("region", cfg.radio.region, actual.get("lora.region"))
            print_config_check("modem", cfg.radio.modem_preset, actual.get("lora.modem_preset"))
            print_config_check("hop_limit", cfg.radio.hop_limit, actual.get("lora.hop_limit"))
            print_config_check("tx_enabled", cfg.radio.transmit_enabled, actual.get("lora.tx_enabled"))
            print_config_check("frequency_slot", cfg.radio.frequency_slot, actual.get("lora.channel_num"))
            print_config_check("tx_power", cfg.radio.tx_power, actual.get("lora.tx_power"))
            print_config_check("device_role", cfg.device.role, actual.get("device.role"))
            print_config_check(
                "rebroadcast_mode",
                cfg.device.rebroadcast_mode,
                actual.get("device.rebroadcast_mode"),
            )
            print_config_check(
                "node_info_broadcast_secs",
                cfg.device.node_info_broadcast_secs,
                actual.get("device.node_info_broadcast_secs"),
            )
            print_config_check(
                "serial_enabled",
                cfg.device.serial_enabled,
                actual.get("device.serial_enabled"),
            )
            print_config_check(
                "power_saving",
                cfg.device.is_power_saving,
                actual.get("power.is_power_saving"),
            )
            print_config_check("channel_name", cfg.radio.channel_name, actual.get("channel.name"))
            expected_psk = channel_psk_for_cli(cfg)
            expected_psk_b64 = expected_psk.removeprefix("base64:") if expected_psk != "none" else ""
            psk_match = expected_psk_b64 == actual.get("channel.psk_base64")
            logger.detail(
                f"channel_psk: {'OK' if psk_match else 'MISMATCH'} "
                f"(actual_len={actual.get('channel.psk_len', '?')})",
                indent=8,
            )
        else:
            logger.detail("actual_radio_config=unavailable", indent=8)
        known = node.radio.known_nodes()
        logger.detail(f"known_nodes={len(known)}", indent=8)
        for item in known[:10]:
            logger.detail(f"- {item.mesh_id} {item.long_name} {item.short_name}", indent=10)
    finally:
        node.close()
    logger.blank()
    registry_status = command_registry(args)
    if not config_ok:
        error = MeshNetError(
            "RADIO_CONFIG_MISMATCH",
            "doctor",
            "the attached radio does not match the active YAML config",
            "Run meshnet setup for this node, then run meshnet doctor again.",
            details={"mismatches": config_mismatches},
        )
        log_meshnet_error(error, "doctor")
        return 1
    return registry_status


def print_config_check(name: str, expected: object, actual: object) -> None:
    logger.detail(
        f"{name}: {'OK' if str(expected).lower() == str(actual).lower() else 'MISMATCH'} "
        f"(expected={expected} actual={actual})",
        indent=8,
    )


def command_trust(args: argparse.Namespace) -> int:
    cfg = load_for_args(args)
    store = StateStore.for_config(cfg)
    try:
        store.trust_node(args.app_id, args.mesh_id)
    finally:
        store.close()
    logger.line("registry", f"Trusted {args.app_id} -> {args.mesh_id}")
    return 0


def command_unpair(args: argparse.Namespace) -> int:
    cfg = load_for_args(args)
    store = StateStore.for_config(cfg)
    try:
        store.remove_node(args.app_id)
    finally:
        store.close()
    logger.line("registry", f"Removed {args.app_id}")
    return 0


def command_monitor(args: argparse.Namespace) -> int:
    while True:
        command_status(args)
        logger.blank()
        time.sleep(max(1, int(args.interval)))


def command_telegram(args: argparse.Namespace) -> int:
    cfg = load_and_preflight(args)
    logger.line("tg", "Using unified master runtime. Normal command: meshnet master")
    run_master_runtime(cfg)
    return 0


def command_telegram_id(args: argparse.Namespace) -> int:
    cfg = load_for_args(args)
    return print_telegram_chat_ids(cfg)


def command_preflight(args: argparse.Namespace) -> int:
    cfg = load_for_args(args)
    result = preflight_check(cfg)
    if result.ok:
        return 0
    error = as_meshnet_error(
        RuntimeError("; ".join(result.errors) or "preflight failed"),
        "preflight",
    )
    log_meshnet_error(error, "preflight")
    return 1


def command_how_to(_: argparse.Namespace) -> int:
    logger.line("how-to", "Basic MeshNet flow")
    logger.detail("1. Install on both Pis: cd meshnet && ./install.sh", indent=8)
    logger.detail("2. Edit config.master.yaml on the master and config.slave.yaml on the slave.", indent=8)
    logger.detail("3. Keep network_id, network_password, region, channel, PSK mode, and modem preset identical.", indent=8)
    logger.detail("4. Run: meshnet check master   and   meshnet check slave", indent=8)
    logger.detail("5. Run once: meshnet setup master   and   meshnet setup slave", indent=8)
    logger.detail("6. Start the slave: meshnet slave", indent=8)
    logger.detail("7. Start the master: meshnet master", indent=8)
    logger.detail("8. Check health: meshnet status   or   meshnet doctor", indent=8)
    logger.blank()
    logger.line("how-to", "Telegram")
    logger.detail("Set TELEGRAM_BOT_TOKEN, send the bot a message, run meshnet telegram-id, then set TELEGRAM_ALLOWED_CHAT_ID.", indent=8)
    logger.detail("After that, use meshnet master. Telegram starts automatically when configured.", indent=8)
    logger.detail("If Telegram is missing or cannot start, MeshNet logs the reason and falls back to normal master runtime.", indent=8)
    logger.detail("Run only one MeshNet runtime per USB radio.", indent=8)
    return 0


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


def telegram_unavailable_reason(cfg) -> str:
    missing = []
    if not cfg.telegram.bot_token:
        missing.append("TELEGRAM_BOT_TOKEN")
    if not cfg.telegram.allowed_chat_id:
        missing.append("TELEGRAM_ALLOWED_CHAT_ID")
    if missing:
        return "missing " + " and ".join(missing)
    return ""


def run_master_runtime(cfg) -> None:
    if cfg.app.role != "master":
        raise RuntimeError("master runtime requires a master config")

    reason = telegram_unavailable_reason(cfg)
    if reason:
        logger.line("tg", f"Telegram inactive: {reason}.")
        logger.line("master", "Starting normal master runtime.")
        run_master(cfg)
        return

    logger.line("tg", "Telegram configured; starting unified Telegram + master runtime.")
    for attempt in range(1, TELEGRAM_START_ATTEMPTS + 1):
        try:
            logger.line("tg", f"Startup attempt {attempt}/{TELEGRAM_START_ATTEMPTS}.")
            run_telegram_bridge(cfg)
            return
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            logger.line(
                "tg",
                f"Telegram runtime failed on attempt {attempt}/{TELEGRAM_START_ATTEMPTS}: {exc}",
            )
            if attempt < TELEGRAM_START_ATTEMPTS:
                logger.line("tg", f"Retrying Telegram in {TELEGRAM_RETRY_DELAY_SECONDS} seconds.")
                time.sleep(TELEGRAM_RETRY_DELAY_SECONDS)

    logger.line("tg", "Telegram unavailable after retries.")
    logger.line("master", "Starting normal master runtime.")
    run_master(cfg)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.how_to or args.command == "how-to":
            return command_how_to(args)
        if args.command is None:
            parser.print_help()
            return 2
        if args.command in ("start", "check", "setup", "nodes"):
            apply_role_config(args)
        if args.command == "detect":
            return command_detect(args)
        if args.command == "telegram-id":
            return command_telegram_id(args)
        if args.command == "status":
            return command_status(args)
        if args.command == "registry":
            return command_registry(args)
        if args.command == "doctor":
            return command_doctor(args)
        if args.command == "trust":
            return command_trust(args)
        if args.command == "unpair":
            return command_unpair(args)
        if args.command == "monitor":
            return command_monitor(args)
        if args.command in ("master", "slave", "start"):
            return command_run(args)
        if args.command == "check":
            return command_preflight(args)
        if args.command == "setup":
            return command_setup_radio(args)
        if args.command == "nodes":
            return command_discover(args)
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
        if args.command == "bridge":
            return command_bridge(args)
        parser.error(f"unknown command: {args.command}")
        return 2
    except KeyboardInterrupt:
        logger.line("meshnet", "Interrupted.")
        return 130
    except MeshNetError as exc:
        log_meshnet_error(exc)
        return 1
    except Exception as exc:
        log_meshnet_error(as_meshnet_error(exc, "command"))
        return 1


if __name__ == "__main__":
    sys.exit(main())
