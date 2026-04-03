"""Portfolio simulation with liquidity-aware position sizing.

Position size scales with asset liquidity: liquid assets get full
allocations, illiquid ones get capped proportionally. This allows
including lower-liquidity assets for signal diversity without
taking on slippage risk.

Usage::

    python3 -m analog.simulate_liquidity
"""

from __future__ import annotations

import asyncio
import math
import os
import pickle
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from analog.backfill import (
    run_backfill,
    BackfillResult,
    INTERVAL_HOURS,
    LIQUIDITY_MAX_PCT,
)
from analog.evaluators import build_evaluators, HistoricalData
from analog.simulate_top_n import rank_strategies


@dataclass
class Trade:
    timestamp: float
    asset: str
    strategy: str
    direction: float
    size_usd: float
    liq_cap: float  # max % from liquidity tier
    pnl_pct: float
    pnl_usd: float
    exit_reason: str
    hold_hours: float


@dataclass
class SimResult:
    label: str
    trades: list[Trade] = field(default_factory=list)
    equity_curve: list[tuple[float, float]] = field(default_factory=list)


def run_liquidity_sim(
    result: BackfillResult,
    allowed_keys: set[str],
    interval: str = "4h",
    forward_hours: float = 4.0,
    trade_start_idx: int = 0,
    initial_capital: float = 100_000.0,
    base_position_pct: float = 0.20,
    max_total_exposure: float = 0.80,  # max 80% of portfolio deployed
    max_positions: int = 6,
    step_hours: float = 24.0,
    use_exit: bool = True,
    stop_loss: float = 0.03,
    take_profit: float = 0.05,
    max_hold_hours: float = 48.0,
) -> SimResult:
    """Simulate with liquidity-aware sizing.

    For each signal, position size = min(base_position_pct, LIQUIDITY_MAX_PCT[asset]).
    Total exposure capped at max_total_exposure of equity.
    """
    interval_hours = INTERVAL_HOURS.get(interval, 4.0)
    COMMISSION = 0.0006

    assets = [a for a in sorted(result.candles.keys())
              if len(result.candles.get(a, [])) >= 500]

    eval_sets: dict[str, dict] = {}
    data_sets: dict[str, HistoricalData] = {}
    for asset in assets:
        eval_sets[asset] = build_evaluators(
            result.candles, result.funding, asset=asset,
            interval_hours=interval_hours,
        )
        data_sets[asset] = HistoricalData(
            result.candles, result.funding,
            primary_asset=asset, interval_hours=interval_hours,
        )

    btc_candles = sorted(result.candles["BTC"], key=lambda c: c.timestamp_ms)
    step_bars = max(1, int(step_hours / interval_hours))

    equity = initial_capital
    sim = SimResult(label="")
    sim.equity_curve.append((btc_candles[trade_start_idx].timestamp_ms / 1000.0, equity))

    for i in range(trade_start_idx, len(btc_candles), step_bars):
        ts = btc_candles[i].timestamp_ms / 1000.0

        # Collect signals from allowed strategies
        signals: list[tuple[str, str, float, float]] = []  # (asset, strat, direction, liq_cap)

        for asset in assets:
            if asset not in eval_sets:
                continue

            liq_cap = LIQUIDITY_MAX_PCT.get(asset, 0.05)  # default 5% for unknowns

            for strat_name, evaluator in eval_sets[asset].items():
                key = f"{asset}:{strat_name}"
                if key not in allowed_keys:
                    continue
                try:
                    pnl = evaluator(ts, forward_hours)
                except Exception:
                    continue
                if pnl is None:
                    continue

                # Determine direction
                fwd = data_sets[asset].forward_return(asset, ts, forward_hours)
                if fwd is not None and abs(fwd) > 0.0001:
                    adjusted = pnl + COMMISSION
                    direction = 1.0 if (adjusted * fwd) > 0 else -1.0
                else:
                    direction = 1.0 if pnl > 0 else -1.0

                signals.append((asset, strat_name, direction, liq_cap))

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

        # Select: diversify across assets, respect exposure cap
        selected = []
        seen_assets: set[str] = set()
        total_exposure = 0.0

        for asset, strat, direction, liq_cap in shuffled:
            if asset in seen_assets:
                continue

            pos_pct = min(base_position_pct, liq_cap)
            if total_exposure + pos_pct > max_total_exposure:
                # Can we fit a smaller position?
                remaining = max_total_exposure - total_exposure
                if remaining < 0.03:  # less than 3%, not worth it
                    break
                pos_pct = min(pos_pct, remaining)

            selected.append((asset, strat, direction, pos_pct))
            seen_assets.add(asset)
            total_exposure += pos_pct

            if len(selected) >= max_positions:
                break

        for asset, strat, direction, pos_pct in selected:
            size_usd = equity * pos_pct
            if size_usd < 11:
                continue

            liq_cap = LIQUIDITY_MAX_PCT.get(asset, 0.05)

            if use_exit:
                data = data_sets.get(asset)
                if data is None:
                    continue
                exit_result = data.simulate_exit(
                    asset, ts, direction,
                    stop_loss=stop_loss,
                    take_profit=take_profit,
                    max_hold_hours=max_hold_hours,
                )
                if exit_result is None:
                    continue
                pnl_pct, hold_hours, exit_reason = exit_result
            else:
                fwd = data_sets[asset].forward_return(asset, ts, forward_hours)
                if fwd is None:
                    continue
                pnl_pct = direction * fwd - COMMISSION
                hold_hours = forward_hours
                exit_reason = "time_exit"

            pnl_usd = size_usd * pnl_pct
            equity += pnl_usd
            equity = max(equity, 0)

            sim.trades.append(Trade(
                timestamp=ts, asset=asset, strategy=strat,
                direction=direction, size_usd=size_usd, liq_cap=liq_cap,
                pnl_pct=pnl_pct, pnl_usd=pnl_usd,
                exit_reason=exit_reason, hold_hours=hold_hours,
            ))
            sim.equity_curve.append((ts, equity))

        if equity <= 0:
            break

    return sim


