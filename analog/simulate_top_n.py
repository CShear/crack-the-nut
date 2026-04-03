"""Simulate concentrated portfolios of the top N strategies.

Walk-forward: rank strategies on Year 1, trade top N on Year 2.

Usage::

    python3 -m analog.simulate_top_n
"""

from __future__ import annotations

import asyncio
import math
import os
import pickle
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from analog.backfill import run_backfill, BackfillResult, INTERVAL_HOURS
from analog.evaluators import build_evaluators


@dataclass
class TrainResult:
    """Training-period performance for one strategy-asset combo."""
    asset: str
    strategy: str
    key: str  # "asset:strategy"
    n_trades: int
    win_rate: float
    mean_return: float
    total_pnl: float
    worst_trade: float
    sharpe: float  # mean / std


@dataclass
class Trade:
    timestamp: float
    asset: str
    strategy: str
    size_usd: float
    pnl_pct: float
    pnl_usd: float


@dataclass
class SimResult:
    label: str
    n_strategies: int
    strategy_list: list[str]
    trades: list[Trade] = field(default_factory=list)
    equity_curve: list[tuple[float, float]] = field(default_factory=list)


def rank_strategies(
    result: BackfillResult,
    interval: str,
    forward_hours: float,
    end_idx: int,
    min_trades: int = 15,
) -> list[TrainResult]:
    """Evaluate all strategy-asset combos on training data, return ranked."""
    interval_hours = INTERVAL_HOURS.get(interval, 4.0)
    bars_per_day = 24.0 / interval_hours
    assets = [a for a in sorted(result.candles.keys())
              if len(result.candles.get(a, [])) >= 500]

    warmup = int(60 * bars_per_day)
    step = max(1, int(24 / interval_hours))  # daily

    all_results: list[TrainResult] = []

    for asset in assets:
        evaluators = build_evaluators(
            result.candles, result.funding, asset=asset,
            interval_hours=interval_hours,
        )
        candles = sorted(result.candles.get(asset, []), key=lambda c: c.timestamp_ms)
        cap = min(end_idx, len(candles))

        for name, evaluator in evaluators.items():
            pnls = []
            for i in range(warmup, cap, step):
                ts = candles[i].timestamp_ms / 1000.0
                try:
                    pnl = evaluator(ts, forward_hours)
                except Exception:
                    continue
                if pnl is not None:
                    pnls.append(pnl)

            if len(pnls) < min_trades:
                continue

            n = len(pnls)
            wins = sum(1 for p in pnls if p > 0)
            wr = wins / n
            mean_ret = sum(pnls) / n
            total = sum(pnls)
            worst = min(pnls)
            std = math.sqrt(sum((p - mean_ret)**2 for p in pnls) / n) if n > 1 else 1.0
            sharpe = mean_ret / std if std > 0 else 0

            all_results.append(TrainResult(
                asset=asset, strategy=name, key=f"{asset}:{name}",
                n_trades=n, win_rate=wr, mean_return=mean_ret,
                total_pnl=total, worst_trade=worst, sharpe=sharpe,
            ))

    # Rank by Sharpe (risk-adjusted return) — better than raw mean
    all_results.sort(key=lambda r: r.sharpe, reverse=True)
    return all_results


def simulate_portfolio(
    result: BackfillResult,
    allowed_keys: set[str],
    interval: str,
    forward_hours: float,
    trade_start_idx: int,
    initial_capital: float = 1000.0,
    max_positions: int = 3,
    position_pct: float = 0.15,
    step_hours: float = 24.0,
) -> SimResult:
    """Trade only the allowed strategy-asset combos, out-of-sample."""
    interval_hours = INTERVAL_HOURS.get(interval, 4.0)

    assets = [a for a in sorted(result.candles.keys())
              if len(result.candles.get(a, [])) >= 500]

    evaluator_sets: dict[str, dict] = {}
    for asset in assets:
        evaluator_sets[asset] = build_evaluators(
            result.candles, result.funding, asset=asset,
            interval_hours=interval_hours,
        )

    btc_candles = sorted(result.candles["BTC"], key=lambda c: c.timestamp_ms)
    step_bars = max(1, int(step_hours / interval_hours))

    equity = initial_capital
    sim = SimResult(label="", n_strategies=len(allowed_keys), strategy_list=sorted(allowed_keys))
    sim.equity_curve.append((btc_candles[trade_start_idx].timestamp_ms / 1000.0, equity))

    for i in range(trade_start_idx, len(btc_candles), step_bars):
        ts = btc_candles[i].timestamp_ms / 1000.0

        # Collect signals from allowed strategies
        signals: list[tuple[str, str, float]] = []
        for asset in assets:
            if asset not in evaluator_sets:
                continue
            for strat_name, evaluator in evaluator_sets[asset].items():
                key = f"{asset}:{strat_name}"
                if key not in allowed_keys:
                    continue
                try:
                    pnl = evaluator(ts, forward_hours)
                except Exception:
                    continue
                if pnl is not None:
                    signals.append((asset, strat_name, pnl))

        if not signals:
            continue

        # Deterministic shuffle (no look-ahead bias)
        import hashlib
        seed = int(hashlib.md5(str(ts).encode()).hexdigest()[:8], 16)
        indices = list(range(len(signals)))
        for j in range(len(indices) - 1, 0, -1):
            seed = (seed * 1103515245 + 12345) & 0x7fffffff
            k = seed % (j + 1)
            indices[j], indices[k] = indices[k], indices[j]
        shuffled = [signals[idx] for idx in indices]

        # Diversify across assets
        selected = []
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

        for asset, strat_name, pnl_pct in selected:
            size_usd = equity * position_pct
            if size_usd < 11:
                continue
            pnl_usd = size_usd * pnl_pct
            equity += pnl_usd
            equity = max(equity, 0)

            sim.trades.append(Trade(
                timestamp=ts, asset=asset, strategy=strat_name,
                size_usd=size_usd, pnl_pct=pnl_pct, pnl_usd=pnl_usd,
            ))
            sim.equity_curve.append((ts, equity))

        if equity <= 0:
            break

    return sim


