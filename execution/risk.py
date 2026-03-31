"""Risk management primitives.

Every strategy runs through risk checks before execution.
These are guardrails — override config per-user, but never disable them entirely.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass
class RiskConfig:
    """Risk parameters. Each user configures their own."""

    max_position_size_pct: float = 0.10  # Max 10% of bankroll per position
    max_total_exposure_pct: float = 0.50  # Max 50% of bankroll in positions
    max_positions: int = 10
    max_daily_loss_pct: float = 0.05  # 5% daily drawdown = stop trading
    kill_switch_pct: float = 0.20  # 20% total drawdown = kill everything
    min_order_usd: float = 10.0


class RiskManager:
    """Evaluates whether a trade should be allowed."""

    def __init__(self, config: RiskConfig, bankroll: Decimal):
        self.config = config
        self.bankroll = bankroll
        self.daily_pnl = Decimal(0)
        self.open_positions = 0
        self.total_exposure = Decimal(0)

    def check_entry(self, size_usd: Decimal) -> tuple[bool, str]:
        """Check if a new entry is allowed. Returns (allowed, reason)."""

        # Kill switch
        if self.daily_pnl < 0 and abs(self.daily_pnl) / self.bankroll > Decimal(str(self.config.kill_switch_pct)):
            return False, f"Kill switch: daily loss {self.daily_pnl} exceeds {self.config.kill_switch_pct:.0%}"

        # Daily loss limit
        if self.daily_pnl < 0 and abs(self.daily_pnl) / self.bankroll > Decimal(str(self.config.max_daily_loss_pct)):
            return False, f"Daily loss limit: {self.daily_pnl} exceeds {self.config.max_daily_loss_pct:.0%}"

        # Position count
        if self.open_positions >= self.config.max_positions:
            return False, f"Max positions ({self.config.max_positions}) reached"

        # Position size
        max_size = self.bankroll * Decimal(str(self.config.max_position_size_pct))
        if size_usd > max_size:
            return False, f"Size ${size_usd} exceeds max ${max_size}"

        # Total exposure
        new_exposure = self.total_exposure + size_usd
        max_exposure = self.bankroll * Decimal(str(self.config.max_total_exposure_pct))
        if new_exposure > max_exposure:
            return False, f"Total exposure ${new_exposure} would exceed max ${max_exposure}"

        # Min order
        if size_usd < Decimal(str(self.config.min_order_usd)):
            return False, f"Size ${size_usd} below minimum ${self.config.min_order_usd}"

        return True, "ok"

    def record_pnl(self, pnl: Decimal) -> None:
        """Record realized PnL."""
        self.daily_pnl += pnl

    def reset_daily(self) -> None:
        """Reset daily PnL counter. Call at start of each trading day."""
        self.daily_pnl = Decimal(0)
