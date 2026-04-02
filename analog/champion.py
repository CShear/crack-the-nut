"""Champion/challenger evaluation system.

Compares different configurations (analog parameters, scoring thresholds,
strategy evaluators) using walk-forward evaluation. Only replaces the
current champion when a challenger beats it on risk-adjusted metrics
across multiple criteria.

The replacement bar is intentionally high: a challenger must be better on
risk-adjusted returns, drawdown profile, and consistency. This prevents
churn from noise.

Usage::

    arena = Arena()
    arena.add_challenger("current", config_current)
    arena.add_challenger("wider_analogs", config_wider)
    arena.add_challenger("tighter_risk", config_tighter)

    results = arena.evaluate(fingerprints, evaluators)
    print(results.ranking)
    print(results.recommendation)
"""

from __future__ import annotations

from dataclasses import dataclass, field

import structlog

from analog.evaluators import StrategyEvaluator
from analog.fingerprint import Fingerprint
from analog.walkforward import WalkForward, WalkForwardResults

logger = structlog.get_logger()


@dataclass
class ChallengerConfig:
    """Configuration for one challenger in the arena.

    Each field corresponds to a tunable parameter in the analog pipeline.
    """

    name: str
    k: int = 20
    recency_halflife_days: float = 180.0
    forward_hours: float = 4.0
    step_bars: int = 6
    min_history: int = 500
    min_analog_confidence: float = 0.25
    min_strategy_confidence: float = 0.40

    def summary(self) -> dict:
        return {
            "name": self.name,
            "k": self.k,
            "recency_halflife": self.recency_halflife_days,
            "forward_hours": self.forward_hours,
            "step_bars": self.step_bars,
        }


@dataclass
class ChallengerResult:
    """Walk-forward results for one challenger."""

    config: ChallengerConfig
    results: WalkForwardResults

    @property
    def name(self) -> str:
        return self.config.name

    @property
    def score(self) -> float:
        """Composite score for ranking. Higher is better.

        Weights:
        - Win rate: 30% (must be profitable)
        - Profit factor: 25% (quality of wins vs losses)
        - Trade frequency: 15% (must actually trade, not always sit out)
        - Max drawdown penalty: 20% (smaller drawdown is better)
        - Sit-out accuracy: 10% (correct when standing aside)
        """
        wr = self.results.win_rate or 0.0
        pf = min(self.results.profit_factor or 0.0, 5.0)  # cap at 5x
        trade_pct = self.results.n_traded / max(self.results.n_steps, 1)
        dd = self.results.max_drawdown
        sit_acc = self.results.sit_out_accuracy or 0.5

        # Normalize to 0-1 ranges
        wr_score = wr  # already 0-1
        pf_score = min(pf / 3.0, 1.0)  # 3x profit factor → max score
        trade_score = min(trade_pct / 0.3, 1.0)  # 30%+ activity → max score
        dd_score = max(0.0, 1.0 - dd * 20)  # 5% drawdown → 0 score
        sit_score = sit_acc  # already 0-1

        return (
            wr_score * 0.30
            + pf_score * 0.25
            + trade_score * 0.15
            + dd_score * 0.20
            + sit_score * 0.10
        )


@dataclass
class ArenaResults:
    """Results from the champion/challenger evaluation."""

    challengers: list[ChallengerResult] = field(default_factory=list)

    @property
    def ranking(self) -> list[ChallengerResult]:
        """Challengers ranked by composite score (best first)."""
        return sorted(self.challengers, key=lambda c: c.score, reverse=True)

    @property
    def champion(self) -> ChallengerResult | None:
        """The top-ranked challenger."""
        ranked = self.ranking
        return ranked[0] if ranked else None

    @property
    def recommendation(self) -> str:
        """Human-readable recommendation."""
        ranked = self.ranking
        if not ranked:
            return "No challengers evaluated."

        best = ranked[0]
        if len(ranked) == 1:
            return f"Only one config tested: '{best.name}' (score={best.score:.3f})"

        second = ranked[1]
        margin = best.score - second.score

        if margin < 0.02:
            return (
                f"Too close to call: '{best.name}' ({best.score:.3f}) vs "
                f"'{second.name}' ({second.score:.3f}). "
                f"Keep current champion — margin ({margin:.3f}) below replacement threshold."
            )

        return (
            f"Replace with '{best.name}' (score={best.score:.3f}, "
            f"margin={margin:.3f} over '{second.name}')"
        )

    def beats_champion(
        self,
        challenger_name: str,
        champion_name: str,
        min_margin: float = 0.02,
    ) -> bool:
        """Does the named challenger beat the champion by enough margin?

        The bar is intentionally high. A marginal improvement could be noise.
        """
        challenger = next((c for c in self.challengers if c.name == challenger_name), None)
        champion = next((c for c in self.challengers if c.name == champion_name), None)

        if challenger is None or champion is None:
            return False

        margin = challenger.score - champion.score
        if margin < min_margin:
            return False

        # Additional checks: challenger must not be worse on key metrics
        c_wr = challenger.results.win_rate or 0
        ch_wr = champion.results.win_rate or 0
        if c_wr < ch_wr - 0.05:  # can't drop win rate by >5%
            return False

        c_dd = challenger.results.max_drawdown
        ch_dd = champion.results.max_drawdown
        if c_dd > ch_dd * 1.5:  # can't increase drawdown by >50%
            return False

        return True


