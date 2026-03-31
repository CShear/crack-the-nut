"""Multi-factor confidence scoring framework.

Generalized from the Polymarket bot's ConfidenceScorer and the TAO monitor's
weighted health scoring. Register sub-scores with weights, get a composite.

Usage::

    scorer = CompositeScorer()
    scorer.register(SubScore("whale_consensus", weight=0.30, score_fn=score_whales))
    scorer.register(SubScore("ai_divergence", weight=0.30, score_fn=score_ai))
    scorer.register(SubScore("liquidity", weight=0.20, score_fn=score_liquidity))
    scorer.register(SubScore("momentum", weight=0.20, score_fn=score_momentum))

    result = scorer.score(data)
    # result.total = weighted average (0-100)
    # result.components = {"whale_consensus": 75.0, ...}
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

import structlog

logger = structlog.get_logger()


@dataclass
class ScoreResult:
    """Result of a composite scoring run."""

    total: float  # 0-100 weighted average
    components: dict[str, float] = field(default_factory=dict)  # name → score
    weights: dict[str, float] = field(default_factory=dict)  # name → weight


@dataclass
class SubScore:
    """A named sub-score with a weight and scoring function.

    The score_fn receives arbitrary data and must return a float in [0, 100].
    """

    name: str
    weight: float
    score_fn: Callable[[Any], float]


class CompositeScorer:
    """Computes a weighted composite score from registered sub-scores."""

    def __init__(self) -> None:
        self._sub_scores: list[SubScore] = []

    def register(self, sub_score: SubScore) -> None:
        self._sub_scores.append(sub_score)

    def score(self, data: Any) -> ScoreResult:
        """Run all sub-scores against data and return weighted composite."""
        if not self._sub_scores:
            return ScoreResult(total=0.0)

        total_weight = sum(s.weight for s in self._sub_scores)
        components: dict[str, float] = {}
        weights: dict[str, float] = {}
        weighted_sum = 0.0

        for sub in self._sub_scores:
            try:
                raw = sub.score_fn(data)
                value = max(0.0, min(100.0, raw))  # clamp to [0, 100]
            except Exception as e:
                logger.warning("sub_score_failed", name=sub.name, error=str(e))
                value = 50.0  # neutral fallback

            components[sub.name] = round(value, 1)
            weights[sub.name] = sub.weight
            weighted_sum += value * sub.weight

        total = weighted_sum / total_weight if total_weight > 0 else 0.0
        return ScoreResult(
            total=round(min(100.0, total), 1),
            components=components,
            weights=weights,
        )
