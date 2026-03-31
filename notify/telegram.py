"""Telegram notification bot for trading alerts."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import structlog

logger = structlog.get_logger()

# Telegram rate limit: 30 messages/second, 20 messages/minute to same chat
_MIN_INTERVAL = 1.0 / 20  # conservative: 20 msg/min per chat


class TelegramNotifier:
    """Async Telegram bot wrapper with rate limiting.

    Usage::

        notifier = TelegramNotifier(token="...", chat_id="...")
        await notifier.start()
        await notifier.send_alert("New signal: BTC LONG @ 65,000")
        await notifier.stop()
    """

    def __init__(self, token: str, chat_id: str, prefix: str = ""):
        self.token = token
        self.chat_id = chat_id
        self.prefix = prefix  # e.g. "[HL]" or "[PM]"
        self._bot: Any = None
        self._last_send: float = 0.0

    async def start(self) -> None:
        """Initialize the bot. Requires python-telegram-bot."""
        if not self.token:
            logger.warning("telegram_disabled", reason="no token")
            return
        try:
            from telegram import Bot

            self._bot = Bot(token=self.token)
            me = await self._bot.get_me()
            logger.info("telegram_connected", bot=me.username)
        except Exception as e:
            logger.error("telegram_init_failed", error=str(e))
            self._bot = None

    async def stop(self) -> None:
        """Shutdown the bot."""
        self._bot = None

    async def send_alert(self, message: str, parse_mode: str = "HTML") -> bool:
        """Send a message with rate limiting. Returns True if sent."""
        if self._bot is None:
            logger.debug("telegram_skip", reason="bot not initialized")
            return False

        # Rate limiting
        now = time.monotonic()
        elapsed = now - self._last_send
        if elapsed < _MIN_INTERVAL:
            await asyncio.sleep(_MIN_INTERVAL - elapsed)

        text = f"{self.prefix} {message}".strip() if self.prefix else message
        try:
            await self._bot.send_message(
                chat_id=self.chat_id,
                text=text,
                parse_mode=parse_mode,
            )
            self._last_send = time.monotonic()
            return True
        except Exception as e:
            logger.error("telegram_send_failed", error=str(e))
            return False

    # -- Formatting helpers --

    @staticmethod
    def format_trade(
        symbol: str,
        side: str,
        price: float,
        size_usd: float,
        pnl: float | None = None,
        paper: bool = False,
    ) -> str:
        """Format a trade notification."""
        emoji = "\U0001f4c8" if side.upper() in ("LONG", "BUY") else "\U0001f4c9"
        tag = " [PAPER]" if paper else ""
        lines = [
            f"{emoji} <b>{side.upper()} {symbol}</b>{tag}",
            f"Price: ${price:,.2f}",
            f"Size: ${size_usd:,.2f}",
        ]
        if pnl is not None:
            sign = "+" if pnl >= 0 else ""
            lines.append(f"PnL: {sign}${pnl:,.2f}")
        return "\n".join(lines)

    @staticmethod
    def format_daily_report(
        date: str,
        trades: int,
        pnl: float,
        win_rate: float | None,
        equity: float,
    ) -> str:
        """Format a daily summary report."""
        sign = "+" if pnl >= 0 else ""
        wr = f"{win_rate:.0f}%" if win_rate is not None else "N/A"
        return (
            f"\U0001f4ca <b>Daily Report \u2014 {date}</b>\n"
            f"Trades: {trades}\n"
            f"PnL: {sign}${pnl:,.2f}\n"
            f"Win Rate: {wr}\n"
            f"Equity: ${equity:,.2f}"
        )

    @staticmethod
    def format_signal(
        symbol: str,
        direction: str,
        confidence: float,
        entry: float,
        source: str = "",
    ) -> str:
        """Format a signal alert."""
        emoji = "\U0001f7e2" if direction.upper() in ("LONG", "BUY") else "\U0001f534"
        lines = [
            f"{emoji} <b>Signal: {direction.upper()} {symbol}</b>",
            f"Confidence: {confidence:.0f}/100",
            f"Entry: ${entry:,.2f}",
        ]
        if source:
            lines.append(f"Source: {source}")
        return "\n".join(lines)
