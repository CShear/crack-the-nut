"""Funding rate arbitrage strategy.

Harvests extreme funding rates on perpetual futures. When funding is very
positive (longs pay shorts), go short to collect. When very negative, go long.

This is a reference implementation — tune thresholds and risk params for your setup.
"""

from __future__ import annotations

from strategies.base import Candle, Direction, Position, Signal, Strategy


class FundingArbStrategy(Strategy):
    """Collect funding rate payments when rates are extreme."""

    name = "funding_arb"
    description = "Short when funding is very positive, long when very negative"

    # Default config — override via config dict
    DEFAULTS = {
        "funding_threshold": 0.01,  # 1% hourly funding = extreme
        "size_pct": 0.05,  # 5% of bankroll per position
        "stop_loss_pct": 0.03,  # 3% stop loss
        "take_profit_pct": 0.02,  # 2% take profit (we're here for funding, not directional)
        "max_positions": 3,
    }

    def __init__(self, config=None):
        super().__init__(config)
        for k, v in self.DEFAULTS.items():
            if k not in self.config:
                self.config[k] = v
        self.current_funding: dict[str, float] = {}

    async def on_data(self, candle: Candle) -> None:
        """Track funding rates from candle metadata."""
        if "funding_rate" in candle.metadata:
            self.current_funding[candle.asset] = candle.metadata["funding_rate"]

    async def should_enter(self) -> Signal | None:
        """Enter when funding rate exceeds threshold."""
        threshold = self.config["funding_threshold"]

        for asset, rate in self.current_funding.items():
            if abs(rate) < threshold:
                continue

            direction = Direction.SHORT if rate > 0 else Direction.LONG

            return Signal(
                asset=asset,
                direction=direction,
                confidence=min(abs(rate) / threshold, 1.0),
                source=self.name,
                size_pct=self.config["size_pct"],
                stop_loss=None,  # set by execution layer based on entry price
                take_profit=None,
                metadata={"funding_rate": rate},
            )

        return None

    async def should_exit(self, position: Position) -> bool:
        """Exit when funding rate normalizes or SL/TP hit."""
        rate = self.current_funding.get(position.asset, 0.0)
        threshold = self.config["funding_threshold"]

        # Funding normalized — the trade thesis is over
        if abs(rate) < threshold * 0.3:
            return True

        # Funding flipped against us
        if position.direction == Direction.SHORT and rate < 0:
            return True
        if position.direction == Direction.LONG and rate > 0:
            return True

        return False
