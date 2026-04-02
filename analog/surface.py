"""Funding rate surface — the "yield curve" of crypto.

Treats cross-asset funding rates as a structured signal object.
The *shape* of the funding surface (mean, dispersion, skew, momentum,
extreme count) is a regime signal that almost nobody trades systematically.

Usage::

    engine = FundingSurfaceEngine(top_n=20)

    # Feed funding snapshots (call every 1-8h with all available rates)
    engine.record({"BTC": 0.0003, "ETH": 0.0001, "SOL": -0.0012, ...})

    # Get current surface
    surface = engine.current()
    # surface.mean, surface.dispersion, surface.skew, ...

    # Get surface features as dict (for fingerprint)
    features = engine.features()
"""

from __future__ import annotations

import math
import time
from collections import deque
from dataclasses import dataclass, field

import structlog

logger = structlog.get_logger()


@dataclass
class FundingSurface:
    """Snapshot of the cross-asset funding rate surface."""

    timestamp: float
    n_assets: int
    mean: float  # average funding across assets
    dispersion: float  # std dev — how spread out are rates
    skew: float  # >0 = longs paying more, <0 = shorts paying
    min_rate: float
    max_rate: float
    extreme_count: int  # assets with |rate| > 2 sigma from trailing mean
    rates: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, float]:
        """Feature dict for fingerprint integration."""
        return {
            "funding_mean": self.mean,
            "funding_dispersion": self.dispersion,
            "funding_skew": self.skew,
            "funding_range": self.max_rate - self.min_rate,
            "funding_extreme_count": float(self.extreme_count),
            "funding_n_assets": float(self.n_assets),
        }


@dataclass
class _SurfaceSnapshot:
    timestamp: float
    rates: dict[str, float]
    mean: float


class FundingSurfaceEngine:
    """Computes funding surface features from cross-asset funding rates.

    Args:
        top_n: Only use the top N most liquid assets. 0 = use all.
        extreme_threshold_sigma: Number of std devs to count as "extreme".
        history_window: Seconds of history to keep for momentum calc.
    """

    def __init__(
        self,
        top_n: int = 20,
        extreme_threshold_sigma: float = 2.0,
        history_window: float = 86400.0,  # 24h
    ):
        self.top_n = top_n
        self.extreme_threshold_sigma = extreme_threshold_sigma
        self.history_window = history_window
        self._history: deque[_SurfaceSnapshot] = deque(maxlen=200)
        self._latest: FundingSurface | None = None

    def record(
        self,
        rates: dict[str, float],
        timestamp: float | None = None,
    ) -> FundingSurface:
        """Record a funding rate snapshot across assets. Returns the surface."""
        ts = timestamp or time.time()

        if not rates:
            raise ValueError("No funding rates provided")

        # Take top_n by absolute rate if configured
        if self.top_n > 0 and len(rates) > self.top_n:
            sorted_assets = sorted(rates.keys(), key=lambda a: abs(rates[a]), reverse=True)
            rates = {a: rates[a] for a in sorted_assets[: self.top_n]}

        values = list(rates.values())
        n = len(values)

        mean = sum(values) / n
        variance = sum((v - mean) ** 2 for v in values) / n if n > 1 else 0.0
        std = math.sqrt(variance)

        # Skew: positive = longs paying more on average
        if std > 0 and n > 2:
            skew = sum(((v - mean) / std) ** 3 for v in values) / n
        else:
            skew = 0.0

        # Count extremes relative to the surface mean
        threshold = self.extreme_threshold_sigma * std if std > 0 else float("inf")
        extreme_count = sum(1 for v in values if abs(v - mean) > threshold)

        surface = FundingSurface(
            timestamp=ts,
            n_assets=n,
            mean=round(mean, 8),
            dispersion=round(std, 8),
            skew=round(skew, 4),
            min_rate=min(values),
            max_rate=max(values),
            extreme_count=extreme_count,
            rates=rates,
        )

        self._history.append(_SurfaceSnapshot(timestamp=ts, rates=rates, mean=mean))
        self._latest = surface
        return surface

    def current(self) -> FundingSurface | None:
        """Return the most recent surface, or None if no data."""
        return self._latest

    def momentum(self, lookback_hours: float = 8.0) -> float | None:
        """Change in surface mean over the lookback period.

        Positive = funding trending toward longs paying more.
        Negative = trending toward shorts paying more.
        """
        if len(self._history) < 2:
            return None

        cutoff = time.time() - (lookback_hours * 3600)
        older = [s for s in self._history if s.timestamp <= cutoff]
        if not older:
            return None

        old_mean = older[-1].mean
        new_mean = self._history[-1].mean
        return round(new_mean - old_mean, 8)

    def features(self) -> dict[str, float]:
        """Full feature dict for fingerprint integration."""
        if self._latest is None:
            return {
                "funding_mean": 0.0,
                "funding_dispersion": 0.0,
                "funding_skew": 0.0,
                "funding_range": 0.0,
                "funding_extreme_count": 0.0,
                "funding_n_assets": 0.0,
                "funding_momentum_8h": 0.0,
            }

        features = self._latest.to_dict()
        mom = self.momentum(lookback_hours=8.0)
        features["funding_momentum_8h"] = mom if mom is not None else 0.0
        return features
