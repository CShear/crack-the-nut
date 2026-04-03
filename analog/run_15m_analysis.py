"""Run comprehensive high-resolution backtesting analysis.

Uses two resolutions:
  - 1h candles (~7 months) for full strategy backtesting
  - 15m candles (~53 days) for focused lead-lag analysis

Hyperliquid data availability:
  - 4h: 2+ years | 1h: ~7 months | 30m: ~3.5 months | 15m: ~53 days

Usage::

    python3 -m analog.run_15m_analysis
    python3 -m analog.run_15m_analysis --skip-backfill  # reuse cached data
"""

from __future__ import annotations

import argparse
import asyncio
import os
import pickle
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import structlog

from analog.backfill import (
    run_backfill,
    print_summary,
    BackfillResult,
    CANDLE_ASSETS,
    INTERVAL_HOURS,
)
from analog.evaluators import build_evaluators
from analog.lead_lag import measure_lead_lag, LeadLagResult

logger = structlog.get_logger()

# Primary analysis: 1h bars (~7 months of data)
INTERVAL = "1h"
INTERVAL_HRS = INTERVAL_HOURS[INTERVAL]  # 1.0
BARS_PER_DAY = int(24 / INTERVAL_HRS)  # 24
CACHE_PATH = "data/backfill_1h.pkl"

# Secondary: 15m for fine lead-lag
LL_INTERVAL = "15m"
LL_INTERVAL_HRS = INTERVAL_HOURS[LL_INTERVAL]  # 0.25
LL_CACHE_PATH = "data/backfill_15m.pkl"


@dataclass
class AssetStrategyResult:
    asset: str
    strategy: str
    n_trades: int
    win_rate: float
    mean_return: float
    worst_return: float
    total_pnl: float


def evaluate_asset(
    result: BackfillResult,
    asset: str,
    forward_hours: float = 1.0,
    step_bars: int = 24,  # every 6h at 15m
) -> list[AssetStrategyResult]:
    """Evaluate all strategies on one asset at 15m resolution."""
    evaluators = build_evaluators(
        result.candles, result.funding, asset=asset,
        interval_hours=INTERVAL_HRS,
    )
    candles = sorted(result.candles.get(asset, []), key=lambda c: c.timestamp_ms)

    if len(candles) < 500:
        return []

    warmup = int(30 * BARS_PER_DAY)  # 30 days
    results: list[AssetStrategyResult] = []

    for name, evaluator in evaluators.items():
        pnls: list[float] = []
        for i in range(warmup, len(candles), step_bars):
            ts = candles[i].timestamp_ms / 1000.0
            try:
                pnl = evaluator(ts, forward_hours)
            except Exception:
                continue
            if pnl is not None:
                pnls.append(pnl)

        if not pnls:
            continue

        n = len(pnls)
        wins = sum(1 for p in pnls if p > 0)
        wr = wins / n
        mean_ret = sum(pnls) / n
        worst = min(pnls)
        total = sum(pnls)

        results.append(AssetStrategyResult(
            asset=asset,
            strategy=name,
            n_trades=n,
            win_rate=wr,
            mean_return=mean_ret,
            worst_return=worst,
            total_pnl=total,
        ))

    return results


def run_lead_lag_analysis(
    result: BackfillResult,
    interval_hours: float = LL_INTERVAL_HRS,
) -> list[LeadLagResult]:
    """Measure lead-lag for all leader/follower pairs."""
    alts = [a for a in sorted(result.candles.keys()) if a not in ("BTC", "ETH")]
    leaders = ["BTC", "ETH"]
    ll_results: list[LeadLagResult] = []

    max_lag = int(12 / interval_hours)  # up to 12 hours
    for leader in leaders:
        for follower in alts:
            ll = measure_lead_lag(
                result.candles, leader, follower,
                max_lag_bars=max_lag,
                interval_hours=interval_hours,
            )
            if ll is not None:
                ll_results.append(ll)

    return ll_results


