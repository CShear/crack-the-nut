"""Whale copy-trading strategy.

Generalized from the Hyperliquid bot's whale_tracker. Monitors large
wallets on any exchange and copies their trades with delay and smaller size.

Usage::

    strategy = WhaleCopyStrategy(
        min_trade_usd=50_000,
        convergence_window_sec=300,
        min_whales=2,
    )
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from strategies.base import Strategy, Signal, Position, Candle, Direction


@dataclass
class WhaleTrade:
    """A detected whale trade."""

    wallet: str
    symbol: str
    side: str  # "buy" or "sell"
    size_usd: float
    timestamp: datetime


class WhaleCopyStrategy(Strategy):
    """Copy trades when multiple whales converge on the same direction.

    Parameters:
        min_trade_usd: Minimum trade size to qualify as a whale trade.
        convergence_window_sec: Time window to look for whale convergence.
        min_whales: Minimum distinct whales needed for a signal.
        signal_threshold: Minimum confidence to emit a signal (0-1).
        decay_factor: Score decay per update cycle (0-1).
    """

    def __init__(
        self,
        min_trade_usd: float = 50_000,
        convergence_window_sec: int = 300,
        min_whales: int = 2,
        signal_threshold: float = 0.6,
        decay_factor: float = 0.98,
    ):
        super().__init__()
        self.min_trade_usd = min_trade_usd
        self.convergence_window_sec = convergence_window_sec
        self.min_whales = min_whales
        self.signal_threshold = signal_threshold
        self.decay_factor = decay_factor

        self._whale_trades: list[WhaleTrade] = []
        self._scores: dict[str, float] = {}  # symbol → score
        self._current_candle: Candle | None = None

    async def on_data(self, candle: Candle) -> None:
        self._current_candle = candle
        # Decay all scores
        for sym in self._scores:
            self._scores[sym] *= self.decay_factor

    def ingest_whale_trade(self, trade: WhaleTrade) -> None:
        """Feed a whale trade into the strategy. Call from your data pipeline."""
        if trade.size_usd < self.min_trade_usd:
            return
        self._whale_trades.append(trade)
        # Prune old trades outside the convergence window
        now = datetime.now(timezone.utc)
        cutoff = now.timestamp() - self.convergence_window_sec
        self._whale_trades = [t for t in self._whale_trades if t.timestamp.timestamp() > cutoff]

    async def should_enter(self) -> Signal | None:
        if self._current_candle is None:
            return None

        symbol = self._current_candle.asset
        now = datetime.now(timezone.utc)
        cutoff = now.timestamp() - self.convergence_window_sec

        # Find recent whale trades for this symbol
        recent = [t for t in self._whale_trades if t.symbol == symbol and t.timestamp.timestamp() > cutoff]

        if not recent:
            return None

        # Count distinct whales per direction
        buyers = set(t.wallet for t in recent if t.side == "buy")
        sellers = set(t.wallet for t in recent if t.side == "sell")

        if len(buyers) >= self.min_whales and len(buyers) > len(sellers):
            direction = Direction.LONG
            confidence = min(1.0, len(buyers) / (len(buyers) + len(sellers) + 1))
        elif len(sellers) >= self.min_whales and len(sellers) > len(buyers):
            direction = Direction.SHORT
            confidence = min(1.0, len(sellers) / (len(buyers) + len(sellers) + 1))
        else:
            return None

        if confidence < self.signal_threshold:
            return None

        return Signal(
            asset=symbol,
            direction=direction,
            confidence=confidence,
            entry_price=self._current_candle.close,
            source="whale_copy",
        )

    async def should_exit(self, position: Position) -> bool:
        if self._current_candle is None:
            return False
        # Simple SL/TP exit
        pnl_pct = (
            (self._current_candle.close - position.entry_price) / position.entry_price
            if position.direction == Direction.LONG
            else (position.entry_price - self._current_candle.close) / position.entry_price
        )
        return pnl_pct <= -0.03 or pnl_pct >= 0.06  # 3% SL, 6% TP

    async def on_fill(self, position: Position) -> None:
        pass

    async def on_close(self, position: Position, pnl: float) -> None:
        pass
