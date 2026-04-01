"""Polymarket market filters — liquidity and resolution-date awareness.

Two filters that should run before any entry decision:

1. LiquidityFilter — checks order book depth before entering.
   Thin markets produce bad fills that destroy edge. If there isn't enough
   size at your target price, skip the trade.

2. ResolutionDecay — reduces position size as resolution date approaches.
   Liquidity dries up near resolution. Spreads widen. The Kelly-optimal
   position is smaller with 3 days left than with 30 days left — even if
   your probability estimate hasn't changed.

Usage::

    from exchanges.polymarket.market_filter import LiquidityFilter, ResolutionDecay
    from datetime import datetime, timezone

    # Filter thin markets
    liq = LiquidityFilter(min_depth_usd=200, max_spread=0.04)
    orderbook = await adapter.get_orderbook(token_id)
    ok, reason = liq.check(orderbook, target_price=0.55, size_usd=50.0)
    if not ok:
        logger.info("skipping_thin_market", reason=reason)
        return

    # Decay position size near resolution
    decay = ResolutionDecay()
    resolution_date = datetime(2025, 11, 5, tzinfo=timezone.utc)
    kelly_size = sizer.size(edge=0.08, confidence=75, bankroll=500)
    adjusted_size = decay.adjust(kelly_size, resolution_date)
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

import structlog

logger = structlog.get_logger()


@dataclass
class LiquidityFilter:
    """Check order book depth before entering a position.

    Args:
        min_depth_usd: Minimum USDC available within max_spread_from_price
            of the target entry price. Default $200.
        max_spread: Maximum acceptable bid-ask spread as a fraction.
            e.g. 0.04 = 4 cents on a $0.50 market. Default 0.04.
        max_spread_from_price: How far from target price to count liquidity.
            Default 0.03 (3 cents).
    """

    min_depth_usd: float = 200.0
    max_spread: float = 0.04
    max_spread_from_price: float = 0.03

    def check(
        self,
        orderbook: dict,
        target_price: float,
        size_usd: float,
    ) -> tuple[bool, str | None]:
        """Check if the market has enough liquidity to fill this order cleanly.

        Args:
            orderbook: From adapter.get_orderbook() — {"bids": [...], "asks": [...]}
            target_price: Your intended entry price.
            size_usd: Intended position size in USDC.

        Returns:
            (True, None) if OK to trade, (False, reason_string) if not.
        """
        bids = orderbook.get("bids", [])
        asks = orderbook.get("asks", [])

        if not bids or not asks:
            return False, "empty_orderbook"

        # Compute spread
        best_bid = bids[0][0] if bids else 0.0
        best_ask = asks[0][0] if asks else 1.0
        spread = best_ask - best_bid

        if spread > self.max_spread:
            return False, f"spread_too_wide: {spread:.3f} > {self.max_spread}"

        # Count available depth near target price (for buy orders, look at asks)
        available_depth = sum(
            price * qty
            for price, qty in asks
            if abs(price - target_price) <= self.max_spread_from_price
        )

        if available_depth < min(size_usd, self.min_depth_usd):
            return False, (
                f"insufficient_depth: ${available_depth:.0f} "
                f"within {self.max_spread_from_price} of {target_price:.2f}"
            )

        return True, None


@dataclass
class ResolutionDecay:
    """Decay position size as resolution date approaches.

    Why: Liquidity dries up near resolution. Spreads widen. You can't exit
    at a fair price if you need to. The Kelly-optimal position is smaller
    with 3 days left than with 30 days left.

    Exception: If you have very high confidence (>85%) and the market hasn't
    fully priced your estimate, holding through resolution can be fine.
    Use the high_confidence_threshold to control this.

    Args:
        full_size_days: Days until resolution where you use full Kelly size.
            Default 14 days — inside 2 weeks, decay kicks in.
        min_size_fraction: Minimum fraction of Kelly to deploy near resolution.
            Default 0.25 (use at most 25% of Kelly inside final 3 days).
        final_days_cutoff: Inside this many days, apply min_size_fraction.
            Default 3 days.
        high_confidence_threshold: Above this confidence (0–100), skip decay.
            Default 87 — only skip for very high conviction trades.
    """

    full_size_days: int = 14
    min_size_fraction: float = 0.25
    final_days_cutoff: int = 3
    high_confidence_threshold: float = 87.0

    def days_until_resolution(self, resolution_date: datetime) -> float:
        now = datetime.now(timezone.utc)
        delta = resolution_date - now
        return max(0.0, delta.total_seconds() / 86400)

    def decay_fraction(self, days_remaining: float) -> float:
        """Return fraction of Kelly size to use (0.0–1.0)."""
        if days_remaining >= self.full_size_days:
            return 1.0
        if days_remaining <= self.final_days_cutoff:
            return self.min_size_fraction
        # Linear decay between full_size_days and final_days_cutoff
        span = self.full_size_days - self.final_days_cutoff
        progress = (days_remaining - self.final_days_cutoff) / span
        return self.min_size_fraction + progress * (1.0 - self.min_size_fraction)

    def adjust(
        self,
        kelly_size: float,
        resolution_date: datetime,
        confidence: float = 50.0,
    ) -> float:
        """Return decayed position size.

        Args:
            kelly_size: Raw Kelly-sized position in USD.
            resolution_date: When the market resolves.
            confidence: LLM confidence score 0–100. Very high confidence
                skips the decay.

        Returns:
            Adjusted position size in USD.
        """
        if confidence >= self.high_confidence_threshold:
            logger.debug("resolution_decay_skipped", reason="high_confidence", confidence=confidence)
            return kelly_size

        days = self.days_until_resolution(resolution_date)
        fraction = self.decay_fraction(days)
        adjusted = kelly_size * fraction

        if fraction < 1.0:
            logger.debug(
                "resolution_decay_applied",
                days_remaining=round(days, 1),
                fraction=round(fraction, 3),
                original=kelly_size,
                adjusted=round(adjusted, 2),
            )

        return round(adjusted, 2)