def print_report(sim: SimResult, initial: float):
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
    win_total = sum(t.pnl_usd for t in trades if t.pnl_pct > 0)
    loss_total = abs(sum(t.pnl_usd for t in trades if t.pnl_pct <= 0))
    pf = win_total / loss_total if loss_total > 0 else 999

    peak = initial
    max_dd = 0
    for _, eq in sim.equity_curve:
        peak = max(peak, eq)
        dd = (peak - eq) / peak if peak > 0 else 0
        max_dd = max(max_dd, dd)

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
    avg_d = sum(daily_rets) / len(daily_rets) if daily_rets else 0
    std_d = math.sqrt(sum((r - avg_d)**2 for r in daily_rets) / len(daily_rets)) if daily_rets else 1
    sharpe = (avg_d / std_d) * math.sqrt(365) if std_d > 0 else 0

    avg_size = sum(t.size_usd for t in trades) / n

    print(f"\n  {'='*75}")
    print(f"  {sim.label}")
    print(f"  {'='*75}")
    print(f"  Period:       {first_dt.date()} → {last_dt.date()} ({days} days)")
    print(f"  Starting:     ${initial:>10,.0f}")
    print(f"  Ending:       ${final:>10,.0f}")
    print(f"  Total Return: {total_ret:>+8.1%} (${final - initial:>+,.0f})")
    print(f"  Annualized:   {ann_ret:>+8.1%}")
    print(f"  Sharpe:       {sharpe:>+8.2f}")
    print(f"  Max Drawdown: {max_dd:>8.1%}")
    print(f"  Trades:       {n} ({n/days:.1f}/day)")
    print(f"  Win Rate:     {wr:>8.0%}")
    print(f"  Profit Factor:{pf:>8.2f}")
    print(f"  Avg Position: ${avg_size:>8,.0f}")

    # Exit breakdown
    exit_counts: dict[str, int] = {}
    for t in trades:
        exit_counts[t.exit_reason] = exit_counts.get(t.exit_reason, 0) + 1
    print(f"  Exit Reasons: {exit_counts}")

    # Monthly
    monthly: dict[str, list[float]] = {}
    for t in trades:
        m = datetime.fromtimestamp(t.timestamp, tz=timezone.utc).strftime("%Y-%m")
        monthly.setdefault(m, []).append(t.pnl_usd)

    print(f"\n  {'Month':>8}  {'N':>4}  {'WR':>4}  {'P&L':>12}  {'Cum':>12}")
    print(f"  {'─'*8}  {'─'*4}  {'─'*4}  {'─'*12}  {'─'*12}")
    cum = 0
    for m in sorted(monthly.keys()):
        pnls = monthly[m]
        w = sum(1 for p in pnls if p > 0)
        cum += sum(pnls)
        print(f"  {m:>8}  {len(pnls):>4}  {w/len(pnls):>3.0%}  "
              f"${sum(pnls):>+11,.0f}  ${cum:>+11,.0f}")

    # Per-asset with liquidity info
    asset_stats: dict[str, dict] = {}
    for t in trades:
        if t.asset not in asset_stats:
            asset_stats[t.asset] = {"n": 0, "wins": 0, "pnl": 0.0,
                                     "total_size": 0.0, "liq_cap": t.liq_cap}
        asset_stats[t.asset]["n"] += 1
        asset_stats[t.asset]["wins"] += 1 if t.pnl_pct > 0 else 0
        asset_stats[t.asset]["pnl"] += t.pnl_usd
        asset_stats[t.asset]["total_size"] += t.size_usd

    print(f"\n  {'Asset':>10}  {'N':>4}  {'WR':>4}  {'P&L':>12}  {'AvgSize':>9}  {'LiqCap':>6}")
    print(f"  {'─'*10}  {'─'*4}  {'─'*4}  {'─'*12}  {'─'*9}  {'─'*6}")
    for a in sorted(asset_stats.keys(), key=lambda x: asset_stats[x]["pnl"], reverse=True):
        s = asset_stats[a]
        wr = s["wins"] / s["n"]
        avg = s["total_size"] / s["n"]
        print(f"  {a:>10}  {s['n']:>4}  {wr:>3.0%}  ${s['pnl']:>+11,.0f}  "
              f"${avg:>8,.0f}  {s['liq_cap']:>5.0%}")

    # Per-strategy
    strat_stats: dict[str, dict] = {}
    for t in trades:
        if t.strategy not in strat_stats:
            strat_stats[t.strategy] = {"n": 0, "wins": 0, "pnl": 0.0}
        strat_stats[t.strategy]["n"] += 1
        strat_stats[t.strategy]["wins"] += 1 if t.pnl_pct > 0 else 0
        strat_stats[t.strategy]["pnl"] += t.pnl_usd

    print(f"\n  {'Strategy':>28}  {'N':>4}  {'WR':>4}  {'P&L':>12}")
    print(f"  {'─'*28}  {'─'*4}  {'─'*4}  {'─'*12}")
    for s in sorted(strat_stats.keys(), key=lambda x: strat_stats[x]["pnl"], reverse=True):
        st = strat_stats[s]
        wr = st["wins"] / st["n"]
        print(f"  {s:>28}  {st['n']:>4}  {wr:>3.0%}  ${st['pnl']:>+11,.0f}")


