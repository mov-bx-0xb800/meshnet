from __future__ import annotations

import asyncio
from typing import Any

from dotenv import load_dotenv

from . import logger
from .config import MeshConfig
from .master import MasterNode
from .protocol import Envelope, decode_envelope, verify_envelope


class TelegramBridge:
    def __init__(self, cfg: MeshConfig) -> None:
        self.cfg = cfg
        self.node = MasterNode(cfg, "tg")
        self.app: Any = None
        self.loop: asyncio.AbstractEventLoop | None = None

    async def start(self) -> None:
        try:
            from telegram import Update
            from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters
        except Exception as exc:
            raise RuntimeError(
                "python-telegram-bot is not installed. Run ./install.sh first."
            ) from exc

        if not self.cfg.telegram.enabled:
            raise RuntimeError("telegram.enabled is false in the config")
        if not self.cfg.telegram.bot_token:
            raise RuntimeError("telegram.bot_token is required")
        if not self.cfg.telegram.allowed_chat_id:
            raise RuntimeError("telegram.allowed_chat_id is required")

        logger.line("tg", "Telegram bridge enabled.")
        logger.line("tg", "Connecting bot...")
        self.loop = asyncio.get_running_loop()
        self.node.connect()
        self.node.radio.add_handler(self._on_mesh_text)

        self.app = ApplicationBuilder().token(self.cfg.telegram.bot_token).build()

        async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            if not self._allowed(update):
                return
            await update.message.reply_text("MeshNet Telegram bridge ready.")

        async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            if not self._allowed(update):
                return
            await update.message.reply_text(
                f"role={self.cfg.app.role} node={self.cfg.app.node_id} channel={self.cfg.radio.channel_name}"
            )

        async def nodes_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            if not self._allowed(update):
                return
            nodes = self.node.radio.known_nodes()
            if not nodes:
                await update.message.reply_text("No known Meshtastic nodes in local node DB yet.")
                return
            lines = [f"{n.mesh_id} {n.long_name} {n.short_name}".strip() for n in nodes]
            await update.message.reply_text("\n".join(lines))

        async def discover_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            if not self._allowed(update):
                return
            await update.message.reply_text("Starting discovery.")
            found = await asyncio.to_thread(self.node.discover, self.cfg.runtime.discovery_timeout_seconds)
            await update.message.reply_text(f"Reached {len(found)} compatible node(s).")

        async def ping_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            if not self._allowed(update):
                return
            ok, rtt, _ = await asyncio.to_thread(self.node.ping_once, self.cfg.network.slave_id)
            if ok and rtt is not None:
                await update.message.reply_text(f"Pong from {self.cfg.network.slave_id}: {rtt:.2f}s")
            else:
                await update.message.reply_text("Ping timed out.")

        async def test_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            if not self._allowed(update):
                return
            await update.message.reply_text("Run full tests from the Pi CLI: python -m src.cli test --config config.master.yaml")

        async def text_msg(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            if not self._allowed(update):
                return
            text = update.message.text.strip()
            self.node.send_text_message(text, self.cfg.network.slave_id)
            await update.message.reply_text("Sent to mesh.")

        self.app.add_handler(CommandHandler("start", start_cmd))
        self.app.add_handler(CommandHandler("status", status_cmd))
        self.app.add_handler(CommandHandler("nodes", nodes_cmd))
        self.app.add_handler(CommandHandler("discover", discover_cmd))
        self.app.add_handler(CommandHandler("ping", ping_cmd))
        self.app.add_handler(CommandHandler("test", test_cmd))
        self.app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_msg))

        logger.line("tg", "Connected.")
        logger.line("tg", f"Allowed chat ID: {self.cfg.telegram.allowed_chat_id}")
        logger.line("tg", "Ready.")
        await self.app.initialize()
        await self.app.start()
        await self.app.updater.start_polling()
        try:
            while True:
                await asyncio.sleep(3600)
        finally:
            await self.app.updater.stop()
            await self.app.stop()
            await self.app.shutdown()
            self.node.close()

    def _allowed(self, update: Any) -> bool:
        chat = getattr(update, "effective_chat", None)
        chat_id = str(getattr(chat, "id", ""))
        return chat_id == str(self.cfg.telegram.allowed_chat_id)

    def _on_mesh_text(self, text: str, packet: dict[str, Any]) -> None:
        try:
            env = decode_envelope(text)
        except Exception:
            return
        ok, _ = verify_envelope(env, self.cfg)
        if not ok or env.t != "text" or env.src == self.cfg.app.node_id:
            return
        if self.app is None or self.loop is None:
            return
        body = f"Mesh text from {env.src}: {env.body}"
        asyncio.run_coroutine_threadsafe(
            self.app.bot.send_message(chat_id=self.cfg.telegram.allowed_chat_id, text=body),
            self.loop,
        )


def run_telegram_bridge(cfg: MeshConfig) -> None:
    load_dotenv()
    bridge = TelegramBridge(cfg)
    asyncio.run(bridge.start())
