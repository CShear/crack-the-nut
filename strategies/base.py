"""Base strategy interface. All strategies implement this."""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class Direction(str, Enum):
    LONG = "long"
    SHORT = "short"


@dataclass
class Signal:
    """A trading signal emitted by a strategy."""

    asset: str
    direction: Direction
    confidence: float  # 0.0 - 1.0
    source: str  # strategy name
    entry_price: float | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    size_pct: float | None = None  # % of bankroll to risk
    metadata: dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=datetime.utcnow)


@dataclass
class Position:
    """An open position."""

    asset: str
    direction: Direction
    entry_price: float
    size: float
    opened_at: datetime
    stop_loss: float | None = None
    take_profit: float | None = None
    unrealized_pnl: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class Candle:
    """OHLCV candle."""

    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    asset: str = ""
    interval: str = "1m"
    metadata: dict[str, Any] = field(default_factory=dict)


class Strategy(abc.ABC):
    """Base class for all strategies.

    Implement on_data(), should_enter(), and should_exit().
    The framework calls these — you don't need to manage the event loop.
    """

    name: str = "unnamed"
    description: str = ""

    def __init__(self, config: dict[str, Any] | None = None):
        self.config = config or {}
        self.state: dict[str, Any] = {}

    @abc.abstractmethod
    async def on_data(self, candle: Candle) -> None:
        """Called on every new data point. Update internal state here."""
        ...

    @abc.abstractmethod
    async def should_enter(self) -> Signal | None:
        """Evaluate whether to open a new position. Return Signal or None."""
        ...

    @abc.abstractmethod
    async def should_exit(self, position: Position) -> bool:
        """Evaluate whether to close an existing position."""
        ...

    async def on_fill(self, position: Position) -> None:
        """Called when an order fills. Override for post-fill logic."""
        pass

    async def on_close(self, position: Position, pnl: float) -> None:
        """Called when a position closes. Override for post-close logic."""
        pass
