"""Meta-controller — the decision layer between analogs and execution.

Given the current market fingerprint and analog matches, the meta-controller:
1. Queries the analog finder for similar historical periods
2. Scores strategies across those analogs
3. Applies confidence gates and risk rules
4. Outputs a structured decision (strategy + params + risk bucket)

The meta-controller does NOT output buy/sell. It outputs which strategy
to run, with what parameter profile, at what risk level. Execution stays
deterministic and bounded.

Usage::

    controller = MetaController(
        finder=finder,
        scorer=scorer,
        profiles=STRATEGY_PROFILES,
    )

    decision = controller.decide(current_fingerprint)
    # decision.strategy = "funding_arb"
    # decision.profile = "conservative"
    # decision.risk_bucket = "0.5x"
    # decision.confidence = 0.72
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

import structlog

from analog.finder import AnalogFinder
from analog.fingerprint import Fingerprint
from analog.scorer import AnalogScorer, StrategyScore

logger = structlog.get_logger()


# --- Strategy Parameter Profiles ---

@dataclass(frozen=True)
class StrategyProfile:
    """A named parameter set for a strategy.

    Strategies don't accept arbitrary parameters from AI. They accept
    one of a small set of predefined profiles. This keeps AI inside
    a bounded control box.
    """

    name: str  # e.g., "conservative", "moderate", "aggressive"
    params: dict  # strategy-specific parameter overrides


# Default profiles per strategy
DEFAULT_PROFILES: dict[str, list[StrategyProfile]] = {
    "funding_arb": [
        StrategyProfile("conservative", {"threshold": 0.0005, "size_pct": 0.02}),
        StrategyProfile("moderate", {"threshold": 0.0003, "size_pct": 0.03}),
        StrategyProfile("aggressive", {"threshold": 0.0002, "size_pct": 0.05}),
    ],
    "multi_asset_funding": [
        StrategyProfile("conservative", {"threshold": 0.0008, "size_pct": 0.02, "max_assets": 1}),
        StrategyProfile("moderate", {"threshold": 0.0005, "size_pct": 0.03, "max_assets": 2}),
        StrategyProfile("aggressive", {"threshold": 0.0003, "size_pct": 0.04, "max_assets": 3}),
    ],
    "trend_follow": [
        StrategyProfile("conservative", {"min_momentum_1d": 0.01, "min_momentum_7d": 0.03, "size_pct": 0.02}),
        StrategyProfile("moderate", {"min_momentum_1d": 0.005, "min_momentum_7d": 0.02, "size_pct": 0.03}),
        StrategyProfile("aggressive", {"min_momentum_1d": 0.003, "min_momentum_7d": 0.015, "size_pct": 0.05}),
    ],
    "mean_reversion": [
        StrategyProfile("conservative", {"move_threshold": 0.04, "vol_ratio_min": 1.3, "size_pct": 0.02}),
        StrategyProfile("moderate", {"move_threshold": 0.03, "vol_ratio_min": 1.2, "size_pct": 0.03}),
        StrategyProfile("aggressive", {"move_threshold": 0.02, "vol_ratio_min": 1.1, "size_pct": 0.04}),
    ],
    "breakout": [
        StrategyProfile("conservative", {"compression": 0.6, "breakout_min": 0.015, "size_pct": 0.02}),
        StrategyProfile("moderate", {"compression": 0.7, "breakout_min": 0.01, "size_pct": 0.03}),
        StrategyProfile("aggressive", {"compression": 0.8, "breakout_min": 0.008, "size_pct": 0.04}),
    ],
}


# --- Decision Output ---

@dataclass
class Decision:
    """Structured output from the meta-controller.

    This is what gets passed to the execution layer. It never contains
    a raw buy/sell signal — only which strategy to run, how, and at
    what risk level.
    """

    strategy: str  # strategy name from the library
    profile: str  # parameter profile name
    risk_bucket: str  # "0x", "0.25x", "0.5x", "1x"
    confidence: float  # 0-1 meta-confidence
    analog_confidence: float  # quality of the analog set
    score: StrategyScore | None  # underlying strategy score
    timestamp: float
    reason: str  # human-readable explanation

    @property
    def should_trade(self) -> bool:
        return self.risk_bucket != "0x"

    @property
    def params(self) -> dict:
        """Get the actual parameter dict for this profile."""
        profiles = DEFAULT_PROFILES.get(self.strategy, [])
        for p in profiles:
            if p.name == self.profile:
                return p.params
        return {}

    @property
    def summary(self) -> dict:
        return {
            "strategy": self.strategy,
            "profile": self.profile,
            "risk": self.risk_bucket,
            "confidence": f"{self.confidence:.2f}",
            "analog_confidence": f"{self.analog_confidence:.2f}",
            "reason": self.reason,
            "params": self.params,
            "timestamp": datetime.fromtimestamp(
                self.timestamp, tz=timezone.utc
            ).isoformat(),
        }


# --- Sit-Out Decision ---

def _sit_out(timestamp: float, analog_confidence: float, reason: str) -> Decision:
    return Decision(
        strategy="sit_out",
        profile="none",
        risk_bucket="0x",
        confidence=0.0,
        analog_confidence=analog_confidence,
        score=None,
        timestamp=timestamp,
        reason=reason,
    )


# --- Meta-Controller ---

class MetaController:
    """Strategy selection and risk allocation controller.

    The meta-controller sits between the analog/scoring system and execution.
    It applies additional confidence gates and selects parameter profiles
    based on the strength of the analog signal.

    Args:
        finder: Fitted AnalogFinder instance.
        scorer: AnalogScorer with registered strategy evaluators.
        profiles: Strategy parameter profiles. Defaults to DEFAULT_PROFILES.
        min_analog_confidence: Below this, always sit out.
        min_strategy_confidence: Below this, downgrade risk or sit out.
        k: Number of analogs to query.
    """

    def __init__(
        self,
        finder: AnalogFinder,
        scorer: AnalogScorer,
        profiles: dict[str, list[StrategyProfile]] | None = None,
        min_analog_confidence: float = 0.25,
        min_strategy_confidence: float = 0.40,
        k: int = 20,
    ):
        self.finder = finder
        self.scorer = scorer
        self.profiles = profiles or DEFAULT_PROFILES
        self.min_analog_confidence = min_analog_confidence
        self.min_strategy_confidence = min_strategy_confidence
        self.k = k

    def decide(self, fingerprint: Fingerprint) -> Decision:
        """Make a strategy selection decision for the current market state.

        Returns a Decision with strategy, profile, risk bucket, and confidence.
        """
        ts = fingerprint.timestamp

        # Step 1: Find analogs
        matches = self.finder.query(fingerprint, k=self.k)
        if not matches:
            return _sit_out(ts, 0.0, "No analogs found")

        quality = self.finder.analog_quality(matches)
        analog_conf = quality["confidence"]

        # Gate 1: Analog quality
        if analog_conf < self.min_analog_confidence:
            return _sit_out(
                ts, analog_conf,
                f"Low analog confidence ({analog_conf:.2f} < {self.min_analog_confidence})"
            )

        # Step 2: Score strategies
        recommendation = self.scorer.recommend(matches)
        if recommendation is None:
            return _sit_out(ts, analog_conf, "No strategy above threshold")

        # Gate 2: Strategy confidence
        if recommendation.confidence < self.min_strategy_confidence:
            return _sit_out(
                ts, analog_conf,
                f"Low strategy confidence ({recommendation.confidence:.2f} < {self.min_strategy_confidence})"
            )

        # Step 3: Select parameter profile based on confidence + risk
        profile = self._select_profile(recommendation)

        # Step 4: Apply risk adjustment
        risk_bucket = self._adjust_risk(recommendation, analog_conf)

        # Composite confidence
        confidence = (recommendation.confidence * 0.6 + analog_conf * 0.4)

        reason = (
            f"{recommendation.strategy_name}: "
            f"WR={recommendation.win_rate:.1%} across {recommendation.n_analogs} analogs, "
            f"analog_conf={analog_conf:.2f}"
        )

        return Decision(
            strategy=recommendation.strategy_name,
            profile=profile.name,
            risk_bucket=risk_bucket,
            confidence=round(confidence, 4),
            analog_confidence=round(analog_conf, 4),
            score=recommendation,
            timestamp=ts,
            reason=reason,
        )

    def _select_profile(self, score: StrategyScore) -> StrategyProfile:
        """Select parameter profile based on strategy confidence.

        High confidence → aggressive, low → conservative.
        """
        profiles = self.profiles.get(score.strategy_name, [])
        if not profiles:
            return StrategyProfile("default", {})

        # Map confidence to profile index
        # conservative: conf < 0.5, moderate: 0.5-0.7, aggressive: > 0.7
        if score.confidence >= 0.7 and score.win_rate >= 0.65:
            target = "aggressive"
        elif score.confidence >= 0.5:
            target = "moderate"
        else:
            target = "conservative"

        for p in profiles:
            if p.name == target:
                return p
        return profiles[0]  # fallback to first (conservative)

    def _adjust_risk(self, score: StrategyScore, analog_confidence: float) -> str:
        """Adjust risk bucket based on both strategy and analog confidence.

        The scorer already assigns a risk bucket based on win rate.
        We further constrain it if analog confidence is weak.
        """
        base_risk = score.risk_bucket

        # Risk ordering for comparison
        risk_levels = ["0x", "0.25x", "0.5x", "1x"]
        base_idx = risk_levels.index(base_risk) if base_risk in risk_levels else 0

        # Downgrade risk if analog confidence is mediocre
        if analog_confidence < 0.4:
            max_idx = 1  # cap at 0.25x
        elif analog_confidence < 0.6:
            max_idx = 2  # cap at 0.5x
        else:
            max_idx = 3  # allow 1x

        final_idx = min(base_idx, max_idx)
        return risk_levels[final_idx]

    def decide_batch(self, fingerprints: list[Fingerprint]) -> list[Decision]:
        """Make decisions for multiple fingerprints (e.g., walk-forward)."""
        return [self.decide(fp) for fp in fingerprints]
