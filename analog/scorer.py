"""Score strategy performance across an analog set.

Given a set of analog matches and forward return data, evaluates how each
strategy in the library would have performed during those analog periods.
The output is a ranked recommendation of which strategy to run now,
with what risk bucket.

This is the meta-controller's decision engine — the piece that connects
"what does the market look like?" to "what should we do?"

Usage::

    scorer = AnalogScorer()
    scorer.register_strategy("funding_arb", funding_pnl_fn)
    scorer.register_strategy("trend_follow", trend_pnl_fn)

    matches = finder.query(current_fingerprint)
    scores = scorer.score(matches, forward_returns)
    # scores[0].strategy_name, scores[0].win_rate, scores[0].risk_bucket
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import structlog

from analog.finder import AnalogMatch

logger = structlog.get_logger()


@dataclass
class StrategyScore:
    """Performance of a strategy across the analog set."""

    strategy_name: str
    win_rate: float  # fraction of analogs where strategy was profitable
    mean_return: float  # weighted mean return across analogs
    worst_return: float  # worst single-analog return
    consistency: float  # 0-1, how stable across analogs (1 = always same sign)
    n_analogs: int  # how many analogs had data for this strategy
    risk_bucket: str  # recommended: "0x", "0.25x", "0.5x", "1x"
    confidence: float  # 0-1 meta-confidence in this recommendation

    @property
    def summary(self) -> dict:
        return {
            "strategy": self.strategy_name,
            "win_rate": f"{self.win_rate:.1%}",
            "mean_return": f"{self.mean_return:+.4f}",
            "worst": f"{self.worst_return:+.4f}",
            "consistency": f"{self.consistency:.2f}",
            "risk": self.risk_bucket,
            "confidence": f"{self.confidence:.2f}",
            "n": self.n_analogs,
        }


# A strategy evaluator receives: (analog_timestamp, forward_hours)
# and returns the P&L that strategy would have produced, or None if
# it wouldn't have traded.
StrategyEvaluator = Callable[[float, float], float | None]


class AnalogScorer:
    """Score strategies against analog sets to produce recommendations.

    Args:
        forward_hours: How far forward to evaluate strategy performance
            after each analog timestamp (default 4h).
        min_analogs: Minimum analogs with strategy data to produce a score.
        risk_thresholds: Win rate thresholds for risk bucket assignment.
    """

    def __init__(
        self,
        forward_hours: float = 4.0,
        min_analogs: int = 5,
        risk_thresholds: dict[str, float] | None = None,
    ):
        self.forward_hours = forward_hours
        self.min_analogs = min_analogs
        self.risk_thresholds = risk_thresholds or {
            "1x": 0.70,    # 70%+ win rate across analogs → full size
            "0.5x": 0.60,  # 60-70% → reduced
            "0.25x": 0.50, # 50-60% → minimal
            "0x": 0.0,     # below 50% → sit out
        }
        self._strategies: dict[str, StrategyEvaluator] = {}

    def register_strategy(self, name: str, evaluator: StrategyEvaluator) -> None:
        """Register a strategy evaluator function.

        The evaluator takes (analog_timestamp, forward_hours) and returns
        the P&L that strategy would have produced in that window, or None
        if the strategy wouldn't have traded.
        """
        self._strategies[name] = evaluator

    def score(
        self,
        matches: list[AnalogMatch],
        forward_hours: float | None = None,
    ) -> list[StrategyScore]:
        """Score all registered strategies against the analog set.

        Returns a list of StrategyScore sorted by confidence (best first).
        """
        fwd = forward_hours or self.forward_hours
        results: list[StrategyScore] = []

        for name, evaluator in self._strategies.items():
            returns: list[tuple[float, float]] = []  # (weight, pnl)

            for match in matches:
                try:
                    pnl = evaluator(match.fingerprint.timestamp, fwd)
                except Exception as e:
                    logger.warning("strategy_eval_error", strategy=name, error=str(e))
                    continue

                if pnl is not None:
                    returns.append((match.weight, pnl))

            if len(returns) < self.min_analogs:
                results.append(
                    StrategyScore(
                        strategy_name=name,
                        win_rate=0.0,
                        mean_return=0.0,
                        worst_return=0.0,
                        consistency=0.0,
                        n_analogs=len(returns),
                        risk_bucket="0x",
                        confidence=0.0,
                    )
                )
                continue

            total_weight = sum(w for w, _ in returns)
            wins = sum(w for w, pnl in returns if pnl > 0)
            win_rate = wins / total_weight if total_weight > 0 else 0.0

            weighted_mean = sum(w * pnl for w, pnl in returns) / total_weight
            worst = min(pnl for _, pnl in returns)

            # Consistency: what fraction of return signs agree with the mean?
            if weighted_mean != 0:
                same_sign = sum(w for w, pnl in returns if (pnl > 0) == (weighted_mean > 0))
                consistency = same_sign / total_weight
            else:
                consistency = 0.0

            # Risk bucket assignment
            risk_bucket = "0x"
            for bucket, threshold in sorted(self.risk_thresholds.items(), key=lambda x: x[1], reverse=True):
                if win_rate >= threshold:
                    risk_bucket = bucket
                    break

            # Confidence: combination of win rate, consistency, and sample size
            sample_factor = min(len(returns) / 15, 1.0)  # 15+ analogs → full confidence
            confidence = (win_rate * 0.4 + consistency * 0.4 + sample_factor * 0.2)

            results.append(
                StrategyScore(
                    strategy_name=name,
                    win_rate=round(win_rate, 4),
                    mean_return=round(weighted_mean, 6),
                    worst_return=round(worst, 6),
                    consistency=round(consistency, 4),
                    n_analogs=len(returns),
                    risk_bucket=risk_bucket,
                    confidence=round(confidence, 4),
                )
            )

        results.sort(key=lambda s: s.confidence, reverse=True)
        return results

    def recommend(
        self,
        matches: list[AnalogMatch],
        forward_hours: float | None = None,
    ) -> StrategyScore | None:
        """Return the single best strategy recommendation, or None if
        nothing scores above 0x.
        """
        scores = self.score(matches, forward_hours)
        for s in scores:
            if s.risk_bucket != "0x":
                return s
        return None

    @property
    def strategy_names(self) -> list[str]:
        return list(self._strategies.keys())
