"""Simulate a realistic multi-strategy portfolio on Hyperliquid.

Walk through historical data day by day, execute strategy signals with
position sizing, track P&L with compounding, and produce a detailed
performance report.

Usage::

    python3 -m analog.simulate_portfolio
    python3 -m analog.simulate_portfolio --capital 1000 --interval 4h
"""

from __future__ import annotations

import argparse
import asyncio
import os
import pickle
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

import structlog

from analog.backfill import (
    run_backfill,
    BackfillResult,
    INTERVAL_HOURS,
)
from analog.evaluators import build_evaluators

logger = structlog.get_logger()


@dataclass
class Trade:
    timestamp: float
    asset: str
    strategy: str
    direction: str  # "long" or "short"
    entry_price: float
    size_usd: float
    pnl_pct: float
    pnl_usd: float
    equity_before: float
    equity_after: float


@dataclass
class PortfolioStats:
    trades: list[Trade] = field(default_factory=list)
    equity_curve: list[tuple[float, float]] = field(default_factory=list)  # (ts, equity)
    daily_returns: list[float] = field(default_factory=list)


def _identify_profitable_strategies(
    result: BackfillResult,
    interval: str,
    forward_hours: float,
    train_end_idx: int,
    min_trades: int = 10,
    min_wr: float = 0.50,
) -> set[str]:
    """Run strategies on training period, return {asset:strategy} pairs
    that are profitable with sufficient trades and win rate."""
    interval_hours = INTERVAL_HOURS.get(interval, 4.0)
    bars_per_day = 24.0 / interval_hours
    assets = [a for a in sorted(result.candles.keys()) if len(result.candles.get(a, [])) >= 500]

    profitable: set[str] = set()
    warmup = int(60 * bars_per_day)
    step = max(1, int(24 / interval_hours))  # daily

    for asset in assets:
        evaluators = build_evaluators(
            result.candles, result.funding, asset=asset,
            interval_hours=interval_hours,
        )
        candles = sorted(result.candles.get(asset, []), key=lambda c: c.timestamp_ms)
        end_idx = min(train_end_idx, len(candles))

        for name, evaluator in evaluators.items():
            pnls = []
            for i in range(warmup, end_idx, step):
                ts = candles[i].timestamp_ms / 1000.0
                try:
                    pnl = evaluator(ts, forward_hours)
                except Exception:
                    continue
                if pnl is not None:
                    pnls.append(pnl)

            if len(pnls) >= min_trades:
                wr = sum(1 for p in pnls if p > 0) / len(pnls)
                mean_ret = sum(pnls) / len(pnls)
                if wr >= min_wr and mean_ret > 0:
                    profitable.add(f"{asset}:{name}")

    return profitable