def print_sim_report(sim: SimResult, initial: float):
    trades = sim.trades
    if not trades:
        print(f"\n  {sim.label}: NO TRADES")
        return

    final = sim.equity_curve[-1][1]
    first_dt = datetime.fromtimestamp(trades[0].timestamp, tz=timezone.utc)
    last_dt = datetime.fromtimestamp(trades[-1].timestamp, tz=timezone.utc)
    days = max(1, (last_dt - first_dt).days)
    total_ret = (final - initial) / initial
    ann_ret = (1 + total_ret) ** (365 / days) - 1

    n = len(trades)
    wins = sum(1 for t in trades if t.pnl_pct > 0)
    wr = wins / n
    avg_win = sum(t.pnl_pct for t in trades if t.pnl_pct > 0) / wins if wins else 0
    avg_loss = sum(t.pnl_pct for t in trades if t.pnl_pct <= 0) / (n - wins) if (n - wins) else 0

    # Drawdown
    peak = initial
    max_dd = 0
    for _, eq in sim.equity_curve:
        peak = max(peak, eq)
        dd = (peak - eq) / peak if peak > 0 else 0
        max_dd = max(max_dd, dd)

    # Monthly
    monthly: dict[str, list[float]] = {}
    for t in trades:
        m = datetime.fromtimestamp(t.timestamp, tz=timezone.utc).strftime("%Y-%m")
        monthly.setdefault(m, []).append(t.pnl_usd)

    # Strategy breakdown
    strat_pnl: dict[str, float] = {}
    strat_n: dict[str, int] = {}
    strat_wins: dict[str, int] = {}
    for t in trades:
        strat_pnl[t.strategy] = strat_pnl.get(t.strategy, 0) + t.pnl_usd
        strat_n[t.strategy] = strat_n.get(t.strategy, 0) + 1
        strat_wins[t.strategy] = strat_wins.get(t.strategy, 0) + (1 if t.pnl_pct > 0 else 0)

    print(f"\n  {'='*72}")
    print(f"  {sim.label}")
    print(f"  {'='*72}")
    print(f"  Period:       {first_dt.date()} → {last_dt.date()} ({days} days)")
    print(f"  Strategies:   {sim.n_strategies} combos allowed")
    print(f"  Starting:     ${initial:,.2f}")
    print(f"  Ending:       ${final:,.2f}")
    print(f"  Total Return: {total_ret:+.1%} (${final - initial:+,.2f})")
    print(f"  Annualized:   {ann_ret:+.1%}")
    print(f"  Max Drawdown: {max_dd:.1%}")
    print(f"  Trades:       {n:,} ({n/days:.1f}/day)")
    print(f"  Win Rate:     {wr:.1%}")
    print(f"  Avg Winner:   {avg_win:+.3%}")
    print(f"  Avg Loser:    {avg_loss:+.3%}")

    win_total = sum(t.pnl_usd for t in trades if t.pnl_pct > 0)
    loss_total = abs(sum(t.pnl_usd for t in trades if t.pnl_pct <= 0))
    pf = win_total / loss_total if loss_total > 0 else float('inf')
    print(f"  Profit Factor:{pf:>6.2f}")

    # Monthly
    print(f"\n  {'Month':>8}  {'Trades':>6}  {'WR':>5}  {'P&L':>10}  {'Cum':>10}")
    print(f"  {'─'*8}  {'─'*6}  {'─'*5}  {'─'*10}  {'─'*10}")
    cum = 0
    for m in sorted(monthly.keys()):
        pnls = monthly[m]
        w = sum(1 for p in pnls if p > 0)
        cum += sum(pnls)
        print(f"  {m:>8}  {len(pnls):>6}  {w/len(pnls):>4.0%}  ${sum(pnls):>+9,.2f}  ${cum:>+9,.2f}")

    # Strategy breakdown
    print(f"\n  {'Strategy':>28}  {'N':>5}  {'WR':>5}  {'P&L':>10}")
    print(f"  {'─'*28}  {'─'*5}  {'─'*5}  {'─'*10}")
    for s in sorted(strat_pnl.keys(), key=lambda k: strat_pnl[k], reverse=True):
        sw = strat_wins.get(s, 0) / strat_n[s] if strat_n[s] else 0
        print(f"  {s:>28}  {strat_n[s]:>5}  {sw:>4.0%}  ${strat_pnl[s]:>+9,.2f}")