def print_lead_lag_table(ll_results: list[LeadLagResult]) -> None:
    print(f"\n  {'='*80}")
    print("  LEAD-LAG CORRELATION TABLE (15m resolution, 0-12h lags)")
    print(f"  {'='*80}")
    print(f"\n  {'Leader':>6}  {'Follower':>10}  {'Opt Lag':>8}  {'Corr@Lag':>9}  "
          f"{'Corr@0':>7}  {'Improve':>8}  {'N':>6}")
    print(f"  {'─'*6}  {'─'*10}  {'─'*8}  {'─'*9}  {'─'*7}  {'─'*8}  {'─'*6}")

    # Sort by improvement descending
    sorted_ll = sorted(ll_results, key=lambda x: x.improvement, reverse=True)
    for ll in sorted_ll:
        if ll.best_lag_hours >= 1:
            lag_str = f"{ll.best_lag_hours:.0f}h"
        else:
            lag_str = f"{ll.best_lag_hours * 60:.0f}m"
        print(
            f"  {ll.leader:>6}  {ll.follower:>10}  {lag_str:>8}  "
            f"{ll.correlation_at_lag:>8.4f}  {ll.correlation_at_zero:>6.4f}  "
            f"{ll.improvement:>+7.4f}  {ll.n_observations:>6}"
        )


def print_top_combos(viable: list[AssetStrategyResult], top_n: int = 50) -> None:
    print(f"\n  {'='*80}")
    print(f"  TOP {top_n} STRATEGY-ASSET COMBINATIONS (1h bars, by mean return, N>=10)")
    print(f"  {'='*80}")
    print(f"\n  {'Rank':>4}  {'Asset':>10}  {'Strategy':>28}  "
          f"{'N':>5}  {'WR':>6}  {'Mean':>8}  {'Total':>8}  {'Worst':>8}")
    print(f"  {'─'*4}  {'─'*10}  {'─'*28}  {'─'*5}  {'─'*6}  {'─'*8}  {'─'*8}  {'─'*8}")

    for rank, r in enumerate(viable[:top_n], 1):
        print(
            f"  {rank:>4}  {r.asset:>10}  {r.strategy:>28}  "
            f"{r.n_trades:>5}  {r.win_rate:>5.1%}  {r.mean_return:>+7.4f}  "
            f"{r.total_pnl:>+7.4f}  {r.worst_return:>+7.4f}"
        )


def print_robustness(viable: list[AssetStrategyResult]) -> None:
    print(f"\n  {'='*80}")
    print("  STRATEGY ROBUSTNESS — positive mean on how many assets? (N>=10)")
    print(f"  {'='*80}")

    strategy_names = sorted(set(r.strategy for r in viable))
    robust: list[tuple[str, int, int, float]] = []
    for strat in strategy_names:
        strat_results = [r for r in viable if r.strategy == strat]
        n_positive = sum(1 for r in strat_results if r.mean_return > 0)
        n_tested = len(strat_results)
        avg_mean = sum(r.mean_return for r in strat_results) / n_tested if n_tested else 0
        robust.append((strat, n_positive, n_tested, avg_mean))

    robust.sort(key=lambda x: (x[1], x[3]), reverse=True)

    print(f"\n  {'Strategy':>28}  {'Pos Assets':>10}  {'Tested':>7}  {'Avg Mean':>9}")
    print(f"  {'─'*28}  {'─'*10}  {'─'*7}  {'─'*9}")
    for strat, n_pos, n_tested, avg_mean in robust[:30]:
        if n_pos > 0:
            print(f"  {strat:>28}  {n_pos:>10}  {n_tested:>7}  {avg_mean:>+8.4f}")


