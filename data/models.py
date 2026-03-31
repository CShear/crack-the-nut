"""Shared data models used across the toolkit."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


class Severity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


@dataclass
class Trade:
    """A completed or open trade."""

    trade_id: str
    symbol: str
    side: str  # "long" or "short"
    entry_price: float
    size: float
    exit_price: float | None = None
    pnl: float = 0.0
    fees: float = 0.0
    funding_pnl: float = 0.0
    paper_trade: bool = False
    status: str = "open"  # open, closed, cancelled
    opened_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    closed_at: datetime | None = None

    @property
    def is_open(self) -> bool:
        return self.status == "open"

    @property
    def net_pnl(self) -> float:
        return self.pnl - self.fees + self.funding_pnl


@dataclass
class PortfolioSnapshot:
    """Point-in-time portfolio state."""

    equity: float
    margin_used: float = 0.0
    unrealized_pnl: float = 0.0
    open_positions: int = 0
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def free_margin(self) -> float:
        return self.equity - self.margin_used


@dataclass
class Alert:
    """A triggered alert with optional cooldown."""

    severity: Severity
    message: str
    rule_name: str
    cooldown_seconds: int = 3600
    fired_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def is_cooled_down(self, last_fired: datetime | None) -> bool:
        """Check if enough time has passed since last fire."""
        if last_fired is None:
            return True
        elapsed = (self.fired_at - last_fired).total_seconds()
        return elapsed >= self.cooldown_seconds