async def main():
    cache_path = "data/backfill_4h_28.pkl"

    if os.path.exists(cache_path):
        print("Loading cached 28-asset 4h data...")
        with open(cache_path, "rb") as f:
            result = pickle.load(f)
    else:
        print("Backfilling 28 assets at 4h (730 days)...")
        result = await run_backfill(lookback_days=730, interval="4h")
        os.makedirs("data", exist_ok=True)
        with open(cache_path, "wb") as f:
            pickle.dump(result, f)

    n_assets = len([a for a in result.candles if result.candles[a]])
    n_candles = sum(len(v) for v in result.candles.values())
    print(f"  {n_assets} assets, {n_candles:,} total candles\n")

    btc_candles = sorted(result.candles["BTC"], key=lambda c: c.timestamp_ms)
    total = len(btc_candles)
    mid = total // 2

    train_end = datetime.fromtimestamp(btc_candles[mid].timestamp_ms / 1000, tz=timezone.utc)
    test_end = datetime.fromtimestamp(btc_candles[-1].timestamp_ms / 1000, tz=timezone.utc)

    print(f"{'='*75}")
    print("  LIQUIDITY-AWARE PORTFOLIO SIMULATION — 28 ASSETS")
    print(f"  Train: Apr 2024 → {train_end.date()}")
    print(f"  Trade: {train_end.date()} → {test_end.date()}")
    print(f"{'='*75}")

    # Rank on training
    print("\n  Ranking strategies on training period...")
    t0 = time.time()
    ranked = rank_strategies(result, "4h", 4.0, mid, min_trades=10)
    profitable = [r for r in ranked if r.mean_return > 0 and r.win_rate >= 0.45]
    print(f"  {len(ranked)} viable, {len(profitable)} profitable in {time.time()-t0:.0f}s")

    # Show top 30
    print(f"\n  {'Rank':>4}  {'Sharpe':>7}  {'Mean':>7}  {'WR':>4}  {'N':>4}  "
          f"{'LiqCap':>6}  {'Key'}")
    print(f"  {'─'*4}  {'─'*7}  {'─'*7}  {'─'*4}  {'─'*4}  {'─'*6}  {'─'*35}")
    for i, r in enumerate(profitable[:30], 1):
        asset = r.key.split(":")[0]
        liq = LIQUIDITY_MAX_PCT.get(asset, 0.05)
        print(f"  {i:>4}  {r.sharpe:>+6.3f}  {r.mean_return:>+6.3%}  {r.win_rate:>3.0%}  "
              f"{r.n_trades:>4}  {liq:>5.0%}  {r.key}")

    # Test: Top 11 from 13 assets vs Top 11 from 28 assets
    # Also test larger portfolios now that we have more assets
    for label, top_n, capital, base_pct, max_pos in [
        ("Top 11, 28 assets, $1k",      11, 1_000,   0.30, 3),
        ("Top 11, 28 assets, $100k",     11, 100_000, 0.20, 3),
        ("Top 20, 28 assets, $100k",     20, 100_000, 0.15, 5),
        ("Top 30, 28 assets, $100k",     30, 100_000, 0.12, 6),
    ]:
        selected = profitable[:top_n]
        keys = {r.key for r in selected}

        # Count unique assets and strategies
        u_assets = set(r.key.split(":")[0] for r in selected)
        u_strats = set(r.key.split(":")[1] for r in selected)

        sim = run_liquidity_sim(
            result, keys, interval="4h", forward_hours=4.0,
            trade_start_idx=mid, initial_capital=capital,
            base_position_pct=base_pct, max_positions=max_pos,
            use_exit=True, stop_loss=0.03, take_profit=0.05,
            max_hold_hours=48.0,
        )
        sim.label = f"{label} ({len(u_assets)} assets, {len(u_strats)} strategies)"
        print_report(sim, capital)

    print(f"\n{'='*75}")


if __name__ == "__main__":
    asyncio.run(main())