def print_asset_summary(viable: list[AssetStrategyResult], assets: list[str]) -> None:
    print(f"\n  {'='*80}")
    print("  ASSET SUMMARY — strategies with positive mean return (N>=10)")
    print(f"  {'='*80}")
    print(f"\n  {'Asset':>10}  {'Winners':>8}  {'Total':>6}  "
          f"{'Best Strategy':>28}  {'Best Mean':>9}")
    print(f"  {'─'*10}  {'─'*8}  {'─'*6}  {'─'*28}  {'─'*9}")

    new_assets = {"TAO", "HYPE", "SPX", "FARTCOIN"}
    for asset in assets:
        asset_viable = [r for r in viable if r.asset == asset]
        winners = [r for r in asset_viable if r.mean_return > 0]
        marker = " *NEW*" if asset in new_assets else ""
        if not asset_viable:
            print(f"  {asset:>10}  {'n/a':>8}  {'n/a':>6}{marker}")
            continue
        best = max(asset_viable, key=lambda r: r.mean_return)
        print(
            f"  {asset:>10}  {len(winners):>8}  {len(asset_viable):>6}  "
            f"{best.strategy:>28}  {best.mean_return:>+8.4f}{marker}"
        )


def print_new_asset_spotlight(viable: list[AssetStrategyResult]) -> None:
    """Highlight how the 4 new assets performed."""
    new_assets = ["TAO", "HYPE", "SPX", "FARTCOIN"]
    print(f"\n  {'='*80}")
    print("  NEW ASSET SPOTLIGHT — TAO, HYPE, SPX, FARTCOIN")
    print(f"  {'='*80}")

    for asset in new_assets:
        asset_results = [r for r in viable if r.asset == asset]
        if not asset_results:
            print(f"\n  {asset}: No viable strategies (insufficient data or < 10 trades)")
            continue

        winners = [r for r in asset_results if r.mean_return > 0]
        best = max(asset_results, key=lambda r: r.mean_return)
        worst = min(asset_results, key=lambda r: r.mean_return)

        print(f"\n  {asset}:")
        print(f"    Viable strategies: {len(asset_results)}")
        print(f"    Profitable: {len(winners)} ({100*len(winners)/len(asset_results):.0f}%)")
        print(f"    Best:  {best.strategy:>28}  mean={best.mean_return:>+7.4f}  WR={best.win_rate:.1%}")
        print(f"    Worst: {worst.strategy:>28}  mean={worst.mean_return:>+7.4f}  WR={worst.win_rate:.1%}")


