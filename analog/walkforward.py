"""Walk-forward evaluation for the analog memory system.

Simulates what the system would have recommended at each historical bar,
then measures whether those recommendations were correct. This is the
primary validation tool — no component earns trust without passing
walk-forward testing.

The evaluator steps through fingerprints chronologically, at each step:
1. Fits the analog finder on all data *before* the current bar (no lookahead)
2. Queries for analogs
3. Scores strategies and picks a recommendation
4. Records what actually happened in the forward window
5. Scores the recommendation against reality

Usage::

    wf = WalkForward(
        fingerprints=fingerprints,
        evaluators=evaluators,
        step_bars=6,  # evaluate every 6 bars (1 day at 4h)
    )
    results = wf.run()
    print(results.summary)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

import structlog

from analog.evaluators import StrategyEvaluator
from analog.finder import AnalogFinder
from analog.fingerprint import Fingerprint
from analog.scorer import AnalogScorer, StrategyScore

logger = structlog.get_logger()


@dataclass
class WalkForwardStep:
    """One step of walk-forward evaluation."""

    timestamp: float
    recommendation: StrategyScore | None  # what the system recommended
    actual_returns: dict[str, float | None]  # strategy_name → actual forward return
    analog_confidence: float  # analog set quality
    n_history: int  # how many fingerprints were available

    @property
    def recommended_strategy(self) -> str:
        return self.recommendation.strategy_name if self.recommendation else "sit_out"

    @property
    def recommended_risk(self) -> str:
        return self.recommendation.risk_bucket if self.recommendation else "0x"

    @property
    def was_correct(self) -> bool | None:
        """Was the recommendation profitable? None if sat out."""
        if self.recommendation is None or self.recommendation.risk_bucket == "0x":
            return None
        actual = self.actual_returns.get(self.recommendation.strategy_name)
        if actual is None:
            return None
        return actual > 0

    @property
    def actual_pnl(self) -> float | None:
        """Actual P&L of the recommended strategy. None if sat out."""
        if self.recommendation is None or self.recommendation.risk_bucket == "0x":
            return None
        return self.actual_returns.get(self.recommendation.strategy_name)


@dataclass
class WalkForwardResults:
    """Aggregated walk-forward evaluation results."""

    steps: list[WalkForwardStep] = field(default_factory=list)

    @property
    def n_steps(self) -> int:
        return len(self.steps)

    @property
    def n_traded(self) -> int:
        """Steps where the system recommended a trade (non-0x)."""
        return sum(1 for s in self.steps if s.was_correct is not None)

    @property
    def n_sat_out(self) -> int:
        return self.n_steps - self.n_traded

    @property
    def win_rate(self) -> float | None:
        """Win rate on traded steps."""
        traded = [s for s in self.steps if s.was_correct is not None]
        if not traded:
            return None
        return sum(1 for s in traded if s.was_correct) / len(traded)

    @property
    def total_pnl(self) -> float:
        """Sum of actual P&L on traded steps."""
        return sum(s.actual_pnl for s in self.steps if s.actual_pnl is not None)

    @property
    def mean_pnl(self) -> float | None:
        """Mean P&L per traded step."""
        traded = [s.actual_pnl for s in self.steps if s.actual_pnl is not None]
        if not traded:
            return None
        return sum(traded) / len(traded)

    @property
    def max_drawdown(self) -> float:
        """Maximum drawdown from peak cumulative P&L."""
        peak = 0.0
        cum = 0.0
        max_dd = 0.0
        for s in self.steps:
            if s.actual_pnl is not None:
                cum += s.actual_pnl
                peak = max(peak, cum)
                dd = peak - cum
                max_dd = max(max_dd, dd)
        return max_dd

    @property
    def profit_factor(self) -> float | None:
        """Gross profits / gross losses."""
        gains = sum(s.actual_pnl for s in self.steps if s.actual_pnl is not None and s.actual_pnl > 0)
        losses = abs(sum(s.actual_pnl for s in self.steps if s.actual_pnl is not None and s.actual_pnl < 0))
        if losses == 0:
            return float("inf") if gains > 0 else None
        return gains / losses

    @property
    def sit_out_accuracy(self) -> float | None:
        """When the system sat out, what fraction of the time was sitting
        out the correct call? (i.e., no strategy was profitable.)

        This measures whether the system correctly identifies bad conditions.
        """
        sat_out_steps = [s for s in self.steps if s.recommended_risk == "0x"]
        if not sat_out_steps:
            return None
        correct = 0
        for s in sat_out_steps:
            # "Correct" sit-out: no strategy had positive returns
            best_actual = max(
                (v for v in s.actual_returns.values() if v is not None),
                default=None,
            )
            if best_actual is None or best_actual <= 0:
                correct += 1
        return correct / len(sat_out_steps)

    @property
    def strategy_distribution(self) -> dict[str, int]:
        """How often each strategy was recommended."""
        dist: dict[str, int] = {}
        for s in self.steps:
            name = s.recommended_strategy
            dist[name] = dist.get(name, 0) + 1
        return dist

    @property
    def risk_distribution(self) -> dict[str, int]:
        """How often each risk bucket was assigned."""
        dist: dict[str, int] = {}
        for s in self.steps:
            risk = s.recommended_risk
            dist[risk] = dist.get(risk, 0) + 1
        return dist

    @property
    def summary(self) -> dict:
        return {
            "n_steps": self.n_steps,
            "n_traded": self.n_traded,
            "n_sat_out": self.n_sat_out,
            "trade_pct": f"{self.n_traded / self.n_steps:.1%}" if self.n_steps else "0%",
            "win_rate": f"{self.win_rate:.1%}" if self.win_rate is not None else "n/a",
            "total_pnl": f"{self.total_pnl:+.4f}",
            "mean_pnl": f"{self.mean_pnl:+.4f}" if self.mean_pnl is not None else "n/a",
            "max_drawdown": f"{self.max_drawdown:.4f}",
            "profit_factor": f"{self.profit_factor:.2f}" if self.profit_factor is not None else "n/a",
            "sit_out_accuracy": f"{self.sit_out_accuracy:.1%}" if self.sit_out_accuracy is not None else "n/a",
            "strategy_dist": self.strategy_distribution,
            "risk_dist": self.risk_distribution,
        }


class WalkForward:
    """Walk-forward evaluator for the analog memory system.

    Args:
        fingerprints: Full list of historical fingerprints (chronological).
        evaluators: Strategy evaluators (from build_evaluators).
        step_bars: Evaluate every N bars (default 6 = once per day at 4h bars).
        min_history: Minimum fingerprints required before starting evaluation.
        k: Number of analogs to find per query.
        forward_hours: Forward window for strategy evaluation.
        recency_halflife_days: Analog recency weighting half-life.
    """

    def __init__(
        self,
        fingerprints: list[Fingerprint],
        evaluators: dict[str, StrategyEvaluator],
        step_bars: int = 6,
        min_history: int = 500,
        k: int = 20,
        forward_hours: float = 4.0,
        recency_halflife_days: float = 180.0,
    ):
        self.fingerprints = sorted(fingerprints, key=lambda fp: fp.timestamp)
        self.evaluators = evaluators
        self.step_bars = step_bars
        self.min_history = min_history
        self.k = k
        self.forward_hours = forward_hours
        self.recency_halflife_days = recency_halflife_days

    def run(self, verbose: bool = True) -> WalkForwardResults:
        """Run the full walk-forward evaluation.

        Steps through fingerprints chronologically, fitting the analog finder
        on past data only, querying, scoring, and recording results.
        """
        results = WalkForwardResults()
        n = len(self.fingerprints)

        if n < self.min_history + 10:
            logger.warning(
                "walkforward_insufficient_data",
                n_fingerprints=n,
                min_history=self.min_history,
            )
            return results

        # Build scorer once (evaluators are stateless closures over historical data)
        scorer = AnalogScorer(forward_hours=self.forward_hours)
        for name, evaluator in self.evaluators.items():
            scorer.register_strategy(name, evaluator)

        eval_indices = range(self.min_history, n, self.step_bars)
        total_evals = len(list(eval_indices))

        if verbose:
            print(f"\nWalk-forward: {total_evals} evaluation points "
                  f"({self.min_history} warmup, step={self.step_bars} bars)")

        for step_num, i in enumerate(range(self.min_history, n, self.step_bars)):
            query = self.fingerprints[i]
            history = self.fingerprints[:i]

            # Fit finder on history only (no lookahead)
            finder = AnalogFinder(
                k=self.k,
                recency_halflife_days=self.recency_halflife_days,
            )
            finder.fit(history)

            # Query for analogs
            matches = finder.query(query)
            quality = finder.analog_quality(matches)

            # Get recommendation
            recommendation = scorer.recommend(matches)

            # Record actual forward returns for ALL strategies
            actual_returns: dict[str, float | None] = {}
            for name, evaluator in self.evaluators.items():
                try:
                    actual_returns[name] = evaluator(query.timestamp, self.forward_hours)
                except Exception:
                    actual_returns[name] = None

            step = WalkForwardStep(
                timestamp=query.timestamp,
                recommendation=recommendation,
                actual_returns=actual_returns,
                analog_confidence=quality["confidence"],
                n_history=len(history),
            )
            results.steps.append(step)

            if verbose and (step_num + 1) % 50 == 0:
                traded = results.n_traded
                wr = results.win_rate
                wr_str = f"{wr:.1%}" if wr is not None else "n/a"
                print(f"  [{step_num + 1}/{total_evals}] "
                      f"traded={traded} WR={wr_str} "
                      f"PnL={results.total_pnl:+.4f}")

        logger.info(
            "walkforward_complete",
            n_steps=results.n_steps,
            n_traded=results.n_traded,
            win_rate=results.win_rate,
            total_pnl=round(results.total_pnl, 6),
        )

        return results


def print_walkforward_report(results: WalkForwardResults) -> None:
    """Print a detailed walk-forward report."""
    s = results.summary

    print("\n" + "=" * 60)
    print("  WALK-FORWARD EVALUATION REPORT")
    print("=" * 60)

    print(f"\n  Evaluation Points:    {s['n_steps']}")
    print(f"  Traded:               {s['n_traded']} ({s['trade_pct']})")
    print(f"  Sat Out:              {s['n_sat_out']}")

    print("\n  --- Trading Performance ---")
    print(f"  Win Rate:             {s['win_rate']}")
    print(f"  Total PnL:            {s['total_pnl']}")
    print(f"  Mean PnL/Trade:       {s['mean_pnl']}")
    print(f"  Max Drawdown:         {s['max_drawdown']}")
    print(f"  Profit Factor:        {s['profit_factor']}")

    print("\n  --- Sit-Out Accuracy ---")
    print(f"  Correct Sit-Outs:     {s['sit_out_accuracy']}")

    print("\n  --- Strategy Distribution ---")
    for name, count in sorted(s['strategy_dist'].items(), key=lambda x: x[1], reverse=True):
        pct = count / results.n_steps
        print(f"  {name:>22}: {count:>4} ({pct:.1%})")

    print("\n  --- Risk Distribution ---")
    for risk, count in sorted(s['risk_dist'].items()):
        pct = count / results.n_steps
        print(f"  {risk:>22}: {count:>4} ({pct:.1%})")

    # Per-strategy actual performance
    print("\n  --- Per-Strategy Actual Returns ---")
    strategy_returns: dict[str, list[float]] = {}
    for step in results.steps:
        for name, ret in step.actual_returns.items():
            if ret is not None:
                strategy_returns.setdefault(name, []).append(ret)

    print(f"  {'Strategy':>22}  {'N':>5}  {'WR':>6}  {'Mean':>8}  {'Worst':>8}")
    print(f"  {'─' * 22}  {'─' * 5}  {'─' * 6}  {'─' * 8}  {'─' * 8}")
    for name, rets in sorted(strategy_returns.items()):
        n = len(rets)
        wr = sum(1 for r in rets if r > 0) / n if n else 0
        mean_r = sum(rets) / n if n else 0
        worst = min(rets) if rets else 0
        print(f"  {name:>22}  {n:>5}  {wr:>5.1%}  {mean_r:>+7.4f}  {worst:>+7.4f}")

    # Time-based P&L curve (monthly buckets)
    if results.steps:
        print("\n  --- Monthly P&L (traded only) ---")
        monthly: dict[str, float] = {}
        for step in results.steps:
            if step.actual_pnl is not None:
                dt = datetime.fromtimestamp(step.timestamp, tz=timezone.utc)
                month_key = dt.strftime("%Y-%m")
                monthly[month_key] = monthly.get(month_key, 0) + step.actual_pnl

        for month, pnl in sorted(monthly.items()):
            bar_len = int(abs(pnl) * 500)
            bar = "+" * bar_len if pnl > 0 else "-" * bar_len
            print(f"  {month}  {pnl:>+8.4f}  {bar}")

    print("\n" + "=" * 60)
