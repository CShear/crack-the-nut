"""Prediction market momentum scorer.

Directional momentum in prediction markets is real. When a large trader
moves a market, smaller traders often follow. When a market drifts in one
direction for 24+ hours without a fundamental news event, mean reversion
is more likely than continuation. Understanding which regime you're in
is a useful signal.

This scorer tracks rolling probability changes and produces:
- A momentum signal (+1 trending YES, -1 trending NO, 0 neutral)
- A magnitude score (0–100) representing how strong the trend is
- A regime flag (trending vs reverting)

The scorer is designed to plug into the CompositeScorer as a SubScore.

Usage::

    from scoring.momentum import MomentumScorer

    scorer = MomentumScorer(window_hours=24, min_samples=4)

    # Feed price snapshots as they come in (call this each time you poll the market)
    scorer.record(token_id="0xabc123", price=0.52, timestamp=time.time())
    ...
    scorer.record(token_id="0xabc123", price=0.58, timestamp=time.time())

    # Get a score for use in CompositeScorer
    score = scorer.score(token_id="0xabc123")  # 0–100
    signal = scorer.signal(token_id="0xabc123")  # +1, -1, or 0
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from dataclasses import dataclass

import structlog

logger = structlog.get_logger()


@dataclass
class PricePoint:
    price: float
    timestamp: float  # unix seconds


class MomentumScorer:
    """Rolling momentum scorer for prediction market token prices.

    Args:
        window_hours: How far back to look for momentum. Default 24h.
        min_samples: Minimum price points needed to produce a signal. Default 3.
        strong_move_threshold: Price change over window that counts as "strong"
            momentum. e.g. 0.05 = 5 cent move. Default 0.05.
        noise_floor: Changes below this are treated as noise. Default 0.01.
    """

    def __init__(
        self,
        window_hours: float = 24.0,
        min_samples: int = 3,
        strong_move_threshold: float = 0.05,
        noise_floor: float = 0.01,
    ):
        self.window_seconds = window_hours * 3600
        self.min_samples = min_samples
        self.strong_move_threshold = strong_move_threshold
        self.noise_floor = noise_floor
        # token_id → deque of PricePoints (auto-expire old ones)
        self._history: dict[str, deque[PricePoint]] = defaultdict(
            lambda: deque(maxlen=500)
        )

    def record(
        self,
        token_id: str,
        price: float,
        timestamp: float | None = None,
    ) -> None:
        """Record a price observation for a token."""
        ts = timestamp or time.time()
        self._history[token_id].append(PricePoint(price=price, timestamp=ts))

    def _window_points(self, token_id: str) -> list[PricePoint]:
        """Return price points within the rolling window."""
        cutoff = time.time() - self.window_seconds
        return [p for p in self._history[token_id] if p.timestamp >= cutoff]

    def delta(self, token_id: str) -> float | None:
        """Price change over the window. None if insufficient data."""
        points = self._window_points(token_id)
        if len(points) < self.min_samples:
            return None
        return points[-1].price - points[0].price

    def signal(self, token_id: str) -> int:
        """Return +1 (trending YES), -1 (trending NO), or 0 (no signal)."""
        d = self.delta(token_id)
        if d is None:
            return 0
        if d > self.noise_floor:
            return 1
        if d < -self.noise_floor:
            return -1
        return 0

    def score(self, token_id: str) -> float:
        """Return a 0–100 score for use in CompositeScorer.

        Score meaning:
        - 50 = no momentum (neutral)
        - >50 = trending YES (higher = stronger upward move)
        - <50 = trending NO (lower = stronger downward move)
        """
        d = self.delta(token_id)
        if d is None:
            return 50.0  # neutral when no data

        # Normalize: strong_move_threshold → ±50 points from center
        normalized = (d / self.strong_move_threshold) * 50
        score = 50.0 + max(-50.0, min(50.0, normalized))
        return round(score, 1)

    def acceleration(self, token_id: str) -> float | None:
        """Rate of change of momentum (is the trend speeding up or slowing?).

        Returns positive if momentum is accelerating, negative if decelerating.
        None if insufficient data.
        """
        points = self._window_points(token_id)
        if len(points) < self.min_samples * 2:
            return None

        mid = len(points) // 2
        first_half = points[:mid]
        second_half = points[mid:]

        first_delta = first_half[-1].price - first_half[0].price
        second_delta = second_half[-1].price - second_half[0].price

        return round(second_delta - first_delta, 5)

    def is_mean_reverting(self, token_id: str) -> bool:
        """True if momentum is decelerating after a strong move.

        A market that moved hard and is now slowing down is more likely to
        revert than continue — useful for avoiding chasing moves.
        """
        d = self.delta(token_id)
        acc = self.acceleration(token_id)
        if d is None or acc is None:
            return False
        # Strong move + decelerating = potential reversion
        return abs(d) > self.strong_move_threshold and (d * acc < 0)
