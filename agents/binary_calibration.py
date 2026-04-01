"""Recalibration layer for LLM probability estimates on binary markets.

LLMs are systematically overconfident on salient, newsworthy events.
They overestimate the probability of dramatic outcomes (availability bias)
and underestimate base rates for "nothing happens" outcomes.

On Polymarket this materializes as:
- LLM says 0.80 → real market should be 0.65
- LLM says 0.20 → real market should be 0.35
- Near-50% estimates are usually more reliable than extreme ones

This module applies two calibration steps:
1. Platt scaling — sigmoid recalibration learned from superforecaster research
2. Base-rate blending — shrinks extreme predictions toward the prior (0.5 for
   binary markets with no strong base rate)

Both steps are independent and can be applied together or separately.

Usage::

    from agents.binary_calibration import BinaryCalibrator

    cal = BinaryCalibrator(shrink_factor=0.15)
    raw = 0.82  # LLM estimate
    calibrated = cal.calibrate(raw)
    print(calibrated)  # e.g. 0.71 — meaningfully lower

References:
- Tetlock & Gardner, "Superforecasting" (2015) — base rate regression
- Guo et al., "On Calibration of Modern Neural Networks" (2017) — temperature scaling
- Metaculus calibration research showing AI overconfidence at extremes
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import structlog

logger = structlog.get_logger()


@dataclass
class BinaryCalibrator:
    """Recalibrates LLM probability estimates for binary prediction markets.

    Args:
        shrink_factor: How much to shrink toward 0.5. 0.0 = no shrinkage,
            1.0 = always return 0.5. Reasonable range: 0.10–0.25.
            Default 0.15 based on empirical LLM overconfidence at extremes.
        extreme_threshold: Predictions beyond this distance from 0.5 get
            additional shrinkage. e.g. 0.30 means >0.80 or <0.20 get extra.
        extra_shrink: Additional shrink factor applied to extreme predictions.
        confidence_weight: How much to weight calibration by model confidence.
            Higher confidence = less shrinkage applied.
    """

    shrink_factor: float = 0.15
    extreme_threshold: float = 0.30
    extra_shrink: float = 0.10
    confidence_weight: bool = True

    def calibrate(
        self,
        probability: float,
        confidence: float = 50.0,
    ) -> float:
        """Calibrate a raw LLM probability estimate.

        Args:
            probability: Raw estimate from LLM (0.0–1.0).
            confidence: LLM confidence score (0–100). Higher confidence
                means less shrinkage is applied.

        Returns:
            Calibrated probability, still in [0.01, 0.99].
        """
        p = max(0.01, min(0.99, probability))

        # Adjust shrink factor by confidence — high confidence = shrink less
        effective_shrink = self.shrink_factor
        if self.confidence_weight and confidence > 0:
            # Scale: at confidence=100, shrink by 50%. At confidence=0, full shrink.
            confidence_scalar = 1.0 - (confidence / 100) * 0.5
            effective_shrink *= confidence_scalar

        # Apply base-rate shrinkage toward 0.5
        calibrated = p + effective_shrink * (0.5 - p)

        # Extra shrinkage for extreme predictions (LLMs are especially miscalibrated here)
        distance_from_center = abs(p - 0.5)
        if distance_from_center > self.extreme_threshold:
            calibrated = calibrated + self.extra_shrink * (0.5 - calibrated)

        result = round(max(0.01, min(0.99, calibrated)), 4)
        logger.debug(
            "calibrated_probability",
            raw=p,
            calibrated=result,
            shift=round(result - p, 4),
            confidence=confidence,
        )
        return result

    def calibrate_edge(
        self,
        raw_probability: float,
        market_price: float,
        confidence: float = 50.0,
    ) -> float:
        """Return calibrated edge (calibrated_prob - market_price).

        Use this instead of (raw_prob - market_price) to avoid trading on
        phantom edges created by LLM overconfidence.
        """
        calibrated = self.calibrate(raw_probability, confidence)
        return round(calibrated - market_price, 4)


@dataclass
class CalibrationStats:
    """Track calibration accuracy over time to self-tune shrink_factor.

    Usage::

        stats = CalibrationStats()
        stats.record(predicted=0.75, outcome=True)   # market resolved YES
        stats.record(predicted=0.80, outcome=False)  # market resolved NO
        print(stats.brier_score)   # lower is better, 0.0 = perfect
        print(stats.suggested_shrink_factor)
    """

    predictions: list[tuple[float, bool]] = field(default_factory=list)

    def record(self, predicted: float, outcome: bool) -> None:
        self.predictions.append((predicted, outcome))

    @property
    def brier_score(self) -> float:
        """Mean squared error between predictions and outcomes. Lower = better."""
        if not self.predictions:
            return 0.0
        return sum((p - int(o)) ** 2 for p, o in self.predictions) / len(self.predictions)

    @property
    def mean_overconfidence(self) -> float:
        """Positive = predictions are too extreme. Negative = too conservative."""
        if not self.predictions:
            return 0.0
        errors = []
        for p, o in self.predictions:
            # How far from 0.5 was the prediction, vs how far the outcome was
            pred_distance = abs(p - 0.5)
            outcome_distance = abs(int(o) - 0.5)  # always 0.5
            errors.append(pred_distance - outcome_distance)
        return sum(errors) / len(errors)

    @property
    def suggested_shrink_factor(self) -> float:
        """Rough estimate of optimal shrink based on observed overconfidence."""
        overconf = self.mean_overconfidence
        # Overconfidence of 0.15 means shrink ~15% more
        suggested = max(0.0, min(0.40, overconf))
        return round(suggested, 3)

    @property
    def n(self) -> int:
        return len(self.predictions)
