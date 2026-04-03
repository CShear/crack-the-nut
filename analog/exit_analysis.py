"""Exit-logic analysis: compare stop-loss, take-profit, and trailing stop variants.

Walk-forward: rank strategies on first half, test exit variants on second half.

Usage::

    python3 -m analog.exit_analysis
"""

from __future__ import annotations

import hashlib
import os
import pickle
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from analog.backfill import BackfillResult, INTERVAL_HOURS
from analog.evaluators import HistoricalData, build_evaluators
from analog.simulate_top_n import rank_strategies

COMMISSION = 0.0006
INTERVAL = "4h"
FORWARD_HOURS = 4.0
INTERVAL_H = INTERVAL_HOURS.get(INTERVAL, 4.0)

# ── Exit variants to test ──────────────────────────────────────────────
EXIT_VARIANTS: list[tuple[str, dict]] = [
    # Fixed hold (baseline)
    ("hold_1h",    dict(max_hold_hours=1)),
    ("hold_4h",    dict(max_hold_hours=4)),
    ("hold_8h",    dict(max_hold_hours=8)),
    ("hold_24h",   dict(max_hold_hours=24)),
    ("hold_48h",   dict(max_hold_hours=48)),
    ("hold_72h",   dict(max_hold_hours=72)),
    # Stop-loss only
    ("sl_2pct",    dict(stop_loss=0.02, max_hold_hours=48)),
    ("sl_3pct",    dict(stop_loss=0.03, max_hold_hours=48)),
    ("sl_5pct",    dict(stop_loss=0.05, max_hold_hours=48)),
    # Take-profit only
    ("tp_2pct",    dict(take_profit=0.02, max_hold_hours=48)),
    ("tp_3pct",    dict(take_profit=0.03, max_hold_hours=48)),
    ("tp_5pct",    dict(take_profit=0.05, max_hold_hours=48)),
    # Combined SL + TP
    ("sl2_tp3",    dict(stop_loss=0.02, take_profit=0.03, max_hold_hours=48)),
    ("sl2_tp5",    dict(stop_loss=0.02, take_profit=0.05, max_hold_hours=48)),
    ("sl3_tp5",    dict(stop_loss=0.03, take_profit=0.05, max_hold_hours=48)),
    # Trailing stop
    ("trail_2pct", dict(trailing_stop=0.02, max_hold_hours=72)),
    ("trail_3pct", dict(trailing_stop=0.03, max_hold_hours=72)),
    ("trail_5pct", dict(trailing_stop=0.05, max_hold_hours=72)),
    # Trailing + TP
    ("trail2_tp5", dict(trailing_stop=0.02, take_profit=0.05, max_hold_hours=72)),
    ("trail3_tp5", dict(trailing_stop=0.03, take_profit=0.05, max_hold_hours=72)),
]


@dataclass
class ExitVariantResult:
    label: str
    n_trades: int
    wins: int
    mean_pnl: float
    total_pnl: float
    avg_hold_hours: float
    exit_counts: dict[str, int] = field(default_factory=dict)


@dataclass
class PortfolioTrade:
    entry_ts: float
    asset: str
    strategy: str
    direction: float
    exit_params: dict
    entry_idx: int


def infer_direction(
    data: HistoricalData,
    asset: str,
    ts: float,
    evaluator_pnl: float,
    forward_hours: float,
) -> float:
    """Infer trade direction from evaluator P&L and forward return."""
    fwd = data.forward_return(asset, ts, forward_hours)
    if fwd is not None and abs(fwd) > 0.0001:
        adjusted = evaluator_pnl + COMMISSION
        return 1.0 if (adjusted / fwd) > 0 else -1.0
    return 1.0 if evaluator_pnl > 0 else -1.0


def test_exit_variant(
    data: HistoricalData,
    asset: str,
    evaluator,
    forward_hours: float,
    candles_list,
    start_idx: int,
    end_idx: int,
    step: int,
    exit_params: dict,
) -> ExitVariantResult | None:
    """Test one exit variant on out-of-sample candles."""
    pnls: list[float] = []
    hold_hours: list[float] = []
    exit_counts: dict[str, int] = {"stop_loss": 0, "take_profit": 0,
                                    "trailing_stop": 0, "time_exit": 0}

    for i in range(start_idx, end_idx, step):
        ts = candles_list[i].timestamp_ms / 1000.0
        try:
            ev_pnl = evaluator(ts, forward_hours)
        except Exception:
            continue
        if ev_pnl is None:
            continue

        direction = infer_direction(data, asset, ts, ev_pnl, forward_hours)
        result = data.simulate_exit(asset, ts, direction, **exit_params)
        if result is None:
            continue

        pnl, hrs, reason = result
        pnls.append(pnl)
        hold_hours.append(hrs)
        exit_counts[reason] = exit_counts.get(reason, 0) + 1

    if not pnls:
        return None

    n = len(pnls)
    wins = sum(1 for p in pnls if p > 0)
    mean = sum(pnls) / n
    total = sum(pnls)
    avg_hold = sum(hold_hours) / n

    return ExitVariantResult(
        label="", n_trades=n, wins=wins, mean_pnl=mean,
        total_pnl=total, avg_hold_hours=avg_hold, exit_counts=exit_counts,
    )


