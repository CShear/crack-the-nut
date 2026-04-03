"""Ensemble strategy: combine multiple strategy signals into weighted votes.

Instead of picking top N strategies and trading them independently,
poll ALL strategies for each asset at each timestamp and combine their
signals into a single conviction score. Trade only when consensus is
strong enough.

Usage::

    python3 -m analog.ensemble
"""

from __future__ import annotations

import asyncio
import math
import os
import pickle
from dataclasses import dataclass, field
from datetime import datetime, timezone

from analog.backfill import run_backfill, BackfillResult, INTERVAL_HOURS
from analog.evaluators import build_evaluators, HistoricalData
from analog.simulate_top_n import rank_strategies, TrainResult


@dataclass
class EnsembleSignal:
    """Aggregated signal for one asset at one timestamp."""
    asset: str
    timestamp: float
    n_long: int
    n_short: int
    n_total: int
    weighted_score: float  # positive = long conviction, negative = short
    raw_agreement: float  # |longs - shorts| / total, 0-1
    top_strategy: str  # strongest individual contributor


@dataclass
class EnsembleTrade:
    timestamp: float
    asset: str
    direction: float
    conviction: float  # 0-1 scale
    n_voters: int
    size_usd: float
    pnl_pct: float
    pnl_usd: float
    exit_reason: str
    hold_hours: float


@dataclass
class EnsembleResult:
    label: str
    trades: list[EnsembleTrade] = field(default_factory=list)
    equity_curve: list[tuple[float, float]] = field(default_factory=list)


def build_strategy_weights(
    ranked: list[TrainResult],
    method: str = "sharpe",
    top_n: int | None = None,
) -> dict[str, float]:
    """Build weight map from training-period rankings.

    Methods:
      - "equal": all strategies get weight 1.0
      - "sharpe": weight by training Sharpe ratio (negative Sharpe = 0)
      - "winrate": weight by (win_rate - 0.5), so 50% WR = 0 weight
      - "mean": weight by mean return (negative = 0)
    """
    candidates = ranked[:top_n] if top_n else ranked
    weights: dict[str, float] = {}

    for r in candidates:
        if method == "equal":
            w = 1.0
        elif method == "sharpe":
            w = max(0, r.sharpe)
        elif method == "winrate":
            w = max(0, r.win_rate - 0.45)  # only strategies above 45% WR
        elif method == "mean":
            w = max(0, r.mean_return * 100)  # scale up for readability
        else:
            w = 1.0

        if w > 0:
            weights[r.key] = w

    # Normalize so max weight = 1
    if weights:
        max_w = max(weights.values())
        if max_w > 0:
            weights = {k: v / max_w for k, v in weights.items()}

    return weights


