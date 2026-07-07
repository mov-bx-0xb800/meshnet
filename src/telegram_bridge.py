from __future__ import annotations

import asyncio
import contextlib
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from dotenv import load_dotenv

from . import logger
from .config import MeshConfig
from .errors import MeshNetError
from .master import MasterNode
from .protocol import Envelope, decode_envelope, payload_hash, verify_envelope


@dataclass
class BridgeStats:
    started_at: float = field(default_factory=time.time)
    tg_text_in: int = 0
    tg_messages_out: int = 0
    mesh_messages_in: int = 0
    mesh_messages_out: int = 0
    mesh_text_in: int = 0
    mesh_text_out: int = 0
    pings_sent: int = 0
    pings_ok: int = 0
    pings_timeout: int = 0
    discoveries: int = 0
    compatible_nodes: int = 0
    tests_run: int = 0
    tests_passed: int = 0
    tests_failed: int = 0
    last_mesh_in_at: float | None = None
    last_mesh_out_at: float | None = None
    last_tg_in_at: float | None = None
    last_ping_rtt: float | None = None
    last_error: str = ""


class TelegramBridge:
    def __init__(self, cfg: MeshConfig) -> None:
        self.cfg = cfg
        self.node = MasterNode(cfg, "tg")
        self.app: Any = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.stats = BridgeStats()
        self._stats_lock = threading.Lock()
        self._mesh_lock = threading.RLock()
        self.events_enabled = True

    async def start(self) -> None:
        try:
            from telegram import Update
            from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters
        except Exception as exc:
            raise RuntimeError(
                "python-telegram-bot is not installed. Run ./install.sh first."
            ) from exc

        if self.cfg.app.role != "master":
            raise RuntimeError("Telegram bridge only runs with the master config.")
        if not self.cfg.telegram.bot_token:
            raise RuntimeError("telegram.bot_token is required. Set TELEGRAM_BOT_TOKEN in .env.")
        if not self.cfg.telegram.allowed_chat_id:
            raise RuntimeError(
                "telegram.allowed_chat_id is required. Send the bot a message, then run: meshnet telegram-id"
            )

        logger.line("tg", "Telegram bridge configured.")
        logger.line("tg", "Connecting radio...")
        self.loop = asyncio.get_running_loop()
        self.node.connect()
        self.node.radio.add_handler(self._on_mesh_packet)

        logger.line("tg", "Connecting bot...")
        try:
            self.app = ApplicationBuilder().token(self.cfg.telegram.bot_token).build()
        except Exception:
            self.node.close()
            raise

        async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            if not self._allowed(update):
                return
            await self._reply(
                update,
                "MeshNet Telegram bridge ready.\n"
                "Text messages are sent to the mesh.\n"
                "Commands: /status /stats /nodes /discover /ping /test /events",
            )

        async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            if not self._allowed(update):
                return
            await self._reply(update, self._status_text())

        async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            if not self._allowed(update):
                return
            await self._reply(update, self._stats_text())

        async def nodes_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            if not self._allowed(update):
                return
            nodes = self.node.radio.known_nodes()
            if not nodes:
                await self._reply(update, "No known Meshtastic nodes in the local node DB yet.")
                return
            lines = []
            for n in nodes:
                signal = []
                if n.rssi is not None:
                    signal.append(f"rssi={n.rssi}")
                if n.snr is not None:
                    signal.append(f"snr={n.snr}")
                suffix = f" ({', '.join(signal)})" if signal else ""
                lines.append(f"{n.mesh_id} {n.long_name} {n.short_name}{suffix}".strip())
            await self._reply(update, "\n".join(lines))

        async def discover_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            if not self._allowed(update):
                return
            await self._reply(update, "Starting discovery.")
            with self._stats_lock:
                self.stats.discoveries += 1
            found = await asyncio.to_thread(
                self._discover_compatible,
                self.cfg.runtime.discovery_timeout_seconds,
            )
            with self._stats_lock:
                self.stats.compatible_nodes = len(found)
            if found:
                nodes = ", ".join(env.src for env in found)
                await self._reply(update, f"Reached {len(found)} compatible node(s): {nodes}")
            else:
                await self._reply(update, "No compatible nodes reached.")

        async def ping_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            if not self._allowed(update):
                return
            with self._stats_lock:
                self.stats.pings_sent += 1
            ok, rtt, _ = await asyncio.to_thread(self._ping_peer)
            with self._stats_lock:
                if ok and rtt is not None:
                    self.stats.pings_ok += 1
                    self.stats.last_ping_rtt = rtt
                else:
                    self.stats.pings_timeout += 1
            if ok and rtt is not None:
                await self._reply(update, f"Pong from {self.cfg.network.slave_id}: {rtt:.2f}s")
            else:
                await self._reply(update, "Ping timed out.")

        async def test_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            if not self._allowed(update):
                return
            await self._reply(update, "Running quick mesh test.")
            with self._stats_lock:
                self.stats.tests_run += 1
            passed, lines = await asyncio.to_thread(self._run_quick_test)
            with self._stats_lock:
                if passed:
                    self.stats.tests_passed += 1
                else:
                    self.stats.tests_failed += 1
            await self._reply(update, "\n".join(lines))

        async def events_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            if not self._allowed(update):
                return
            choice = context.args[0].lower() if context.args else ""
            if choice in {"on", "yes", "1"}:
                self.events_enabled = True
            elif choice in {"off", "no", "0"}:
                self.events_enabled = False
            await self._reply(update, f"Mesh event notifications: {'on' if self.events_enabled else 'off'}")

        async def send_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            if not self._allowed(update):
                return
            text = " ".join(context.args).strip()
            if not text:
                await self._reply(update, 'Usage: /send hello')
                return
            await self._send_mesh_text(update, text)

        async def text_msg(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            if not self._allowed(update):
                return
            text = update.message.text.strip()
            await self._send_mesh_text(update, text)

        self.app.add_handler(CommandHandler("start", start_cmd))
        self.app.add_handler(CommandHandler("help", start_cmd))
        self.app.add_handler(CommandHandler("status", status_cmd))
        self.app.add_handler(CommandHandler("stats", stats_cmd))
        self.app.add_handler(CommandHandler("nodes", nodes_cmd))
        self.app.add_handler(CommandHandler("discover", discover_cmd))
        self.app.add_handler(CommandHandler("ping", ping_cmd))
        self.app.add_handler(CommandHandler("test", test_cmd))
        self.app.add_handler(CommandHandler("events", events_cmd))
        self.app.add_handler(CommandHandler("send", send_cmd))
        self.app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_msg))

        logger.line("tg", f"Allowed chat ID: {self.cfg.telegram.allowed_chat_id}")
        app_initialized = False
        app_started = False
        polling_started = False
        monitor_task: asyncio.Task[None] | None = None
        try:
            await self.app.initialize()
            app_initialized = True
            await self.app.start()
            app_started = True
            if self.app.updater is None:
                raise RuntimeError("Telegram updater is unavailable.")
            await self.app.updater.start_polling()
            polling_started = True
            logger.line("tg", "Bot polling started.")
            logger.line("tg", "Ready.")
            monitor_task = asyncio.create_task(self._master_monitor())
            await self._notify("MeshNet Telegram bridge started.")
            while True:
                await asyncio.sleep(3600)
        finally:
            if monitor_task is not None:
                monitor_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await monitor_task
            if self.app is not None:
                if polling_started and self.app.updater is not None:
                    with contextlib.suppress(Exception):
                        await self.app.updater.stop()
                if app_started:
                    with contextlib.suppress(Exception):
                        await self.app.stop()
                if app_initialized:
                    with contextlib.suppress(Exception):
                        await self.app.shutdown()
            self.node.close()

    async def _master_monitor(self) -> None:
        logger.line("master", f"Looking for slave: {self.cfg.network.slave_id}")
        with self._stats_lock:
            self.stats.discoveries += 1
        compatible = await asyncio.to_thread(
            self._discover_compatible,
            self.cfg.runtime.discovery_timeout_seconds,
        )
        with self._stats_lock:
            self.stats.compatible_nodes = len(compatible)
        if compatible:
            logger.line("master", f"TRUE NODE found: {self.cfg.network.slave_id}")
        else:
            logger.line("master", "No compatible slave found yet; continuing runtime checks.")

        seq = 0
        next_heartbeat = time.monotonic()
        while True:
            try:
                if time.monotonic() >= next_heartbeat:
                    await asyncio.to_thread(self._send_heartbeat)
                    logger.line("master", "Heartbeat broadcast sent.")
                    next_heartbeat = time.monotonic() + self.cfg.runtime.heartbeat_interval_seconds

                seq += 1
                logger.line("master", f"Ping -> {self.cfg.network.slave_id} seq={seq}")
                with self._stats_lock:
                    self.stats.pings_sent += 1
                ok, rtt, _ = await asyncio.to_thread(self._ping_peer, seq)
                with self._stats_lock:
                    if ok and rtt is not None:
                        self.stats.pings_ok += 1
                        self.stats.last_ping_rtt = rtt
                    else:
                        self.stats.pings_timeout += 1
                if ok and rtt is not None:
                    logger.line(
                        "master",
                        f"Pong <- {self.cfg.network.slave_id} seq={seq} rtt={rtt:.1f}s",
                    )
                else:
                    logger.line("master", f"Pong timeout from {self.cfg.network.slave_id} seq={seq}")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                with self._stats_lock:
                    self.stats.last_error = str(exc)
                logger.line("master", f"Runtime check failed: {exc}")

            await asyncio.sleep(self.cfg.safe_ping_interval)

    def _discover_compatible(self, timeout_seconds: int) -> list[Envelope]:
        with self._mesh_lock:
            self._ensure_radio_connected_locked()
            return self.node.discover(timeout_seconds)

    def _ping_peer(self, seq: int | None = None) -> tuple[bool, float | None, Envelope]:
        with self._mesh_lock:
            self._ensure_radio_connected_locked()
            return self.node.ping_once(self.cfg.network.slave_id, seq=seq)

    def _send_heartbeat(self) -> Envelope:
        with self._mesh_lock:
            self._ensure_radio_connected_locked()
            return self.node.send("heartbeat", dst="broadcast", body={"role": "master"}, seq=0)

    def _send_mesh_text_sync(self, text: str) -> Envelope:
        with self._mesh_lock:
            self._ensure_radio_connected_locked()
            result = self.node.send_text_message(text, self.cfg.network.slave_id)
            if not result.ok:
                raise MeshNetError(
                    result.error_code or "DELIVERY_FAILED",
                    "delivery",
                    result.last_error or "message delivery failed",
                    result.action or "Check the peer runtime and matching radio configuration.",
                    retryable=result.retryable,
                    attempts=result.attempts,
                )
            return result.envelope

    def _ensure_radio_connected_locked(self) -> None:
        if self.node.radio.is_connected():
            return
        logger.line("tg", "Radio offline; starting reconnect.")
        self.node.close()
        node = MasterNode(self.cfg, "tg")
        try:
            node.connect()
        except Exception:
            node.close()
            raise
        node.radio.add_handler(self._on_mesh_packet)
        self.node = node
        logger.line("tg", "Radio reconnected.")

    def _allowed(self, update: Any) -> bool:
        chat = getattr(update, "effective_chat", None)
        chat_id = str(getattr(chat, "id", ""))
        allowed = chat_id == str(self.cfg.telegram.allowed_chat_id)
        if not allowed:
            logger.line("tg", f"Ignored Telegram message from unauthorized chat: {chat_id or 'unknown'}")
        return allowed

    async def _reply(self, update: Any, text: str) -> None:
        message = getattr(update, "message", None) or getattr(update, "effective_message", None)
        if message is None:
            return
        await message.reply_text(text)
        with self._stats_lock:
            self.stats.tg_messages_out += 1

    async def _notify(self, text: str) -> None:
        if self.app is None:
            return
        await self.app.bot.send_message(chat_id=self.cfg.telegram.allowed_chat_id, text=text)
        with self._stats_lock:
            self.stats.tg_messages_out += 1

    def _notify_from_radio_thread(self, text: str) -> None:
        if self.loop is None:
            return
        asyncio.run_coroutine_threadsafe(self._notify(text), self.loop)

    async def _send_mesh_text(self, update: Any, text: str) -> None:
        with self._stats_lock:
            self.stats.tg_text_in += 1
            self.stats.last_tg_in_at = time.time()
        try:
            envelope = await asyncio.to_thread(
                self._send_mesh_text_sync,
                text,
            )
        except MeshNetError as exc:
            with self._stats_lock:
                self.stats.last_error = f"{exc.code}: {exc.message}"
            await self._reply(
                update,
                f"Mesh send failed [{exc.code}]\nProblem: {exc.message}\nFix: {exc.action}",
            )
            return
        except Exception as exc:
            with self._stats_lock:
                self.stats.last_error = str(exc)
            await self._reply(update, f"Mesh send failed: {exc}")
            return
        with self._stats_lock:
            self.stats.mesh_messages_out += 1
            self.stats.mesh_text_out += 1
            self.stats.last_mesh_out_at = time.time()
        await self._reply(update, f"TX text -> {envelope.dst} id={envelope.id}")

    def _on_mesh_packet(self, text: str, packet: dict[str, Any]) -> None:
        try:
            env = decode_envelope(text)
        except Exception:
            return
        ok, _ = verify_envelope(env, self.cfg)
        if not ok or env.src == self.cfg.app.node_id:
            return

        with self._stats_lock:
            self.stats.mesh_messages_in += 1
            self.stats.last_mesh_in_at = time.time()
            if env.t == "text":
                self.stats.mesh_text_in += 1

        event = self._event_text(env, packet)
        if event and self.events_enabled:
            self._notify_from_radio_thread(event)

    def _event_text(self, env: Envelope, packet: dict[str, Any]) -> str:
        if env.t == "text":
            return f"RX text <- {env.src}\n{env.body}"
        if env.t == "status_res":
            return f"RX status <- {env.src}\n{env.body}"
        if env.t == "error":
            return f"RX error <- {env.src}\n{env.body}"
        return ""

    def _run_quick_test(self) -> tuple[bool, list[str]]:
        with self._mesh_lock:
            self._ensure_radio_connected_locked()
            return self._run_quick_test_locked()

    def _run_quick_test_locked(self) -> tuple[bool, list[str]]:
        lines: list[str] = []
        passed = True

        compatible = self.node.discover(self.cfg.runtime.discovery_timeout_seconds)
        with self._stats_lock:
            self.stats.compatible_nodes = len(compatible)
        if compatible:
            lines.append(f"PASS discovery: {len(compatible)} compatible node(s)")
        else:
            lines.append("FAIL discovery: no compatible node reached")
            passed = False

        with self._stats_lock:
            self.stats.pings_sent += 1
        ok, rtt, _ = self.node.ping_once(self.cfg.network.slave_id)
        with self._stats_lock:
            if ok and rtt is not None:
                self.stats.pings_ok += 1
                self.stats.last_ping_rtt = rtt
            else:
                self.stats.pings_timeout += 1
        if ok and rtt is not None:
            lines.append(f"PASS ping: {rtt:.2f}s")
        else:
            lines.append("FAIL ping: timeout")
            passed = False

        seq = self.node.next_seq()
        payload = f"telegram-test-{seq}"
        result = self.node.send_reliable(
            "test",
            dst=self.cfg.network.slave_id,
            body={"payload": payload, "hash": payload_hash(payload)},
            seq=seq,
            expect_reply_type="test_ack",
        )
        with self._stats_lock:
            self.stats.mesh_messages_out += 1
            self.stats.last_mesh_out_at = time.time()
        ack_ok = bool(
            result.reply is not None
            and isinstance(result.reply.envelope.body, dict)
            and result.reply.envelope.body.get("ok")
        )
        if ack_ok:
            lines.append("PASS test payload: ack received")
        else:
            lines.append("FAIL test payload: no valid ack")
            passed = False

        lines.append(f"Final result: {'PASS' if passed else 'FAIL'}")
        return passed, lines

    def _status_text(self) -> str:
        radio_port = self.node.radio.port or self.cfg.radio.port
        with self._stats_lock:
            last_ping = (
                f"{self.stats.last_ping_rtt:.2f}s"
                if self.stats.last_ping_rtt is not None
                else "none"
            )
            error = self.stats.last_error or "none"
        return (
            "MeshNet bridge: online\n"
            f"node: {self.cfg.app.node_id} ({self.cfg.app.role})\n"
            f"peer: {self.cfg.network.slave_id}\n"
            f"network: {self.cfg.network.network_id}\n"
            f"channel: {self.cfg.radio.channel_name}\n"
            f"radio: {radio_port}\n"
            f"events: {'on' if self.events_enabled else 'off'}\n"
            f"uptime: {format_duration(time.time() - self.stats.started_at)}\n"
            f"last ping: {last_ping}\n"
            f"last error: {error}"
        )

    def _stats_text(self) -> str:
        with self._stats_lock:
            stats = BridgeStats(**self.stats.__dict__)
        return (
            "MeshNet stats\n"
            f"uptime: {format_duration(time.time() - stats.started_at)}\n"
            f"telegram text in: {stats.tg_text_in}\n"
            f"telegram messages out: {stats.tg_messages_out}\n"
            f"mesh messages in/out: {stats.mesh_messages_in}/{stats.mesh_messages_out}\n"
            f"mesh text in/out: {stats.mesh_text_in}/{stats.mesh_text_out}\n"
            f"pings ok/timeout: {stats.pings_ok}/{stats.pings_timeout}\n"
            f"discoveries: {stats.discoveries}\n"
            f"compatible nodes: {stats.compatible_nodes}\n"
            f"tests pass/fail: {stats.tests_passed}/{stats.tests_failed}\n"
            f"last mesh RX: {format_timestamp(stats.last_mesh_in_at)}\n"
            f"last mesh TX: {format_timestamp(stats.last_mesh_out_at)}"
        )