def run_analysis():
    """Main analysis routine."""
    cache_path = "data/backfill_4h.pkl"

    if not os.path.exists(cache_path):
        print(f"ERROR: {cache_path} not found. Run backfill first.")
        return

    print("Loading cached 4h data...")
    with open(cache_path, "rb") as f:
        result: BackfillResult = pickle.load(f)

    btc_candles = sorted(result.candles["BTC"], key=lambda c: c.timestamp_ms)
    total = len(btc_candles)
    mid = total // 2
    bars_per_day = 24.0 / INTERVAL_H
    warmup = int(60 * bars_per_day)
    step = max(1, int(24 / INTERVAL_H))  # daily stepping

    train_end_dt = datetime.fromtimestamp(
        btc_candles[mid].timestamp_ms / 1000, tz=timezone.utc
    )
    test_end_dt = datetime.fromtimestamp(
        btc_candles[-1].timestamp_ms / 1000, tz=timezone.utc
    )

    print(f"\n{'='*80}")
    print("  EXIT LOGIC ANALYSIS")
    print(f"  Train: start -> {train_end_dt.date()}")
    print(f"  OOS:   {train_end_dt.date()} -> {test_end_dt.date()}")
    print(f"  {len(EXIT_VARIANTS)} exit variants x top 20 strategy-asset combos")
    print(f"{'='*80}")

    # ── Phase 1: Rank strategies on training period ──
    print("\n  Ranking strategies on training period...")
    t0 = time.time()
    ranked = rank_strategies(result, INTERVAL, FORWARD_HOURS, mid, min_trades=10)
    profitable = [r for r in ranked if r.mean_return > 0 and r.win_rate >= 0.45]
    print(f"  {len(profitable)} profitable combos found in {time.time()-t0:.1f}s")

    top20 = profitable[:20]
    if not top20:
        print("  No profitable strategies found. Exiting.")
        return

    print("\n  Top 20 combos for exit analysis:")
    print(f"  {'Rank':>4}  {'Sharpe':>7}  {'Mean':>7}  {'WR':>5}  {'N':>4}  {'Key'}")
    print(f"  {'---':>4}  {'---':>7}  {'---':>7}  {'---':>5}  {'---':>4}  {'---'}")
    for i, r in enumerate(top20, 1):
        print(
            f"  {i:>4}  {r.sharpe:>+6.3f}  {r.mean_return:>+6.3%}  "
            f"{r.win_rate:>4.0%}  {r.n_trades:>4}  {r.key}"
        )

    # ── Phase 2: Test all exit variants on OOS for each combo ──
    print("\n  Testing exit variants on out-of-sample period...")
    print("  (This may take a few minutes)\n")

    # Build evaluators and HistoricalData per asset
    asset_evaluators: dict[str, dict[str, object]] = {}
    asset_data: dict[str, HistoricalData] = {}

    for r in top20:
        if r.asset not in asset_evaluators:
            evals = build_evaluators(
                result.candles, result.funding, asset=r.asset,
                interval_hours=INTERVAL_H,
            )
            asset_evaluators[r.asset] = evals
            # Build HistoricalData for simulate_exit
            asset_data[r.asset] = HistoricalData(
                result.candles, result.funding,
                primary_asset=r.asset, interval_hours=INTERVAL_H,
            )

    # Per-combo results: combo_key -> list of (variant_label, ExitVariantResult)
    combo_results: dict[str, list[tuple[str, ExitVariantResult]]] = {}
    best_exit_per_combo: dict[str, tuple[str, dict, ExitVariantResult]] = {}

    for combo_idx, r in enumerate(top20, 1):
        asset = r.asset
        strategy = r.strategy
        key = r.key
        data = asset_data[asset]
        evaluator = asset_evaluators[asset].get(strategy)
        if evaluator is None:
            print(f"  WARNING: evaluator not found for {key}")
            continue

        candles = sorted(result.candles[asset], key=lambda c: c.timestamp_ms)
        cap = len(candles)
        oos_start = max(warmup, mid)

        variant_results: list[tuple[str, ExitVariantResult]] = []

        for vlabel, vparams in EXIT_VARIANTS:
            vr = test_exit_variant(
                data, asset, evaluator, FORWARD_HOURS,
                candles, oos_start, cap, step, vparams,
            )
            if vr is not None:
                vr.label = vlabel
                variant_results.append((vlabel, vr))

        combo_results[key] = variant_results

        # Find best variant by mean P&L (must have >= 5 trades)
        valid = [(vl, vr) for vl, vr in variant_results if vr.n_trades >= 5]
        if valid:
            best_label, best_vr = max(valid, key=lambda x: x[1].mean_pnl)
            # Find the params dict for the best label
            best_params = dict(next(p for el, p in EXIT_VARIANTS if el == best_label))
            best_exit_per_combo[key] = (best_label, best_params, best_vr)

        # Print table for this combo
        print(f"\n  [{combo_idx}/20] {key} (Training Sharpe: {r.sharpe:+.3f})")
        print(f"  {'─'*90}")
        print(
            f"  {'Exit Logic':<14} {'N':>4} {'WR':>5} {'Mean':>8} "
            f"{'Total':>8} {'AvgHold':>7}  {'SL%':>5} {'TP%':>5} {'Trail%':>6} {'Time%':>5}"
        )
        print(
            f"  {'─'*14} {'─'*4} {'─'*5} {'─'*8} "
            f"{'─'*8} {'─'*7}  {'─'*5} {'─'*5} {'─'*6} {'─'*5}"
        )

        for vlabel, vr in variant_results:
            n = vr.n_trades
            wr = vr.wins / n if n else 0
            sl_pct = vr.exit_counts.get("stop_loss", 0) / n * 100 if n else 0
            tp_pct = vr.exit_counts.get("take_profit", 0) / n * 100 if n else 0
            tr_pct = vr.exit_counts.get("trailing_stop", 0) / n * 100 if n else 0
            tm_pct = vr.exit_counts.get("time_exit", 0) / n * 100 if n else 0

            marker = " <-- BEST" if key in best_exit_per_combo and best_exit_per_combo[key][0] == vlabel else ""
            print(
                f"  {vlabel:<14} {n:>4} {wr:>4.0%} {vr.mean_pnl:>+7.3%} "
                f"{vr.total_pnl:>+7.3f} {vr.avg_hold_hours:>6.1f}h  "
                f"{sl_pct:>4.0f}% {tp_pct:>4.0f}% {tr_pct:>5.0f}% {tm_pct:>4.0f}%{marker}"
            )

    # ── Summary Table 1: Best exit per strategy ──
    print(f"\n\n{'='*80}")
    print("  SUMMARY 1: BEST EXIT PER STRATEGY")
    print(f"{'='*80}")
    print(
        f"  {'Key':<35} {'Best Exit':<14} {'N':>4} {'WR':>5} "
        f"{'Mean':>8} {'AvgHold':>7}"
    )
    print(f"  {'─'*35} {'─'*14} {'─'*4} {'─'*5} {'─'*8} {'─'*7}")

    for r in top20:
        key = r.key
        if key in best_exit_per_combo:
            bl, bp, bvr = best_exit_per_combo[key]
            n = bvr.n_trades
            wr = bvr.wins / n if n else 0
            print(
                f"  {key:<35} {bl:<14} {n:>4} {wr:>4.0%} "
                f"{bvr.mean_pnl:>+7.3%} {bvr.avg_hold_hours:>6.1f}h"
            )
        else:
            print(f"  {key:<35} {'N/A':<14}")

    # ── Summary Table 2: Best exit overall (avg improvement across combos) ──
    print(f"\n\n{'='*80}")
    print("  SUMMARY 2: EXIT VARIANT RANKINGS (avg mean P&L across all combos)")
    print(f"{'='*80}")

    variant_agg: dict[str, list[float]] = {}
    for key, variants in combo_results.items():
        for vlabel, vr in variants:
            if vr.n_trades >= 5:
                variant_agg.setdefault(vlabel, []).append(vr.mean_pnl)

    variant_avg = []
    for vlabel, means in variant_agg.items():
        avg = sum(means) / len(means) if means else 0
        variant_avg.append((vlabel, avg, len(means)))

    variant_avg.sort(key=lambda x: x[1], reverse=True)

    print(f"  {'Exit Variant':<14} {'Avg Mean P&L':>12} {'# Combos':>9}")
    print(f"  {'─'*14} {'─'*12} {'─'*9}")
    for vlabel, avg, count in variant_avg:
        print(f"  {vlabel:<14} {avg:>+11.4%} {count:>9}")

    # ── Summary Table 3: Portfolio simulation comparison ──
    print(f"\n\n{'='*80}")
    print("  SUMMARY 3: PORTFOLIO SIMULATION -- Fixed 4h vs Optimal Exit")
    print(f"{'='*80}")

    top10 = top20[:10]
    initial_capital = 1000.0
    max_positions = 3
    position_pct = 0.10

    # --- Fixed 4h baseline portfolio ---
    fixed_equity, fixed_trades_list, fixed_curve = _run_portfolio_sim(
        result, top10, asset_evaluators, asset_data,
        mid, step, warmup,
        exit_override=dict(max_hold_hours=4),
        initial_capital=initial_capital,
        max_positions=max_positions,
        position_pct=position_pct,
    )

    # --- Optimal exit portfolio ---
    optimal_equity, optimal_trades_list, optimal_curve = _run_portfolio_sim(
        result, top10, asset_evaluators, asset_data,
        mid, step, warmup,
        exit_per_combo=best_exit_per_combo,
        initial_capital=initial_capital,
        max_positions=max_positions,
        position_pct=position_pct,
    )

    _print_portfolio_comparison(
        "Fixed 4h Hold", fixed_equity, fixed_trades_list, fixed_curve,
        "Optimal Exit", optimal_equity, optimal_trades_list, optimal_curve,
        initial_capital,
    )