async def main(
    lookback_days: int = 730,
    forward_hours: float = 1.0,
    top_n: int = 50,
    skip_backfill: bool = False,
    step_bars: int = 6,  # every 6 bars = every 6h at 1h
):
    print("=" * 80)
    print("  HIGH-RESOLUTION MULTI-ASSET STRATEGY ANALYSIS")
    print(f"  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print("  Strategy eval: 1h bars (~7mo) | Lead-lag: 15m bars (~53d)")
    print(f"  Forward: {forward_hours}h | Step: every {step_bars} bars ({step_bars}h)")
    print("=" * 80)

    # --- 1. Backfill 1h data for strategy evaluation ---
    if skip_backfill and os.path.exists(CACHE_PATH):
        print(f"\n[1/5] Loading cached 1h backfill from {CACHE_PATH}...")
        with open(CACHE_PATH, "rb") as f:
            result_1h = pickle.load(f)
        print(f"  Loaded {sum(len(v) for v in result_1h.candles.values())} candles "
              f"across {len(result_1h.candles)} assets")
    else:
        print(f"\n[1/5] Backfilling {len(CANDLE_ASSETS)} assets at 1h "
              f"({lookback_days} days)...")
        print("  HL has ~7 months of 1h data. This takes ~10-15 minutes.\n")
        t0 = time.time()
        result_1h = await run_backfill(
            lookback_days=lookback_days,
            data_dir="data/fingerprints_1h",
            interval=INTERVAL,
        )
        elapsed = time.time() - t0
        print(f"\n  1h backfill completed in {elapsed/60:.1f} minutes")
        print_summary(result_1h)

        os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
        with open(CACHE_PATH, "wb") as f:
            pickle.dump(result_1h, f)
        print(f"  Cached to {CACHE_PATH}")

    assets_1h = sorted(result_1h.candles.keys())
    print(f"\n  1h assets: {', '.join(assets_1h)}")
    for asset in assets_1h:
        n = len(result_1h.candles.get(asset, []))
        days = n / BARS_PER_DAY
        print(f"    {asset:>10}: {n:>6} candles ({days:.0f} days)")

    # --- 2. Backfill 15m data for lead-lag ---
    if skip_backfill and os.path.exists(LL_CACHE_PATH):
        print(f"\n[2/5] Loading cached 15m backfill from {LL_CACHE_PATH}...")
        with open(LL_CACHE_PATH, "rb") as f:
            result_15m = pickle.load(f)
        print(f"  Loaded {sum(len(v) for v in result_15m.candles.values())} candles "
              f"across {len(result_15m.candles)} assets")
    else:
        print("\n[2/5] Backfilling 15m data for lead-lag analysis (53 days avail)...")
        t0 = time.time()
        result_15m = await run_backfill(
            lookback_days=60,  # HL has ~53 days of 15m data
            data_dir="data/fingerprints_15m",
            interval=LL_INTERVAL,
        )
        elapsed = time.time() - t0
        print(f"  15m backfill completed in {elapsed/60:.1f} minutes")

        os.makedirs(os.path.dirname(LL_CACHE_PATH), exist_ok=True)
        with open(LL_CACHE_PATH, "wb") as f:
            pickle.dump(result_15m, f)

    assets_15m = sorted(result_15m.candles.keys())
    print(f"  15m assets: {', '.join(assets_15m)}")
    for asset in assets_15m:
        n = len(result_15m.candles.get(asset, []))
        days = n / (24 / LL_INTERVAL_HRS)
        print(f"    {asset:>10}: {n:>6} candles ({days:.0f} days)")

    # --- 3. Strategy evaluation on 1h data ---
    print(f"\n[3/5] Evaluating strategies across {len(assets_1h)} assets "
          f"(1h bars, forward={forward_hours}h, step={step_bars})...")
    t0 = time.time()
    all_results: list[AssetStrategyResult] = []

    for asset in assets_1h:
        n_candles = len(result_1h.candles.get(asset, []))
        if n_candles < 500:
            print(f"  {asset:>10}: skipped (only {n_candles} candles)")
            continue
        print(f"  {asset:>10}: {n_candles} candles, evaluating...", end="", flush=True)
        asset_results = evaluate_asset(
            result_1h, asset, forward_hours=forward_hours, step_bars=step_bars,
        )
        n_with_trades = len([r for r in asset_results if r.n_trades > 0])
        print(f" {n_with_trades} strategies with trades")
        all_results.extend(asset_results)

    eval_time = time.time() - t0
    print(f"\n  Strategy evaluation completed in {eval_time/60:.1f} minutes")

    # --- 4. Lead-lag on 15m data ---
    print("\n[4/5] Running lead-lag analysis at 15m resolution...")
    t0 = time.time()

    # 15m lead-lag
    ll_15m = run_lead_lag_analysis(result_15m)

    # Also run lead-lag on 1h data for comparison
    ll_1h: list[LeadLagResult] = []
    alts_1h = [a for a in assets_1h if a not in ("BTC", "ETH")]
    for leader in ["BTC", "ETH"]:
        for follower in alts_1h:
            ll = measure_lead_lag(
                result_1h.candles, leader, follower,
                max_lag_bars=24,  # 0 to 24h at 1h
                interval_hours=INTERVAL_HRS,
            )
            if ll is not None:
                ll_1h.append(ll)

    ll_time = time.time() - t0
    print(f"  Lead-lag completed in {ll_time:.1f}s "
          f"({len(ll_15m)} 15m pairs, {len(ll_1h)} 1h pairs)")

    # --- 5. Report ---
    print("\n[5/5] Generating report...\n")

    # Lead-lag: 15m
    print_lead_lag_table(ll_15m)

    # Lead-lag: 1h comparison
    if ll_1h:
        print(f"\n  {'='*80}")
        print("  LEAD-LAG CORRELATION TABLE (1h resolution, 0-24h lags)")
        print(f"  {'='*80}")
        print(f"\n  {'Leader':>6}  {'Follower':>10}  {'Opt Lag':>8}  {'Corr@Lag':>9}  "
              f"{'Corr@0':>7}  {'Improve':>8}  {'N':>6}")
        print(f"  {'─'*6}  {'─'*10}  {'─'*8}  {'─'*9}  {'─'*7}  {'─'*8}  {'─'*6}")
        sorted_ll = sorted(ll_1h, key=lambda x: x.improvement, reverse=True)
        for ll in sorted_ll:
            lag_str = f"{ll.best_lag_hours:.0f}h" if ll.best_lag_hours >= 1 else f"{ll.best_lag_hours * 60:.0f}m"
            print(
                f"  {ll.leader:>6}  {ll.follower:>10}  {lag_str:>8}  "
                f"{ll.correlation_at_lag:>8.4f}  {ll.correlation_at_zero:>6.4f}  "
                f"{ll.improvement:>+7.4f}  {ll.n_observations:>6}"
            )

    # Filter viable
    viable = [r for r in all_results if r.n_trades >= 10]
    viable.sort(key=lambda r: r.mean_return, reverse=True)

    # Top combos
    print_top_combos(viable, top_n)

    # Strategy robustness
    print_robustness(viable)

    # Asset summary
    print_asset_summary(viable, assets_1h)

    # New asset spotlight
    print_new_asset_spotlight(viable)

    # Summary stats
    n_profitable = sum(1 for r in viable if r.mean_return > 0)
    print(f"\n  {'='*80}")
    print("  SUMMARY")
    print(f"  {'='*80}")
    print(f"  Total combinations tested: {len(all_results)}")
    print(f"  Viable (N>=10): {len(viable)}")
    if viable:
        print(f"  Profitable: {n_profitable} ({100*n_profitable/len(viable):.1f}%)")
    else:
        print("  Profitable: 0")
    print(f"  Forward window: {forward_hours}h")
    print("  Strategy resolution: 1h bars")
    print("  Lead-lag resolution: 15m bars")
    print(f"  Lead-lag pairs: {len(ll_15m)} (15m) + {len(ll_1h)} (1h)")

    # Lead-lag highlights
    for label, ll_data in [("15m", ll_15m), ("1h", ll_1h)]:
        if ll_data:
            best = max(ll_data, key=lambda x: x.improvement)
            lag_str = f"{best.best_lag_hours:.0f}h" if best.best_lag_hours >= 1 else f"{best.best_lag_hours * 60:.0f}m"
            print(f"  Best lead-lag ({label}): {best.leader} → {best.follower} "
                  f"at {lag_str} (corr improvement: {best.improvement:+.4f})")

    print(f"  {'='*80}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="High-Resolution Multi-Asset Strategy Analysis (1h + 15m)")
    parser.add_argument("--days", type=int, default=730, help="Lookback days (capped by HL availability)")
    parser.add_argument("--forward", type=float, default=1.0,
                        help="Forward hours for P&L evaluation (default 1h)")
    parser.add_argument("--top", type=int, default=50, help="Top N to show")
    parser.add_argument("--skip-backfill", action="store_true",
                        help="Skip backfill if cached data exists")
    parser.add_argument("--step", type=int, default=6,
                        help="Step interval in bars (6=6h at 1h bars)")
    args = parser.parse_args()

    asyncio.run(main(
        lookback_days=args.days,
        forward_hours=args.forward,
        top_n=args.top,
        skip_backfill=args.skip_backfill,
        step_bars=args.step,
    ))