async def main():
    cache_path = "data/backfill_4h.pkl"
    interval = "4h"
    forward_hours = 4.0

    if os.path.exists(cache_path):
        print("Loading cached 4h data...")
        with open(cache_path, "rb") as f:
            result = pickle.load(f)
    else:
        print("Backfilling 4h data (730 days)...")
        result = await run_backfill(lookback_days=730, interval="4h")
        with open(cache_path, "wb") as f:
            pickle.dump(result, f)

    btc_candles = sorted(result.candles["BTC"], key=lambda c: c.timestamp_ms)
    total = len(btc_candles)
    mid = total // 2

    train_end_dt = datetime.fromtimestamp(btc_candles[mid].timestamp_ms / 1000, tz=timezone.utc)
    test_start_dt = train_end_dt
    test_end_dt = datetime.fromtimestamp(btc_candles[-1].timestamp_ms / 1000, tz=timezone.utc)

    print(f"\n{'='*72}")
    print("  CONCENTRATED PORTFOLIO ANALYSIS")
    print(f"  Train: Apr 2024 → {train_end_dt.date()}")
    print(f"  Trade: {test_start_dt.date()} → {test_end_dt.date()} "
          f"({(test_end_dt - test_start_dt).days} days)")
    print(f"{'='*72}")

    # Phase 1: Rank all strategies on training period
    print("\n  Ranking strategies on training period...")
    t0 = time.time()
    ranked = rank_strategies(result, interval, forward_hours, mid, min_trades=10)
    print(f"  Ranked {len(ranked)} viable strategy-asset combos in {time.time()-t0:.1f}s")

    # Show the full ranking
    print(f"\n  {'Rank':>4}  {'Sharpe':>7}  {'Mean':>7}  {'WR':>5}  {'N':>4}  {'Worst':>7}  {'Asset:Strategy'}")
    print(f"  {'─'*4}  {'─'*7}  {'─'*7}  {'─'*5}  {'─'*4}  {'─'*7}  {'─'*40}")

    profitable = [r for r in ranked if r.mean_return > 0 and r.win_rate >= 0.45]
    for i, r in enumerate(profitable[:30], 1):
        print(f"  {i:>4}  {r.sharpe:>+6.3f}  {r.mean_return:>+6.3%}  {r.win_rate:>4.0%}  "
              f"{r.n_trades:>4}  {r.worst_trade:>+6.3%}  {r.key}")

    total_profitable = len(profitable)
    print(f"\n  Total profitable combos (mean>0, WR>=45%): {total_profitable}")

    # Phase 2: Simulate top 5, top 10, top 20
    initial = 1000.0

    for top_n in [5, 10, 20]:
        selected = profitable[:top_n]
        keys = {r.key for r in selected}

        strat_names = set()
        asset_names = set()
        for r in selected:
            strat_names.add(r.strategy)
            asset_names.add(r.asset)

        print(f"\n  Top {top_n}: {len(strat_names)} unique strategies, "
              f"{len(asset_names)} assets")

        sim = simulate_portfolio(
            result, keys, interval, forward_hours,
            trade_start_idx=mid,
            initial_capital=initial,
            max_positions=min(3, top_n),
            position_pct=min(0.30, 1.0 / max(1, top_n // 3)),
            step_hours=24.0,
        )
        sim.label = f"TOP {top_n} STRATEGIES — ${initial:,.0f} starting capital"
        print_sim_report(sim, initial)

    # Also run: just the single best strategy
    if profitable:
        best = profitable[0]
        print(f"\n\n  {'='*72}")
        print(f"  BONUS: SINGLE BEST STRATEGY — {best.key}")
        print(f"  Training Sharpe: {best.sharpe:+.3f}, WR: {best.win_rate:.0%}, "
              f"Mean: {best.mean_return:+.3%}")
        print(f"  {'='*72}")

        sim = simulate_portfolio(
            result, {best.key}, interval, forward_hours,
            trade_start_idx=mid,
            initial_capital=initial,
            max_positions=1,
            position_pct=0.20,
            step_hours=24.0,
        )
        sim.label = f"SINGLE BEST: {best.key}"
        print_sim_report(sim, initial)


if __name__ == "__main__":
    asyncio.run(main())