def _run_portfolio_sim(
    result: BackfillResult,
    top_combos,
    asset_evaluators: dict,
    asset_data: dict,
    mid: int,
    step: int,
    warmup: int,
    exit_override: dict | None = None,
    exit_per_combo: dict | None = None,
    initial_capital: float = 1000.0,
    max_positions: int = 3,
    position_pct: float = 0.10,
) -> tuple[float, list[dict], list[tuple[float, float]]]:
    """Run portfolio sim with bar-by-bar exit logic.

    Returns (final_equity, trades_list, equity_curve).
    """
    btc_candles = sorted(result.candles["BTC"], key=lambda c: c.timestamp_ms)
    total_bars = len(btc_candles)

    equity = initial_capital
    trades_out: list[dict] = []
    equity_curve: list[tuple[float, float]] = []
    equity_curve.append((btc_candles[mid].timestamp_ms / 1000.0, equity))

    for i in range(mid, total_bars, step):
        ts = btc_candles[i].timestamp_ms / 1000.0

        # Check for signals from allowed strategies
        signals: list[tuple[str, str, float, float]] = []  # (asset, strat, ev_pnl, direction)
        for r in top_combos:
            evaluator = asset_evaluators.get(r.asset, {}).get(r.strategy)
            if evaluator is None:
                continue
            try:
                ev_pnl = evaluator(ts, FORWARD_HOURS)
            except Exception:
                continue
            if ev_pnl is None:
                continue

            data = asset_data[r.asset]
            direction = infer_direction(data, r.asset, ts, ev_pnl, FORWARD_HOURS)

            # Get exit params
            if exit_override is not None:
                params = dict(exit_override)
            elif exit_per_combo is not None and r.key in exit_per_combo:
                _, params, _ = exit_per_combo[r.key]
                params = dict(params)
            else:
                params = dict(max_hold_hours=4)

            signals.append((r.asset, r.strategy, ev_pnl, direction))

        if not signals:
            continue

        # Deterministic shuffle
        seed = int(hashlib.md5(str(ts).encode()).hexdigest()[:8], 16)
        indices = list(range(len(signals)))
        for j in range(len(indices) - 1, 0, -1):
            seed = (seed * 1103515245 + 12345) & 0x7FFFFFFF
            k = seed % (j + 1)
            indices[j], indices[k] = indices[k], indices[j]
        shuffled = [signals[idx] for idx in indices]

        # Diversify across assets
        selected: list[tuple[str, str, float, float]] = []
        seen_assets: set[str] = set()
        for sig in shuffled:
            if sig[0] not in seen_assets:
                selected.append(sig)
                seen_assets.add(sig[0])
            if len(selected) >= max_positions:
                break
        if len(selected) < max_positions:
            for sig in shuffled:
                if sig not in selected:
                    selected.append(sig)
                if len(selected) >= max_positions:
                    break

        for asset, strat_name, ev_pnl, direction in selected:
            key = f"{asset}:{strat_name}"
            size_usd = equity * position_pct
            if size_usd < 11:
                continue

            data = asset_data[asset]

            if exit_override is not None:
                params = dict(exit_override)
            elif exit_per_combo is not None and key in exit_per_combo:
                _, params, _ = exit_per_combo[key]
                params = dict(params)
            else:
                params = dict(max_hold_hours=4)

            sim_result = data.simulate_exit(asset, ts, direction, **params)
            if sim_result is None:
                continue

            pnl_pct, hold_hrs, exit_reason = sim_result
            pnl_usd = size_usd * pnl_pct
            equity += pnl_usd
            equity = max(equity, 0)

            trades_out.append(dict(
                ts=ts, asset=asset, strategy=strat_name,
                size_usd=size_usd, pnl_pct=pnl_pct, pnl_usd=pnl_usd,
                hold_hours=hold_hrs, exit_reason=exit_reason,
            ))
            equity_curve.append((ts, equity))

        if equity <= 0:
            break

    return equity, trades_out, equity_curve


