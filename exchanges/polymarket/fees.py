"""Polymarket fee model and fee-adjusted edge calculation.

Polymarket charges taker fees that vary by market. Ignoring these is one
of the primary reasons paper-trading results don't survive to live trading.

From production experience:
- Standard markets: 2% taker fee (200 bps) on the USDC notional
- High-volume markets: sometimes 0% (fee_rate_bps=0 in order args)
- Spread cost: the bid-ask spread is an additional implicit cost on entry AND exit
- Round-trip cost: you pay spread twice (entry + exit) + fees twice

Usage::

    from exchanges.polymarket.fees import FeeModel, fee_adjusted_edge

    model = FeeModel(taker_fee_bps=200)
    real_edge = fee_adjusted_edge(
        raw_edge=0.08,          # LLM says 58% probability, market at 50%
        entry_price=0.50,
        taker_fee_bps=200,
        spread=0.02,            # 2 cent bid-ask spread
    )
    # real_edge will be significantly lower than 0.08
    # If it's negative, don't trade.
"""

from __future__ import annotations

from dataclasses import dataclass

import structlog

logger = structlog.get_logger()

# Polymarket's standard taker fee as of 2025
DEFAULT_TAKER_FEE_BPS = 200  # 2%


@dataclass
class FeeModel:
    """Polymarket fee parameters.

    Args:
        taker_fee_bps: Taker fee in basis points. Default 200 (2%).
            Check the specific market — some high-volume markets are 0 bps.
        min_spread_cents: Minimum expected spread to assume even if order book
            looks tight. Real fills are often worse than top-of-book.
    """

    taker_fee_bps: int = DEFAULT_TAKER_FEE_BPS
    min_spread_cents: float = 0.01  # 1 cent minimum assumed spread

    @property
    def taker_fee_rate(self) -> float:
        return self.taker_fee_bps / 10_000

    def entry_cost(self, price: float, size_usd: float) -> float:
        """Total cost to enter: taker fee + half spread."""
        fee = size_usd * self.taker_fee_rate
        half_spread = max(self.min_spread_cents / 2, 0.0) * (size_usd / price)
        return fee + half_spread

    def exit_cost(self, price: float, size_usd: float) -> float:
        """Total cost to exit: taker fee + half spread."""
        return self.entry_cost(price, size_usd)

    def round_trip_cost_fraction(self, price: float) -> float:
        """Round-trip cost as a fraction of position value.

        This is the minimum edge you need to break even on a trade.
        If your edge is below this, the trade destroys value even if you're right.
        """
        fee_cost = 2 * self.taker_fee_rate  # pay fee on entry and exit
        spread_cost = self.min_spread_cents / price  # pay spread on entry
        return fee_cost + spread_cost


def fee_adjusted_edge(
    raw_edge: float,
    entry_price: float,
    fee_model: FeeModel | None = None,
    spread: float | None = None,
) -> float:
    """Compute the real edge after fees and spread costs.

    Args:
        raw_edge: Your estimated probability minus market price.
            e.g. if you estimate 60% and market is at 50%, raw_edge = 0.10
        entry_price: The price you'll pay (0.01–0.99).
        fee_model: FeeModel instance. Defaults to standard 200 bps.
        spread: Observed bid-ask spread. If provided, overrides min_spread_cents.

    Returns:
        Adjusted edge after costs. If negative, the trade is not worth taking.
    """
    model = fee_model or FeeModel()

    if spread is not None:
        model = FeeModel(
            taker_fee_bps=model.taker_fee_bps,
            min_spread_cents=spread * 100,  # convert to cents
        )

    round_trip_cost = model.round_trip_cost_fraction(entry_price)
    adjusted = raw_edge - round_trip_cost

    if adjusted < 0:
        logger.debug(
            "edge_destroyed_by_fees",
            raw_edge=f"{raw_edge:.4f}",
            round_trip_cost=f"{round_trip_cost:.4f}",
            adjusted=f"{adjusted:.4f}",
        )

    return round(adjusted, 6)


def minimum_edge_to_trade(entry_price: float, fee_model: FeeModel | None = None) -> float:
    """The minimum raw edge required for a trade to be worth taking.

    Use this as a filter before running Kelly sizing. If your estimated
    edge is below this threshold, skip the trade entirely.

    Example: at standard 2% fees and a 1-cent spread on a $0.50 market,
    you need at least ~4% raw edge just to break even.
    """
    model = fee_model or FeeModel()
    return model.round_trip_cost_fraction(entry_price)
