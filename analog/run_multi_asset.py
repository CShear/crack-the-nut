"""Run walk-forward across all assets and find the best strategy-asset combinations.

Usage::

    python3 -m analog.run_multi_asset
    python3 -m analog.run_multi_asset --days 365
    python3 -m analog.run_multi_asset --top 20
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass

import structlog

from analog.backfill import run_backfill, print_summary, BackfillResult
from analog.evaluators import build_evaluators

logger = structlog.get_logger()


@dataclass
class AssetStrategyResult:
    """Result for one strategy on one asset."""

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
    forward_hours: float = 4.0,
) -> list[AssetStrategyResult]:
    """Evaluate all strategies on one asset by running each evaluator
    across the full history (not analog-selected — raw strategy performance).
    """
    evaluators = build_evaluators(result.candles, result.funding, asset=asset)
    candles = sorted(result.candles.get(asset, []), key=lambda c: c.timestamp_ms)

    if len(candles) < 200:
        return []

    # Step through every 6th candle (daily) starting after warmup
    warmup = 180
    results: list[AssetStrategyResult] = []

    for name, evaluator in evaluators.items():
        pnls: list[float] = []
        for i in range(warmup, len(candles), 6):
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


async def main(lookback_days: int = 730, forward_hours: float = 4.0, top_n: int = 30):
    print("=" * 70)
    print("  MULTI-ASSET STRATEGY ANALYSIS")
    print("=" * 70)

    # --- Backfill all assets ---
    print("\n[1/3] Backfilling all 9 assets...")
    result = await run_backfill(lookback_days=lookback_days)
    print_summary(result)

    assets = sorted(result.candles.keys())
    print(f"\nAssets with candle data: {', '.join(assets)}")

    # --- Evaluate each asset ---
    print(f"\n[2/3] Evaluating 39 strategies x {len(assets)} assets...")
    all_results: list[AssetStrategyResult] = []

    for asset in assets:
        n_candles = len(result.candles.get(asset, []))
        if n_candles < 200:
            print(f"  {asset}: skipped (only {n_candles} candles)")
            continue
        print(f"  {asset}: {n_candles} candles, evaluating...", end="", flush=True)
        asset_results = evaluate_asset(result, asset, forward_hours)
        print(f" {len(asset_results)} strategies with trades")
        all_results.extend(asset_results)

    # --- Rank and display ---
    print(f"\n[3/3] Ranking {len(all_results)} asset-strategy combinations...\n")

    # Filter to strategies that fired at least 10 times
    viable = [r for r in all_results if r.n_trades >= 10]
    viable.sort(key=lambda r: r.mean_return, reverse=True)

    # Top winners
    print(f"  {'='*70}")
    print(f"  TOP {top_n} STRATEGY-ASSET COMBINATIONS (by mean return, N≥10)")
    print(f"  {'='*70}")
    print(f"\n  {'Rank':>4}  {'Asset':>5}  {'Strategy':>28}  {'N':>5}  {'WR':>6}  {'Mean':>8}  {'Total':>8}  {'Worst':>8}")
    print(f"  {'─'*4}  {'─'*5}  {'─'*28}  {'─'*5}  {'─'*6}  {'─'*8}  {'─'*8}  {'─'*8}")

    for rank, r in enumerate(viable[:top_n], 1):
        print(
            f"  {rank:>4}  {r.asset:>5}  {r.strategy:>28}  "
            f"{r.n_trades:>5}  {r.win_rate:>5.1%}  {r.mean_return:>+7.4f}  "
            f"{r.total_pnl:>+7.4f}  {r.worst_return:>+7.4f}"
        )

    # Summary by asset: which assets have the most winning strategies?
    print(f"\n  {'='*70}")
    print("  ASSET SUMMARY — strategies with positive mean return (N≥10)")
    print(f"  {'='*70}")
    print(f"\n  {'Asset':>5}  {'Winners':>8}  {'Total':>6}  {'Best Strategy':>28}  {'Best Mean':>9}")
    print(f"  {'─'*5}  {'─'*8}  {'─'*6}  {'─'*28}  {'─'*9}")

    for asset in assets:
        asset_viable = [r for r in viable if r.asset == asset]
        winners = [r for r in asset_viable if r.mean_return > 0]
        if not asset_viable:
            print(f"  {asset:>5}  {'n/a':>8}  {'n/a':>6}")
            continue
        best = max(asset_viable, key=lambda r: r.mean_return)
        print(
            f"  {asset:>5}  {len(winners):>8}  {len(asset_viable):>6}  "
            f"{best.strategy:>28}  {best.mean_return:>+8.4f}"
        )

    # Summary by strategy: which strategies work across multiple assets?
    print(f"\n  {'='*70}")
    print("  STRATEGY ROBUSTNESS — positive mean on how many assets? (N≥10)")
    print(f"  {'='*70}")

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
    for strat, n_pos, n_tested, avg_mean in robust[:25]:
        if n_pos > 0:
            print(f"  {strat:>28}  {n_pos:>10}  {n_tested:>7}  {avg_mean:>+8.4f}")

    print(f"\n  {'='*70}")
    print(f"  Total combinations tested: {len(all_results)}")
    print(f"  Viable (N≥10): {len(viable)}")
    print(f"  Profitable: {sum(1 for r in viable if r.mean_return > 0)}")
    print(f"  {'='*70}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Multi-Asset Strategy Analysis")
    parser.add_argument("--days", type=int, default=730, help="Lookback days")
    parser.add_argument("--forward", type=float, default=4.0, help="Forward hours")
    parser.add_argument("--top", type=int, default=30, help="Top N to show")
    args = parser.parse_args()

    asyncio.run(main(lookback_days=args.days, forward_hours=args.forward, top_n=args.top))