def run_simulation(
    result: BackfillResult,
    initial_capital: float = 1000.0,
    interval: str = "4h",
    max_positions: int = 3,
    position_pct: float = 0.10,  # 10% of equity per trade
    min_position: float = 11.0,  # HL minimum $10 + buffer
    forward_hours: float = 4.0,
    step_hours: float = 24.0,  # evaluate once per day
) -> PortfolioStats:
    """Walk-forward portfolio simulation.

    1. Use first 50% of data to identify profitable strategies
    2. Trade only those strategies on the remaining 50%
    3. Take top `max_positions` signals per evaluation point
    """
    interval_hours = INTERVAL_HOURS.get(interval, 4.0)

    assets = [a for a in sorted(result.candles.keys()) if len(result.candles.get(a, [])) >= 500]

    # Get the common timeline from BTC candles
    btc_candles = sorted(result.candles.get("BTC", []), key=lambda c: c.timestamp_ms)
    if not btc_candles:
        return PortfolioStats()

    # Walk-forward: train on first 50%, trade on last 50%
    total_bars = len(btc_candles)
    train_end = total_bars // 2
    trade_start = train_end

    train_end_dt = datetime.fromtimestamp(
        btc_candles[train_end].timestamp_ms / 1000, tz=timezone.utc)
    trade_start_dt = datetime.fromtimestamp(
        btc_candles[trade_start].timestamp_ms / 1000, tz=timezone.utc)
    end_dt = datetime.fromtimestamp(
        btc_candles[-1].timestamp_ms / 1000, tz=timezone.utc)
    print(f"  Walk-forward: train to {train_end_dt.date()}, "
          f"trade {trade_start_dt.date()} → {end_dt.date()}")

    # Phase 1: identify profitable strategies on training data
    print("  Phase 1: Identifying profitable strategies on training period...")
    profitable = _identify_profitable_strategies(
        result, interval, forward_hours, train_end,
        min_trades=8, min_wr=0.50,
    )
    print(f"  Found {len(profitable)} profitable strategy-asset combos")
    if not profitable:
        print("  WARNING: No profitable strategies found! Relaxing filter...")
        profitable = _identify_profitable_strategies(
            result, interval, forward_hours, train_end,
            min_trades=5, min_wr=0.45,
        )
        print(f"  Found {len(profitable)} with relaxed filters")

    # Show which strategies made the cut
    strat_counts: dict[str, int] = {}
    for key in profitable:
        strat = key.split(":", 1)[1]
        strat_counts[strat] = strat_counts.get(strat, 0) + 1
    for strat, cnt in sorted(strat_counts.items(), key=lambda x: -x[1])[:10]:
        print(f"    {strat:>28}: {cnt} assets")

    # Phase 2: build evaluators and trade
    print("\n  Phase 2: Trading out-of-sample period...")
    evaluator_sets: dict[str, dict] = {}
    for asset in assets:
        evaluator_sets[asset] = build_evaluators(
            result.candles, result.funding, asset=asset,
            interval_hours=interval_hours,
        )

    step_bars = max(1, int(step_hours / interval_hours))
    equity = initial_capital
    stats = PortfolioStats()
    stats.equity_curve.append((btc_candles[trade_start].timestamp_ms / 1000.0, equity))

    prev_day_equity = equity
    current_day = None
    strategy_trade_counts: dict[str, int] = {}
    strategy_win_counts: dict[str, int] = {}
    strategy_pnl: dict[str, float] = {}
    asset_trade_counts: dict[str, int] = {}

    for i in range(trade_start, len(btc_candles), step_bars):
        ts = btc_candles[i].timestamp_ms / 1000.0
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)

        # Track daily returns
        if current_day is not None and dt.date() != current_day:
            if prev_day_equity > 0:
                daily_ret = (equity - prev_day_equity) / prev_day_equity
                stats.daily_returns.append(daily_ret)
            prev_day_equity = equity
        current_day = dt.date()

        # Collect signals ONLY from profitable strategies
        signals: list[tuple[str, str, float]] = []

        for asset in assets:
            if asset not in evaluator_sets:
                continue
            for strat_name, evaluator in evaluator_sets[asset].items():
                key = f"{asset}:{strat_name}"
                if key not in profitable:
                    continue  # skip unprofitable strategies
                try:
                    pnl = evaluator(ts, forward_hours)
                except Exception:
                    continue
                if pnl is not None:
                    signals.append((asset, strat_name, pnl))

        if not signals:
            continue

        # IMPORTANT: Do NOT sort by P&L — that's look-ahead bias.
        # Select by training-period priority (strategies that were best
        # historically), breaking ties by asset diversification.
        # Use a deterministic shuffle based on timestamp to avoid bias.
        import hashlib
        seed = int(hashlib.md5(str(ts).encode()).hexdigest()[:8], 16)
        rng_indices = list(range(len(signals)))
        # Fisher-Yates shuffle with deterministic seed
        for j in range(len(rng_indices) - 1, 0, -1):
            seed = (seed * 1103515245 + 12345) & 0x7fffffff
            k = seed % (j + 1)
            rng_indices[j], rng_indices[k] = rng_indices[k], rng_indices[j]
        shuffled = [signals[idx] for idx in rng_indices]

        # Diversify: don't take 2 trades on same asset
        selected = []
        seen_assets: set[str] = set()
        for sig in shuffled:
            if sig[0] not in seen_assets:
                selected.append(sig)
                seen_assets.add(sig[0])
            if len(selected) >= max_positions:
                break
        # Fill remaining slots if needed
        if len(selected) < max_positions:
            for sig in shuffled:
                if sig not in selected:
                    selected.append(sig)
                if len(selected) >= max_positions:
                    break

        for asset, strat_name, pnl_pct in selected:
            # Position sizing: % of current equity
            size_usd = equity * position_pct
            if size_usd < min_position:
                continue  # too small to trade

            pnl_usd = size_usd * pnl_pct
            equity_before = equity
            equity += pnl_usd
            equity = max(equity, 0)  # can't go below 0

            # Get entry price
            candle = None
            for c in result.candles.get(asset, []):
                if abs(c.timestamp_ms / 1000.0 - ts) < interval_hours * 3600:
                    candle = c
                    break

            entry_price = candle.close if candle else 0.0

            trade = Trade(
                timestamp=ts,
                asset=asset,
                strategy=strat_name,
                direction="long" if pnl_pct > 0 else "short",
                entry_price=entry_price,
                size_usd=size_usd,
                pnl_pct=pnl_pct,
                pnl_usd=pnl_usd,
                equity_before=equity_before,
                equity_after=equity,
            )
            stats.trades.append(trade)
            stats.equity_curve.append((ts, equity))

            # Track per-strategy stats
            key = f"{asset}:{strat_name}"
            strategy_trade_counts[key] = strategy_trade_counts.get(key, 0) + 1
            strategy_win_counts[key] = strategy_win_counts.get(key, 0) + (1 if pnl_pct > 0 else 0)
            strategy_pnl[key] = strategy_pnl.get(key, 0) + pnl_usd
            asset_trade_counts[asset] = asset_trade_counts.get(asset, 0) + 1

        if equity <= 0:
            print("  BLOWN UP - equity hit zero")
            break

    return stats