def format_duration(seconds: float) -> str:
    total = int(seconds)
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes}m {secs}s"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def format_timestamp(value: float | None) -> str:
    if value is None:
        return "never"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(value))


async def _print_telegram_chat_ids_async(cfg: MeshConfig) -> int:
    try:
        from telegram import Bot
    except Exception as exc:
        raise RuntimeError("python-telegram-bot is not installed. Run ./install.sh first.") from exc

    if not cfg.telegram.bot_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is required in .env or config.")

    bot = Bot(cfg.telegram.bot_token)
    updates = await bot.get_updates(timeout=5)
    if not updates:
        logger.line("tg", "No Telegram updates found.")
        logger.line("tg", "Open Telegram, send any message to your bot, then run this again.")
        return 1

    seen: set[str] = set()
    logger.line("tg", "Recent chat IDs:")
    for update in updates:
        chat = update.effective_chat
        if chat is None:
            continue
        chat_id = str(chat.id)
        if chat_id in seen:
            continue
        seen.add(chat_id)
        title = chat.title or chat.full_name or chat.username or "unknown"
        logger.detail(f"{chat_id}  {chat.type}  {title}", indent=8)
    return 0


def print_telegram_chat_ids(cfg: MeshConfig) -> int:
    load_dotenv()
    return asyncio.run(_print_telegram_chat_ids_async(cfg))


def run_telegram_bridge(cfg: MeshConfig) -> None:
    load_dotenv()
    bridge = TelegramBridge(cfg)
    asyncio.run(bridge.start())