def run_ensemble(
    result: BackfillResult,
    weights: dict[str, float],
    interval: str = "4h",
    forward_hours: float = 4.0,
    trade_start_idx: int = 0,
    initial_capital: float = 1000.0,
    conviction_threshold: float = 0.5,  # min agreement to trade
    min_voters: int = 3,  # need at least this many signals
    max_positions: int = 3,
    base_position_pct: float = 0.15,
    scale_by_conviction: bool = True,  # bigger size when more agree
    step_hours: float = 24.0,
    use_exit_logic: bool = True,
    stop_loss: float = 0.03,
    take_profit: float = 0.05,
    max_hold_hours: float = 48.0,
) -> EnsembleResult:
    """Run ensemble voting simulation."""
    interval_hours = INTERVAL_HOURS.get(interval, 4.0)

    assets = [a for a in sorted(result.candles.keys())
              if len(result.candles.get(a, [])) >= 500]

    # Build evaluators per asset
    eval_sets: dict[str, dict] = {}
    data_sets: dict[str, HistoricalData] = {}
    for asset in assets:
        evals = build_evaluators(
            result.candles, result.funding, asset=asset,
            interval_hours=interval_hours,
        )
        eval_sets[asset] = evals
        data_sets[asset] = HistoricalData(
            result.candles, result.funding,
            primary_asset=asset, interval_hours=interval_hours,
        )

    # Map weights to per-asset strategy weights
    asset_strat_weights: dict[str, dict[str, float]] = {}
    for key, w in weights.items():
        parts = key.split(":", 1)
        if len(parts) == 2:
            asset, strat = parts
            if asset not in asset_strat_weights:
                asset_strat_weights[asset] = {}
            asset_strat_weights[asset][strat] = w

    btc_candles = sorted(result.candles["BTC"], key=lambda c: c.timestamp_ms)
    step_bars = max(1, int(step_hours / interval_hours))

    equity = initial_capital
    sim = EnsembleResult(label="")
    sim.equity_curve.append((btc_candles[trade_start_idx].timestamp_ms / 1000.0, equity))

    COMMISSION = 0.0006

    for i in range(trade_start_idx, len(btc_candles), step_bars):
        ts = btc_candles[i].timestamp_ms / 1000.0

        # For each asset, aggregate signals
        asset_signals: list[EnsembleSignal] = []

        for asset in assets:
            if asset not in eval_sets:
                continue

            n_long = 0
            n_short = 0
            weighted_long = 0.0
            weighted_short = 0.0
            best_strat = ""
            best_weight = 0.0

            strat_weights = asset_strat_weights.get(asset, {})

            for strat_name, evaluator in eval_sets[asset].items():
                # Get weight — strategies not in our weight map get 0
                w = strat_weights.get(strat_name, 0)
                if w <= 0:
                    continue

                try:
                    pnl = evaluator(ts, forward_hours)
                except Exception:
                    continue

                if pnl is None:
                    continue

                # Determine direction from the evaluator
                fwd = data_sets[asset].forward_return(asset, ts, forward_hours)
                if fwd is not None and abs(fwd) > 0.0001:
                    adjusted = pnl + COMMISSION
                    direction = 1.0 if (adjusted * fwd) > 0 else -1.0
                else:
                    direction = 1.0 if pnl > 0 else -1.0

                if direction > 0:
                    n_long += 1
                    weighted_long += w
                else:
                    n_short += 1
                    weighted_short += w

                if w > best_weight:
                    best_weight = w
                    best_strat = strat_name

            n_total = n_long + n_short
            if n_total < min_voters:
                continue

            # Composite score: positive = net long, negative = net short
            weighted_score = weighted_long - weighted_short
            raw_agreement = abs(n_long - n_short) / n_total

            asset_signals.append(EnsembleSignal(
                asset=asset,
                timestamp=ts,
                n_long=n_long,
                n_short=n_short,
                n_total=n_total,
                weighted_score=weighted_score,
                raw_agreement=raw_agreement,
                top_strategy=best_strat,
            ))

        # Filter by conviction threshold
        tradeable = [s for s in asset_signals if s.raw_agreement >= conviction_threshold]
        if not tradeable:
            continue

        # Rank by absolute weighted score (strongest consensus first)
        tradeable.sort(key=lambda s: abs(s.weighted_score), reverse=True)

        # Diversify: one trade per asset
        selected = []
        seen = set()
        for sig in tradeable:
            if sig.asset not in seen:
                selected.append(sig)
                seen.add(sig.asset)
            if len(selected) >= max_positions:
                break

        for sig in selected:
            direction = 1.0 if sig.weighted_score > 0 else -1.0
            conviction = sig.raw_agreement

            # Size by conviction
            if scale_by_conviction:
                pos_pct = base_position_pct * (0.5 + conviction * 0.5)
            else:
                pos_pct = base_position_pct

            size_usd = equity * pos_pct
            if size_usd < 11:
                continue

            # Exit logic
            if use_exit_logic:
                data = data_sets.get(sig.asset)
                if data is None:
                    continue
                exit_result = data.simulate_exit(
                    sig.asset, ts, direction,
                    stop_loss=stop_loss,
                    take_profit=take_profit,
                    max_hold_hours=max_hold_hours,
                )
                if exit_result is None:
                    continue
                pnl_pct, hold_hours, exit_reason = exit_result
            else:
                # Fixed hold
                fwd = data_sets[sig.asset].forward_return(sig.asset, ts, forward_hours)
                if fwd is None:
                    continue
                pnl_pct = direction * fwd - COMMISSION
                hold_hours = forward_hours
                exit_reason = "time_exit"

            pnl_usd = size_usd * pnl_pct
            equity += pnl_usd
            equity = max(equity, 0)

            sim.trades.append(EnsembleTrade(
                timestamp=ts, asset=sig.asset, direction=direction,
                conviction=conviction, n_voters=sig.n_total,
                size_usd=size_usd, pnl_pct=pnl_pct, pnl_usd=pnl_usd,
                exit_reason=exit_reason, hold_hours=hold_hours,
            ))
            sim.equity_curve.append((ts, equity))

        if equity <= 0:
            break

    return sim