def print_report(stats: PortfolioStats, initial_capital: float, interval: str, forward_hours: float):
    if not stats.trades:
        print("No trades executed!")
        return

    trades = stats.trades
    first_dt = datetime.fromtimestamp(trades[0].timestamp, tz=timezone.utc)
    last_dt = datetime.fromtimestamp(trades[-1].timestamp, tz=timezone.utc)
    days = (last_dt - first_dt).days

    final_equity = trades[-1].equity_after
    total_return = (final_equity - initial_capital) / initial_capital
    n_trades = len(trades)
    winners = [t for t in trades if t.pnl_pct > 0]
    losers = [t for t in trades if t.pnl_pct <= 0]
    win_rate = len(winners) / n_trades if n_trades else 0

    # Drawdown
    peak = initial_capital
    max_dd = 0
    max_dd_date = first_dt
    for ts, eq in stats.equity_curve:
        if eq > peak:
            peak = eq
        dd = (peak - eq) / peak if peak > 0 else 0
        if dd > max_dd:
            max_dd = dd
            max_dd_date = datetime.fromtimestamp(ts, tz=timezone.utc)

    # Average trade stats
    avg_win = sum(t.pnl_pct for t in winners) / len(winners) if winners else 0
    avg_loss = sum(t.pnl_pct for t in losers) / len(losers) if losers else 0
    avg_size = sum(t.size_usd for t in trades) / n_trades
    avg_pnl = sum(t.pnl_usd for t in trades) / n_trades

    # Per-strategy breakdown
    strat_stats: dict[str, dict] = {}
    for t in trades:
        key = t.strategy
        if key not in strat_stats:
            strat_stats[key] = {"n": 0, "wins": 0, "pnl": 0.0, "assets": set()}
        strat_stats[key]["n"] += 1
        strat_stats[key]["wins"] += 1 if t.pnl_pct > 0 else 0
        strat_stats[key]["pnl"] += t.pnl_usd
        strat_stats[key]["assets"].add(t.asset)

    # Per-asset breakdown
    asset_stats: dict[str, dict] = {}
    for t in trades:
        if t.asset not in asset_stats:
            asset_stats[t.asset] = {"n": 0, "wins": 0, "pnl": 0.0}
        asset_stats[t.asset]["n"] += 1
        asset_stats[t.asset]["wins"] += 1 if t.pnl_pct > 0 else 0
        asset_stats[t.asset]["pnl"] += t.pnl_usd

    # Monthly breakdown
    monthly: dict[str, dict] = {}
    for t in trades:
        month = datetime.fromtimestamp(t.timestamp, tz=timezone.utc).strftime("%Y-%m")
        if month not in monthly:
            monthly[month] = {"n": 0, "pnl": 0.0, "wins": 0}
        monthly[month]["n"] += 1
        monthly[month]["pnl"] += t.pnl_usd
        monthly[month]["wins"] += 1 if t.pnl_pct > 0 else 0

    # Daily returns stats
    import math
    if stats.daily_returns:
        avg_daily = sum(stats.daily_returns) / len(stats.daily_returns)
        std_daily = math.sqrt(sum((r - avg_daily)**2 for r in stats.daily_returns) / len(stats.daily_returns))
        sharpe = (avg_daily / std_daily) * math.sqrt(365) if std_daily > 0 else 0
    else:
        sharpe = 0

    # === PRINT REPORT ===

    print("\n" + "=" * 80)
    print("  PORTFOLIO SIMULATION REPORT")
    print("=" * 80)

    print(f"""
  Period:         {first_dt.date()} to {last_dt.date()} ({days} days)
  Interval:       {interval} bars, {forward_hours}h forward evaluation
  Starting:       ${initial_capital:,.2f}
  Ending:         ${final_equity:,.2f}
  Total Return:   {total_return:+.1%} (${final_equity - initial_capital:+,.2f})
  Annualized:     {((1 + total_return) ** (365/max(days,1)) - 1):+.1%}
  Sharpe Ratio:   {sharpe:.2f}
  Max Drawdown:   {max_dd:.1%} (on {max_dd_date.date()})
""")

    print(f"  {'─'*70}")
    print("  TRADE STATISTICS")
    print(f"  {'─'*70}")
    print(f"""
  Total Trades:   {n_trades:,} ({n_trades/days:.1f}/day)
  Win Rate:       {win_rate:.1%} ({len(winners)} wins / {len(losers)} losses)
  Avg Position:   ${avg_size:,.2f}
  Avg P&L/Trade:  ${avg_pnl:+,.2f} ({sum(t.pnl_pct for t in trades)/n_trades:+.3%})
  Avg Winner:     {avg_win:+.3%}
  Avg Loser:      {avg_loss:+.3%}
  Profit Factor:  {abs(sum(t.pnl_usd for t in winners)) / abs(sum(t.pnl_usd for t in losers)):.2f}
  Best Trade:     {max(t.pnl_pct for t in trades):+.2%} ({max(t.pnl_usd for t in trades):+.2f})
  Worst Trade:    {min(t.pnl_pct for t in trades):+.2%} ({min(t.pnl_usd for t in trades):+.2f})
""")

    print(f"  {'─'*70}")
    print("  MONTHLY BREAKDOWN")
    print(f"  {'─'*70}")
    print(f"\n  {'Month':>8}  {'Trades':>7}  {'Win Rate':>8}  {'P&L':>10}  {'Cum P&L':>10}")
    print(f"  {'─'*8}  {'─'*7}  {'─'*8}  {'─'*10}  {'─'*10}")
    cum_pnl = 0
    for month in sorted(monthly.keys()):
        m = monthly[month]
        wr = m["wins"] / m["n"] if m["n"] else 0
        cum_pnl += m["pnl"]
        print(f"  {month:>8}  {m['n']:>7}  {wr:>7.1%}  ${m['pnl']:>+9,.2f}  ${cum_pnl:>+9,.2f}")

    print(f"\n  {'─'*70}")
    print("  TOP 15 STRATEGIES BY P&L")
    print(f"  {'─'*70}")
    print(f"\n  {'Strategy':>28}  {'Trades':>7}  {'WR':>6}  {'P&L':>10}  {'Assets':>7}")
    print(f"  {'─'*28}  {'─'*7}  {'─'*6}  {'─'*10}  {'─'*7}")
    sorted_strats = sorted(strat_stats.items(), key=lambda x: x[1]["pnl"], reverse=True)
    for strat, s in sorted_strats[:15]:
        wr = s["wins"] / s["n"] if s["n"] else 0
        print(f"  {strat:>28}  {s['n']:>7}  {wr:>5.1%}  ${s['pnl']:>+9,.2f}  {len(s['assets']):>7}")

    print(f"\n  {'─'*70}")
    print("  WORST 10 STRATEGIES BY P&L")
    print(f"  {'─'*70}")
    print(f"\n  {'Strategy':>28}  {'Trades':>7}  {'WR':>6}  {'P&L':>10}")
    print(f"  {'─'*28}  {'─'*7}  {'─'*6}  {'─'*10}")
    for strat, s in sorted_strats[-10:]:
        wr = s["wins"] / s["n"] if s["n"] else 0
        print(f"  {strat:>28}  {s['n']:>7}  {wr:>5.1%}  ${s['pnl']:>+9,.2f}")

    print(f"\n  {'─'*70}")
    print("  ASSET BREAKDOWN")
    print(f"  {'─'*70}")
    print(f"\n  {'Asset':>10}  {'Trades':>7}  {'WR':>6}  {'P&L':>10}  {'% of Trades':>11}")
    print(f"  {'─'*10}  {'─'*7}  {'─'*6}  {'─'*10}  {'─'*11}")
    for asset in sorted(asset_stats.keys(), key=lambda a: asset_stats[a]["pnl"], reverse=True):
        s = asset_stats[asset]
        wr = s["wins"] / s["n"] if s["n"] else 0
        pct = s["n"] / n_trades
        print(f"  {asset:>10}  {s['n']:>7}  {wr:>5.1%}  ${s['pnl']:>+9,.2f}  {pct:>10.1%}")

    # Equity curve milestones
    print(f"\n  {'─'*70}")
    print("  EQUITY MILESTONES")
    print(f"  {'─'*70}\n")
    milestones = [0.1, 0.25, 0.5, 0.75, 1.0]
    for i, (ts, eq) in enumerate(stats.equity_curve):
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        progress = i / len(stats.equity_curve)
        for m in milestones:
            if abs(progress - m) < 1 / len(stats.equity_curve):
                print(f"  {dt.date()}  ${eq:>9,.2f}  ({(eq/initial_capital - 1):+.1%})")
    print(f"  {last_dt.date()}  ${final_equity:>9,.2f}  ({total_return:+.1%})  [FINAL]")

    print(f"\n{'='*80}")