def _print_portfolio_comparison(
    label_a: str, equity_a: float, trades_a: list[dict], curve_a: list,
    label_b: str, equity_b: float, trades_b: list[dict], curve_b: list,
    initial: float,
):
    """Print side-by-side portfolio comparison."""
    for label, equity, trades, curve in [
        (label_a, equity_a, trades_a, curve_a),
        (label_b, equity_b, trades_b, curve_b),
    ]:
        n = len(trades)
        if n == 0:
            print(f"\n  {label}: NO TRADES")
            continue

        ret = (equity - initial) / initial
        wins = sum(1 for t in trades if t["pnl_pct"] > 0)
        wr = wins / n if n else 0
        avg_pnl = sum(t["pnl_pct"] for t in trades) / n
        avg_hold = sum(t.get("hold_hours", 4) for t in trades) / n

        # Drawdown
        peak = initial
        max_dd = 0
        for _, eq in curve:
            peak = max(peak, eq)
            dd = (peak - eq) / peak if peak > 0 else 0
            max_dd = max(max_dd, dd)

        # Profit factor
        win_total = sum(t["pnl_usd"] for t in trades if t["pnl_pct"] > 0)
        loss_total = abs(sum(t["pnl_usd"] for t in trades if t["pnl_pct"] <= 0))
        pf = win_total / loss_total if loss_total > 0 else float("inf")

        # Exit reason breakdown
        reason_counts: dict[str, int] = {}
        for t in trades:
            r = t.get("exit_reason", "time_exit")
            reason_counts[r] = reason_counts.get(r, 0) + 1

        first_dt = datetime.fromtimestamp(trades[0]["ts"], tz=timezone.utc)
        last_dt = datetime.fromtimestamp(trades[-1]["ts"], tz=timezone.utc)
        days = max(1, (last_dt - first_dt).days)
        ann_ret = (1 + ret) ** (365 / days) - 1 if ret > -1 else -1.0

        print(f"\n  {'─'*60}")
        print(f"  {label}")
        print(f"  {'─'*60}")
        print(f"  Period:        {first_dt.date()} -> {last_dt.date()} ({days}d)")
        print(f"  Starting:      ${initial:,.2f}")
        print(f"  Ending:        ${equity:,.2f}")
        print(f"  Total Return:  {ret:+.1%} (${equity - initial:+,.2f})")
        print(f"  Annualized:    {ann_ret:+.1%}")
        print(f"  Max Drawdown:  {max_dd:.1%}")
        print(f"  Trades:        {n} ({n/days:.1f}/day)")
        print(f"  Win Rate:      {wr:.1%}")
        print(f"  Avg P&L:       {avg_pnl:+.3%}")
        print(f"  Avg Hold:      {avg_hold:.1f}h")
        print(f"  Profit Factor: {pf:.2f}")
        print(f"  Exit Reasons:  {reason_counts}")

    # Comparison
    if trades_a and trades_b:
        ret_a = (equity_a - initial) / initial
        ret_b = (equity_b - initial) / initial
        diff = ret_b - ret_a
        print(f"\n  {'='*60}")
        print(f"  IMPROVEMENT: {label_b} vs {label_a}")
        print(f"  Return delta:  {diff:+.1%} ({ret_a:+.1%} -> {ret_b:+.1%})")
        print(f"  {'='*60}")


def main():
    run_analysis()


if __name__ == "__main__":
    main()