def print_ensemble_report(sim: EnsembleResult, initial: float):
    trades = sim.trades
    if not trades:
        print(f"  {sim.label}: NO TRADES")
        return

    final = sim.equity_curve[-1][1]
    total_ret = (final - initial) / initial

    n = len(trades)
    wins = sum(1 for t in trades if t.pnl_pct > 0)
    wr = wins / n if n else 0

    win_total = sum(t.pnl_usd for t in trades if t.pnl_pct > 0)
    loss_total = abs(sum(t.pnl_usd for t in trades if t.pnl_pct <= 0))
    pf = win_total / loss_total if loss_total > 0 else 999

    peak = initial
    max_dd = 0
    for _, eq in sim.equity_curve:
        peak = max(peak, eq)
        dd = (peak - eq) / peak if peak > 0 else 0
        max_dd = max(max_dd, dd)

    avg_conviction = sum(t.conviction for t in trades) / n if n else 0
    avg_voters = sum(t.n_voters for t in trades) / n if n else 0
    avg_hold = sum(t.hold_hours for t in trades) / n if n else 0

    # Exit reason breakdown
    exit_reasons: dict[str, int] = {}
    for t in trades:
        exit_reasons[t.exit_reason] = exit_reasons.get(t.exit_reason, 0) + 1

    # Sharpe
    daily_eq: dict[str, float] = {}
    for ts, eq in sim.equity_curve:
        d = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
        daily_eq[d] = eq
    prev = initial
    daily_rets = []
    for d in sorted(daily_eq.keys()):
        if prev > 0:
            daily_rets.append((daily_eq[d] - prev) / prev)
        prev = daily_eq[d]
    if daily_rets:
        avg_d = sum(daily_rets) / len(daily_rets)
        std_d = math.sqrt(sum((r - avg_d)**2 for r in daily_rets) / len(daily_rets))
        sharpe = (avg_d / std_d) * math.sqrt(365) if std_d > 0 else 0
    else:
        sharpe = 0

    print(f"\n  {'─'*70}")
    print(f"  {sim.label}")
    print(f"  {'─'*70}")
    print(f"  Return: {total_ret:>+7.1%}  Final: ${final:>7,.0f}  MaxDD: {max_dd:.1%}  "
          f"Sharpe: {sharpe:+.2f}")
    print(f"  Trades: {n}  WR: {wr:.0%}  PF: {pf:.2f}  "
          f"Avg conviction: {avg_conviction:.0%}  Avg voters: {avg_voters:.1f}")
    print(f"  Avg hold: {avg_hold:.0f}h  Exits: {exit_reasons}")

    # Monthly
    monthly: dict[str, list[float]] = {}
    for t in trades:
        m = datetime.fromtimestamp(t.timestamp, tz=timezone.utc).strftime("%Y-%m")
        monthly.setdefault(m, []).append(t.pnl_usd)

    print(f"\n  {'Month':>8}  {'N':>4}  {'WR':>4}  {'P&L':>10}  {'Cum':>10}")
    print(f"  {'─'*8}  {'─'*4}  {'─'*4}  {'─'*10}  {'─'*10}")
    cum = 0
    for m in sorted(monthly.keys()):
        pnls = monthly[m]
        w = sum(1 for p in pnls if p > 0)
        cum += sum(pnls)
        print(f"  {m:>8}  {len(pnls):>4}  {w/len(pnls):>3.0%}  "
              f"${sum(pnls):>+9,.2f}  ${cum:>+9,.2f}")

    # Per-asset
    asset_pnl: dict[str, float] = {}
    asset_n: dict[str, int] = {}
    for t in trades:
        asset_pnl[t.asset] = asset_pnl.get(t.asset, 0) + t.pnl_usd
        asset_n[t.asset] = asset_n.get(t.asset, 0) + 1

    print(f"\n  {'Asset':>10}  {'N':>4}  {'P&L':>10}")
    print(f"  {'─'*10}  {'─'*4}  {'─'*10}")
    for a in sorted(asset_pnl.keys(), key=lambda x: asset_pnl[x], reverse=True):
        print(f"  {a:>10}  {asset_n[a]:>4}  ${asset_pnl[a]:>+9,.2f}")