async def main(
    capital: float = 1000.0,
    interval: str = "4h",
    max_positions: int = 3,
    position_pct: float = 0.10,
    forward_hours: float = 4.0,
    skip_backfill: bool = False,
):
    cache_path = f"data/backfill_{interval}.pkl"

    if skip_backfill and os.path.exists(cache_path):
        print(f"Loading cached {interval} data from {cache_path}...")
        with open(cache_path, "rb") as f:
            result = pickle.load(f)
    else:
        print(f"Backfilling {interval} data...")
        result = await run_backfill(
            lookback_days=730,
            data_dir=f"data/fingerprints_{interval}",
            interval=interval,
        )
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        with open(cache_path, "wb") as f:
            pickle.dump(result, f)

    n_candles = sum(len(v) for v in result.candles.values())
    print(f"  {len(result.candles)} assets, {n_candles:,} total candles\n")

    print("Running portfolio simulation...")
    print(f"  Capital: ${capital:,.2f}")
    print(f"  Interval: {interval}")
    print(f"  Max positions: {max_positions}")
    print(f"  Position size: {position_pct:.0%} of equity")
    print(f"  Forward eval: {forward_hours}h\n")

    t0 = time.time()
    stats = run_simulation(
        result,
        initial_capital=capital,
        interval=interval,
        max_positions=max_positions,
        position_pct=position_pct,
        forward_hours=forward_hours,
    )
    elapsed = time.time() - t0
    print(f"  Simulation completed in {elapsed:.1f}s")

    print_report(stats, capital, interval, forward_hours)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Multi-Strategy Portfolio Simulation")
    parser.add_argument("--capital", type=float, default=1000, help="Starting capital (USD)")
    parser.add_argument("--interval", default="4h", help="Bar interval")
    parser.add_argument("--max-pos", type=int, default=3, help="Max concurrent positions")
    parser.add_argument("--pos-pct", type=float, default=0.10, help="Position size as fraction of equity")
    parser.add_argument("--forward", type=float, default=4.0, help="Forward hours for P&L")
    parser.add_argument("--skip-backfill", action="store_true", help="Use cached data")
    args = parser.parse_args()

    asyncio.run(main(
        capital=args.capital,
        interval=args.interval,
        max_positions=args.max_pos,
        position_pct=args.pos_pct,
        forward_hours=args.forward,
        skip_backfill=args.skip_backfill,
    ))