class Arena:
    """Run walk-forward evaluations across multiple configurations.

    Args:
        verbose: Print progress during evaluation.
    """

    def __init__(self, verbose: bool = True):
        self.verbose = verbose
        self._configs: list[ChallengerConfig] = []

    def add_challenger(self, config: ChallengerConfig) -> None:
        """Add a challenger configuration to evaluate."""
        self._configs.append(config)

    def evaluate(
        self,
        fingerprints: list[Fingerprint],
        evaluators: dict[str, StrategyEvaluator],
    ) -> ArenaResults:
        """Run walk-forward evaluation for all challengers.

        Each challenger gets the same data and evaluators, differing only
        in analog/scoring parameters.
        """
        results = ArenaResults()

        for i, config in enumerate(self._configs):
            if self.verbose:
                print(f"\n{'─' * 40}")
                print(f"  Challenger {i + 1}/{len(self._configs)}: '{config.name}'")
                print(f"  k={config.k}, halflife={config.recency_halflife_days}d, "
                      f"fwd={config.forward_hours}h, step={config.step_bars}")
                print(f"{'─' * 40}")

            wf = WalkForward(
                fingerprints=fingerprints,
                evaluators=evaluators,
                step_bars=config.step_bars,
                min_history=config.min_history,
                k=config.k,
                forward_hours=config.forward_hours,
                recency_halflife_days=config.recency_halflife_days,
            )

            wf_results = wf.run(verbose=self.verbose)

            results.challengers.append(
                ChallengerResult(config=config, results=wf_results)
            )

            if self.verbose:
                s = wf_results.summary
                print(f"\n  Result: WR={s['win_rate']} PnL={s['total_pnl']} "
                      f"PF={s['profit_factor']} DD={s['max_drawdown']}")

        if self.verbose:
            print(f"\n{'=' * 60}")
            print("  ARENA RESULTS")
            print(f"{'=' * 60}")
            print_arena_results(results)

        logger.info(
            "arena_complete",
            n_challengers=len(results.challengers),
            champion=results.champion.name if results.champion else None,
            recommendation=results.recommendation,
        )

        return results


def print_arena_results(results: ArenaResults) -> None:
    """Print formatted arena comparison."""
    ranked = results.ranking

    print(f"\n  {'Rank':>4}  {'Name':>20}  {'Score':>6}  {'WR':>6}  {'PnL':>9}  "
          f"{'PF':>5}  {'DD':>7}  {'Traded':>7}  {'SitAcc':>7}")
    print(f"  {'─' * 4}  {'─' * 20}  {'─' * 6}  {'─' * 6}  {'─' * 9}  "
          f"{'─' * 5}  {'─' * 7}  {'─' * 7}  {'─' * 7}")

    for rank, cr in enumerate(ranked, 1):
        r = cr.results
        wr = f"{r.win_rate:.1%}" if r.win_rate is not None else "n/a"
        pf = f"{r.profit_factor:.2f}" if r.profit_factor is not None else "n/a"
        sit = f"{r.sit_out_accuracy:.1%}" if r.sit_out_accuracy is not None else "n/a"
        trade_pct = f"{r.n_traded / max(r.n_steps, 1):.1%}"

        print(
            f"  {rank:>4}  {cr.name:>20}  {cr.score:>5.3f}  {wr:>6}  "
            f"{r.total_pnl:>+8.4f}  {pf:>5}  {r.max_drawdown:>6.4f}  "
            f"{trade_pct:>7}  {sit:>7}"
        )

    print(f"\n  Recommendation: {results.recommendation}")
