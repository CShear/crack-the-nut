"""Base exchange adapter interface."""

from __future__ import annotations

import abc
from dataclasses import dataclass
from decimal import Decimal
from typing import Any


@dataclass
class OrderResult:
    """Result of an order submission."""

    order_id: str
    asset: str
    side: str  # "buy" or "sell"
    size: Decimal
    price: Decimal | None
    status: str  # "filled", "partial", "pending", "rejected"
    filled_size: Decimal = Decimal(0)
    avg_fill_price: Decimal | None = None
    fees: Decimal = Decimal(0)
    raw: dict[str, Any] | None = None


@dataclass
class BalanceInfo:
    """Account balance snapshot."""

    total_equity: Decimal
    available_margin: Decimal
    positions_value: Decimal
    unrealized_pnl: Decimal
    currency: str = "USD"


class ExchangeAdapter(abc.ABC):
    """Base class for exchange adapters.

    Each exchange (Hyperliquid, Polymarket, Binance, DEXs) implements this
    interface so strategies and execution are exchange-agnostic.
    """

    name: str = "base"

    @abc.abstractmethod
    async def connect(self) -> None:
        """Initialize connections, authenticate."""
        ...

    @abc.abstractmethod
    async def disconnect(self) -> None:
        """Clean up connections."""
        ...

    @abc.abstractmethod
    async def get_balance(self) -> BalanceInfo:
        """Get current account balance."""
        ...

    @abc.abstractmethod
    async def place_order(
        self,
        asset: str,
        side: str,
        size: Decimal,
        price: Decimal | None = None,
        order_type: str = "market",
    ) -> OrderResult:
        """Place an order. price=None for market orders."""
        ...

    @abc.abstractmethod
    async def cancel_order(self, order_id: str) -> bool:
        """Cancel an open order."""
        ...

    @abc.abstractmethod
    async def get_positions(self) -> list[dict[str, Any]]:
        """Get all open positions."""
        ...

    @abc.abstractmethod
    async def get_orderbook(self, asset: str) -> dict[str, Any]:
        """Get current order book for an asset."""
        ...

    async def get_funding_rate(self, asset: str) -> float | None:
        """Get current funding rate. Override for perp exchanges."""
        return None

    async def subscribe_trades(self, asset: str, callback) -> None:
        """Subscribe to real-time trade stream. Override for WS-capable exchanges."""
        raise NotImplementedError(f"{self.name} does not support trade subscriptions")
