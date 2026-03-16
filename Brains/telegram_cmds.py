"""Telegram bot for Brains strategy commands.

Uses Bagbot's own Telegram bot token (not tao_spy_bot).
Runs in a background thread alongside the main bot loop.
"""

import asyncio
import logging
import os
import threading
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from Brains.integration import StrategyEngine

logger = logging.getLogger(__name__)

# Lazy import to avoid requiring telegram lib when not used
_tg = None
_tg_ext = None


def _ensure_telegram():
    global _tg, _tg_ext
    if _tg is None:
        import telegram
        import telegram.ext
        _tg = telegram
        _tg_ext = telegram.ext


class BagbotTelegram:
    """Telegram bot interface for Brains strategy commands."""

    def __init__(self, token: str, engine: 'StrategyEngine'):
        _ensure_telegram()
        self.engine = engine
        self.token = token
        self._app = None
        self._thread = None
        self._loop = None
        self._bot = None

    def start(self):
        """Start the Telegram bot in a background thread."""
        self._thread = threading.Thread(target=self._run_bot, daemon=True)
        self._thread.start()
        logger.info('Brains Telegram bot started in background thread')

    def _run_bot(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._async_run())

    async def _async_run(self):
        self._app = _tg_ext.ApplicationBuilder().token(self.token).build()

        # Register command handlers
        self._app.add_handler(_tg_ext.CommandHandler('show_strategy', self._cmd_show_strategy))
        self._app.add_handler(_tg_ext.CommandHandler('set_risk', self._cmd_set_risk))
        self._app.add_handler(_tg_ext.CommandHandler('pause_buys', self._cmd_pause_buys))
        self._app.add_handler(_tg_ext.CommandHandler('resume_buys', self._cmd_resume_buys))
        self._app.add_handler(_tg_ext.CommandHandler('brains', self._cmd_brains_help))

        # Also handle plain text commands (without /)
        self._app.add_handler(_tg_ext.MessageHandler(
            _tg_ext.filters.TEXT & ~_tg_ext.filters.COMMAND,
            self._handle_text,
        ))

        # Store bot reference for send_async
        self._bot = self._app.bot

        await self._app.initialize()
        await self._app.start()
        await self._app.updater.start_polling()

        # Run forever
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            pass
        finally:
            await self._app.updater.stop()
            await self._app.stop()
            await self._app.shutdown()

    def send_async(self, msg: str, chat_id: Optional[int] = None):
        """Send a message asynchronously. If no chat_id, logs instead."""
        if self._bot is None or self._loop is None:
            logger.debug(f'Telegram bot not ready, skipping message: {msg}')
            return
        if chat_id is None:
            # Without a known chat_id we can only log
            logger.info(f'Telegram (no chat): {msg}')
            return
        asyncio.run_coroutine_threadsafe(
            self._bot.send_message(chat_id=chat_id, text=msg),
            self._loop,
        )

    async def _handle_text(self, update, context):
        """Handle plain text that looks like commands."""
        text = (update.message.text or '').strip().lower()
        if text.startswith('show strategy'):
            await self._cmd_show_strategy(update, context)
        elif text.startswith('set risk'):
            await self._cmd_set_risk(update, context)
        elif text.startswith('pause buys'):
            await self._cmd_pause_buys(update, context)
        elif text.startswith('resume buys'):
            await self._cmd_resume_buys(update, context)

    async def _cmd_brains_help(self, update, context):
        msg = (
            "Brains Strategy Commands:\n"
            "/show_strategy [netuid] - Show strategy status\n"
            "/set_risk <conservative|balanced|aggressive> - Set risk mode\n"
            "/pause_buys <netuid> - Pause buys for a subnet\n"
            "/resume_buys <netuid> - Resume buys for a subnet\n"
        )
        await update.message.reply_text(msg)

    async def _cmd_show_strategy(self, update, context):
        """Show strategy status for a subnet or all subnets."""
        args = context.args if context.args else []
        text = (update.message.text or '').strip()

        # Parse netuid from args or text
        target_netuid = None
        if args:
            try:
                target_netuid = int(args[0])
            except ValueError:
                pass
        elif 'show strategy' in text.lower():
            parts = text.split()
            for p in parts:
                try:
                    target_netuid = int(p)
                    break
                except ValueError:
                    continue

        patches = self.engine.get_all_patches()
        if not patches:
            await update.message.reply_text(
                f"Brains: No active strategy patches.\n"
                f"Mode: {self.engine.risk_mode} | Dry run: {self.engine.dry_run}"
            )
            return

        if target_netuid is not None:
            if target_netuid in patches:
                await update.message.reply_text(
                    self._format_patch(patches[target_netuid])
                )
            else:
                await update.message.reply_text(f"No strategy patch for SN{target_netuid}")
            return

        # Show all
        lines = [f"Brains: {self.engine.risk_mode} mode | Dry run: {self.engine.dry_run}"]
        for netuid in sorted(patches.keys()):
            lines.append(self._format_patch(patches[netuid]))
        await update.message.reply_text('\n\n'.join(lines))

    def _format_patch(self, patch) -> str:
        state = self.engine.state_store.get(patch.netuid)
        return (
            f"SN{patch.netuid} strategy\n"
            f"Regime: {patch.regime}\n"
            f"Confidence: {patch.confidence:.2f}\n"
            f"Buy zone: {patch.buy_lower:.6f} - {patch.buy_upper:.6f}\n"
            f"Sell zone: {patch.sell_lower:.6f} - {patch.sell_upper:.6f}\n"
            f"Max buy: {patch.max_tao_per_buy:.3f} TAO\n"
            f"Max sell: {patch.max_tao_per_sell:.3f} TAO\n"
            f"Buys: {'ON' if patch.enable_buys else 'OFF'} | "
            f"Sells: {'ON' if patch.enable_sells else 'OFF'}\n"
            f"Cost basis: {state.avg_entry_price:.6f if state.avg_entry_price else 'unknown'}\n"
            f"Reason: {patch.reason}\n"
            f"{'DRY RUN' if patch.dry_run else 'LIVE'}"
        )

    async def _cmd_set_risk(self, update, context):
        args = context.args if context.args else []
        text = (update.message.text or '').strip().lower()

        mode = None
        valid_modes = ['conservative', 'balanced', 'aggressive']
        for m in valid_modes:
            if m in text or (args and m in [a.lower() for a in args]):
                mode = m
                break

        if mode is None:
            await update.message.reply_text(
                f"Usage: /set_risk <conservative|balanced|aggressive>\n"
                f"Current: {self.engine.risk_mode}"
            )
            return

        self.engine.risk_mode = mode
        await update.message.reply_text(f"Risk mode set to: {mode}")
        logger.info(f'Brains risk mode changed to {mode} via Telegram')

    async def _cmd_pause_buys(self, update, context):
        args = context.args if context.args else []
        text = (update.message.text or '').strip()

        netuid = self._parse_netuid(args, text)
        if netuid is None:
            await update.message.reply_text("Usage: /pause_buys <netuid>")
            return

        patch = self.engine.get_patch(netuid)
        if patch is not None:
            patch.enable_buys = False
            await update.message.reply_text(f"Buys paused for SN{netuid}")
            logger.info(f'Brains: buys paused for sn{netuid} via Telegram')
        else:
            await update.message.reply_text(f"No active patch for SN{netuid}")

    async def _cmd_resume_buys(self, update, context):
        args = context.args if context.args else []
        text = (update.message.text or '').strip()

        netuid = self._parse_netuid(args, text)
        if netuid is None:
            await update.message.reply_text("Usage: /resume_buys <netuid>")
            return

        patch = self.engine.get_patch(netuid)
        if patch is not None:
            patch.enable_buys = True
            await update.message.reply_text(f"Buys resumed for SN{netuid}")
            logger.info(f'Brains: buys resumed for sn{netuid} via Telegram')
        else:
            await update.message.reply_text(f"No active patch for SN{netuid}")

    def _parse_netuid(self, args, text):
        if args:
            try:
                return int(args[0])
            except ValueError:
                pass
        parts = text.split()
        for p in parts:
            try:
                return int(p)
            except ValueError:
                continue
        return None


def setup_telegram(engine: 'StrategyEngine', token_path: str) -> Optional[BagbotTelegram]:
    """Load token from file and start the Telegram bot.

    Args:
        engine: The StrategyEngine instance.
        token_path: Path to a file containing the bot token (or TOKEN=xxx format).

    Returns:
        BagbotTelegram instance if successful, None otherwise.
    """
    if not token_path or not os.path.exists(token_path):
        logger.info(f'Telegram token path not found: {token_path}, skipping Telegram')
        return None

    try:
        with open(token_path, 'r') as f:
            content = f.read().strip()

        # Support TOKEN=xxx or plain token
        token = content
        for line in content.splitlines():
            line = line.strip()
            if '=' in line:
                key, val = line.split('=', 1)
                if key.strip().upper() in ('TOKEN', 'TELEGRAM_TOKEN', 'BOT_TOKEN'):
                    token = val.strip().strip('"').strip("'")
                    break

        if not token:
            logger.warning('Empty Telegram token')
            return None

        bot = BagbotTelegram(token, engine)
        bot.start()
        return bot

    except Exception as e:
        logger.error(f'Failed to setup Telegram bot: {e}')
        return None