async def main():
    cache_path = "data/backfill_4h.pkl"

    if os.path.exists(cache_path):
        print("Loading cached 4h data...")
        with open(cache_path, "rb") as f:
            result = pickle.load(f)
    else:
        print("Backfilling 4h data...")
        result = await run_backfill(lookback_days=730, interval="4h")
        with open(cache_path, "wb") as f:
            pickle.dump(result, f)

    btc_candles = sorted(result.candles["BTC"], key=lambda c: c.timestamp_ms)
    total = len(btc_candles)
    mid = total // 2

    train_end_dt = datetime.fromtimestamp(btc_candles[mid].timestamp_ms / 1000, tz=timezone.utc)
    test_end_dt = datetime.fromtimestamp(btc_candles[-1].timestamp_ms / 1000, tz=timezone.utc)

    print(f"\n{'='*75}")
    print("  ENSEMBLE VOTING ANALYSIS")
    print(f"  Train: Apr 2024 → {train_end_dt.date()} | Trade → {test_end_dt.date()}")
    print(f"{'='*75}")

    # Rank strategies on training period
    print("\n  Ranking strategies on training period...")
    ranked = rank_strategies(result, "4h", 4.0, mid, min_trades=10)
    profitable = [r for r in ranked if r.mean_return > 0 and r.win_rate >= 0.45]
    print(f"  {len(profitable)} profitable combos available for ensemble")

    # ── Experiment 1: How many strategies to include ──
    print(f"\n{'='*75}")
    print("  EXPERIMENT 1: Pool Size — How many strategies should vote?")
    print(f"{'='*75}")

    for pool_size in [11, 20, 30, 50, 80, 138]:
        actual = min(pool_size, len(profitable))
        weights = build_strategy_weights(profitable, method="sharpe", top_n=actual)

        sim = run_ensemble(
            result, weights, interval="4h", forward_hours=4.0,
            trade_start_idx=mid, initial_capital=1000.0,
            conviction_threshold=0.6, min_voters=3,
            max_positions=3, base_position_pct=0.30,
            scale_by_conviction=True,
            use_exit_logic=True, stop_loss=0.03, take_profit=0.05,
        )
        sim.label = f"Pool={actual}, Sharpe-weighted, threshold=60%"
        print_ensemble_report(sim, 1000.0)

    # ── Experiment 2: Conviction threshold ──
    print(f"\n\n{'='*75}")
    print("  EXPERIMENT 2: Conviction Threshold — How much agreement needed?")
    print("  (Using top 50 strategies, Sharpe-weighted)")
    print(f"{'='*75}")

    weights_50 = build_strategy_weights(profitable, method="sharpe", top_n=50)

    for threshold in [0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9]:
        sim = run_ensemble(
            result, weights_50, interval="4h", forward_hours=4.0,
            trade_start_idx=mid, initial_capital=1000.0,
            conviction_threshold=threshold, min_voters=3,
            max_positions=3, base_position_pct=0.30,
            scale_by_conviction=True,
            use_exit_logic=True, stop_loss=0.03, take_profit=0.05,
        )
        sim.label = f"Threshold={threshold:.0%}, pool=50"
        print_ensemble_report(sim, 1000.0)

    # ── Experiment 3: Weighting methods ──
    print(f"\n\n{'='*75}")
    print("  EXPERIMENT 3: Weighting Method — Equal vs Sharpe vs WR vs Mean")
    print("  (Top 50 strategies, 60% threshold)")
    print(f"{'='*75}")

    for method in ["equal", "sharpe", "winrate", "mean"]:
        weights_m = build_strategy_weights(profitable, method=method, top_n=50)
        sim = run_ensemble(
            result, weights_m, interval="4h", forward_hours=4.0,
            trade_start_idx=mid, initial_capital=1000.0,
            conviction_threshold=0.6, min_voters=3,
            max_positions=3, base_position_pct=0.30,
            scale_by_conviction=True,
            use_exit_logic=True, stop_loss=0.03, take_profit=0.05,
        )
        sim.label = f"Weight={method}, pool=50, threshold=60%"
        print_ensemble_report(sim, 1000.0)

    # ── Experiment 4: Exit logic comparison ──
    print(f"\n\n{'='*75}")
    print("  EXPERIMENT 4: Ensemble + Exit Logic vs Fixed Hold")
    print("  (Best config from above)")
    print(f"{'='*75}")

    # Find best config from experiments (we'll use pool=50, sharpe, 60%)
    best_weights = build_strategy_weights(profitable, method="sharpe", top_n=50)

    for label, use_exit, sl, tp, mh in [
        ("Fixed 4h hold",     False, None, None, 4.0),
        ("Fixed 24h hold",    False, None, None, 24.0),
        ("3% SL + 5% TP",    True,  0.03, 0.05, 48.0),
        ("2% SL + 5% TP",    True,  0.02, 0.05, 48.0),
        ("2% SL + 3% TP",    True,  0.02, 0.03, 48.0),
        ("5% SL + 10% TP",   True,  0.05, 0.10, 72.0),
    ]:
        sim = run_ensemble(
            result, best_weights, interval="4h", forward_hours=4.0 if not use_exit else mh,
            trade_start_idx=mid, initial_capital=1000.0,
            conviction_threshold=0.6, min_voters=3,
            max_positions=3, base_position_pct=0.30,
            scale_by_conviction=True,
            use_exit_logic=use_exit,
            stop_loss=sl if sl else 0.03,
            take_profit=tp if tp else 0.05,
            max_hold_hours=mh,
        )
        sim.label = f"Ensemble + {label}"
        print_ensemble_report(sim, 1000.0)

    # ── Comparison: Ensemble vs Top-11 Independent ──
    print(f"\n\n{'='*75}")
    print("  FINAL COMPARISON: Ensemble vs Top-11 Independent")
    print(f"{'='*75}")

    # Top-11 independent (from previous analysis)
    from analog.simulate_top_n import simulate_portfolio as sim_top_n
    top11_keys = {r.key for r in profitable[:11]}
    top11_sim = sim_top_n(
        result, top11_keys, "4h", 4.0,
        trade_start_idx=mid, initial_capital=1000.0,
        max_positions=3, position_pct=0.30, step_hours=24.0,
    )

    top11_trades = top11_sim.trades
    if top11_trades:
        top11_final = top11_sim.equity_curve[-1][1]
        top11_ret = (top11_final - 1000) / 1000
        top11_n = len(top11_trades)
        top11_wr = sum(1 for t in top11_trades if t.pnl_pct > 0) / top11_n
        top11_peak = 1000
        top11_dd = 0
        for _, eq in top11_sim.equity_curve:
            top11_peak = max(top11_peak, eq)
            dd = (top11_peak - eq) / top11_peak
            top11_dd = max(top11_dd, dd)
    else:
        top11_ret = 0
        top11_n = 0
        top11_wr = 0
        top11_dd = 0
        top11_final = 1000

    # Best ensemble
    best_ensemble = run_ensemble(
        result, best_weights, interval="4h", forward_hours=4.0,
        trade_start_idx=mid, initial_capital=1000.0,
        conviction_threshold=0.6, min_voters=3,
        max_positions=3, base_position_pct=0.30,
        scale_by_conviction=True,
        use_exit_logic=True, stop_loss=0.03, take_profit=0.05,
        max_hold_hours=48.0,
    )
    ens_trades = best_ensemble.trades
    if ens_trades:
        ens_final = best_ensemble.equity_curve[-1][1]
        ens_ret = (ens_final - 1000) / 1000
        ens_n = len(ens_trades)
        ens_wr = sum(1 for t in ens_trades if t.pnl_pct > 0) / ens_n
        ens_peak = 1000
        ens_dd = 0
        for _, eq in best_ensemble.equity_curve:
            ens_peak = max(ens_peak, eq)
            dd = (ens_peak - eq) / ens_peak
            ens_dd = max(ens_dd, dd)
    else:
        ens_ret = 0
        ens_n = 0
        ens_wr = 0
        ens_dd = 0
        ens_final = 1000

    print(f"""
  {'Metric':<25} {'Top-11 Indep':>15} {'Ensemble (50)':>15}
  {'─'*25} {'─'*15} {'─'*15}
  {'Return':<25} {top11_ret:>+14.1%} {ens_ret:>+14.1%}
  {'Final Equity':<25} ${top11_final:>13,.0f} ${ens_final:>13,.0f}
  {'Max Drawdown':<25} {top11_dd:>14.1%} {ens_dd:>14.1%}
  {'Trades':<25} {top11_n:>15} {ens_n:>15}
  {'Win Rate':<25} {top11_wr:>14.0%} {ens_wr:>14.0%}
""")

    print(f"{'='*75}")


if __name__ == "__main__":
    asyncio.run(main())
