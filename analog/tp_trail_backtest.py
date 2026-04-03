"""Backtest: TP-to-Trailing-Stop exit strategy.

When price hits the take-profit level, instead of exiting immediately,
switch to a tight trailing stop. This rides momentum beyond TP while
protecting gains.

Compares:
  1. Baseline: fixed SL + hard TP (current approach)
  2. TP→Trail 0.5%: when TP hit, set 0.5% trailing stop from peak
  3. TP→Trail 1.0%: when TP hit, set 1.0% trailing stop from peak
  4. TP→Trail 1.5%: when TP hit, set 1.5% trailing stop from peak

Each variant tested across all top-20 strategy-asset combos on OOS data.

Usage::

    python3 -m analog.tp_trail_backtest
"""

from __future__ import annotations

import math
import os
import pickle
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from analog.backfill import BackfillResult, CandleData, INTERVAL_HOURS
from analog.evaluators import HistoricalData, build_evaluators
from analog.simulate_top_n import rank_strategies

COMMISSION = 0.0006
INTERVAL = "4h"
FORWARD_HOURS = 4.0
INTERVAL_H = INTERVAL_HOURS.get(INTERVAL, 4.0)


def simulate_exit_tp_to_trail(
    data: HistoricalData,
    asset: str,
    entry_ts: float,
    direction: float,
    stop_loss: float | None = None,
    take_profit: float | None = None,
    trail_after_tp: float = 0.01,  # trailing stop distance after TP is hit
    max_hold_hours: float = 48.0,
) -> tuple[float, float, str] | None:
    """Walk forward bar-by-bar. When TP is hit, switch to trailing stop.

    Phase 1 (before TP): Normal SL check. If price hits TP level,
    DON'T exit — switch to Phase 2.

    Phase 2 (after TP): Track peak P&L from entry. Exit when price
    drops trail_after_tp from peak. SL still active as hard floor.

    Returns (pnl_pct, hold_hours, exit_reason) or None.
    """
    entry_idx = data.get_candle_index(asset, entry_ts)
    if entry_idx is None:
        return None

    candles = data._candle_data[asset]
    entry_price = candles[entry_idx].close
    if entry_price <= 0:
        return None

    max_bars = max(1, int(max_hold_hours / data.interval_hours))
    end_idx = min(entry_idx + max_bars, len(candles) - 1)
    if entry_idx + 1 > end_idx:
        return None

    tp_activated = False
    peak_pnl = 0.0

    for i in range(entry_idx + 1, end_idx + 1):
        candle = candles[i]
        hold_hours = (i - entry_idx) * data.interval_hours

        # --- Always check stop-loss (hard floor) ---
        if stop_loss is not None:
            if direction > 0:
                worst_pnl = (candle.low - entry_price) / entry_price
            else:
                worst_pnl = -(candle.high - entry_price) / entry_price
            if worst_pnl <= -stop_loss:
                exit_pnl = -stop_loss - COMMISSION
                return (exit_pnl, hold_hours, "stop_loss")

        # --- Update peak P&L ---
        if direction > 0:
            bar_peak = (candle.high - entry_price) / entry_price
            bar_worst = (candle.low - entry_price) / entry_price
        else:
            bar_peak = -(candle.low - entry_price) / entry_price
            bar_worst = -(candle.high - entry_price) / entry_price

        peak_pnl = max(peak_pnl, bar_peak)

        # --- Check if TP level is reached this bar ---
        if not tp_activated and take_profit is not None:
            if bar_peak >= take_profit:
                tp_activated = True
                # Don't exit — switch to trailing mode
                # peak_pnl is already updated above

        # --- Phase 2: trailing stop after TP ---
        if tp_activated and peak_pnl > 0:
            if peak_pnl - bar_worst >= trail_after_tp:
                # Trail triggered — exit at peak minus trail
                exit_pnl = max(peak_pnl - trail_after_tp, 0) - COMMISSION
                return (exit_pnl, hold_hours, "tp_trail")

    # --- Time exit ---
    last_candle = candles[end_idx]
    if direction > 0:
        final_pnl = (last_candle.close - entry_price) / entry_price
    else:
        final_pnl = -(last_candle.close - entry_price) / entry_price

    final_hold = (end_idx - entry_idx) * data.interval_hours
    reason = "time_exit_post_tp" if tp_activated else "time_exit"
    return (final_pnl - COMMISSION, final_hold, reason)


def simulate_exit_baseline(
    data: HistoricalData,
    asset: str,
    entry_ts: float,
    direction: float,
    stop_loss: float | None = None,
    take_profit: float | None = None,
    max_hold_hours: float = 48.0,
) -> tuple[float, float, str] | None:
    """Standard fixed SL + TP (current approach)."""
    return data.simulate_exit(
        asset, entry_ts, direction,
        stop_loss=stop_loss, take_profit=take_profit,
        max_hold_hours=max_hold_hours,
    )


def infer_direction(
    data: HistoricalData, asset: str, ts: float,
    evaluator_pnl: float, forward_hours: float,
) -> float:
    fwd = data.forward_return(asset, ts, forward_hours)
    if fwd is not None and abs(fwd) > 0.0001:
        adjusted = evaluator_pnl + COMMISSION
        return 1.0 if (adjusted / fwd) > 0 else -1.0
    return 1.0 if evaluator_pnl > 0 else -1.0


@dataclass
class TradeResult:
    pnl: float
    hold_hours: float
    exit_reason: str
    direction: float
    asset: str
    strategy: str
    ts: float


@dataclass
class VariantSummary:
    label: str
    trades: list[TradeResult] = field(default_factory=list)

    @property
    def n(self) -> int:
        return len(self.trades)

    @property
    def wins(self) -> int:
        return sum(1 for t in self.trades if t.pnl > 0)

    @property
    def wr(self) -> float:
        return self.wins / self.n if self.n else 0

    @property
    def mean_pnl(self) -> float:
        return sum(t.pnl for t in self.trades) / self.n if self.n else 0

    @property
    def total_pnl(self) -> float:
        return sum(t.pnl for t in self.trades)

    @property
    def avg_hold(self) -> float:
        return sum(t.hold_hours for t in self.trades) / self.n if self.n else 0

    @property
    def exit_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for t in self.trades:
            counts[t.exit_reason] = counts.get(t.exit_reason, 0) + 1
        return counts

    @property
    def pf(self) -> float:
        gross_win = sum(t.pnl for t in self.trades if t.pnl > 0)
        gross_loss = abs(sum(t.pnl for t in self.trades if t.pnl <= 0))
        return gross_win / gross_loss if gross_loss > 0 else float("inf")

    @property
    def sharpe(self) -> float:
        if self.n < 2:
            return 0
        pnls = [t.pnl for t in self.trades]
        mean = sum(pnls) / len(pnls)
        var = sum((p - mean) ** 2 for p in pnls) / len(pnls)
        std = math.sqrt(var) if var > 0 else 1e-9
        return mean / std


# ── Per-strategy optimal SL/TP from the research report ──
STRATEGY_EXIT_PARAMS: dict[str, dict] = {
    "rsi_regime_reversion":    {"stop_loss": 0.02, "take_profit": 0.05, "max_hold_hours": 48},
    "residual_breakout":       {"stop_loss": 0.03, "take_profit": 0.05, "max_hold_hours": 48},
    "btc_eth_ratio_rv":        {"stop_loss": 0.02, "take_profit": 0.03, "max_hold_hours": 48},
    "mean_reversion":          {"stop_loss": 0.02, "take_profit": 0.05, "max_hold_hours": 48},
    "bollinger_reversion":     {"stop_loss": 0.02, "take_profit": 0.03, "max_hold_hours": 48},
    "disagreement_breakout":   {"stop_loss": 0.03, "take_profit": 0.05, "max_hold_hours": 48},
    "residual_mean_reversion": {"stop_loss": 0.02, "take_profit": 0.05, "max_hold_hours": 48},
    # These have no SL/TP — test with default 3%/5%
    "flip_beta_rotation":      {"stop_loss": 0.03, "take_profit": 0.05, "max_hold_hours": 24},
    "funding_carry_voladj":    {"stop_loss": 0.03, "take_profit": 0.05, "max_hold_hours": 48},
    "donchian_breakout":       {"stop_loss": 0.03, "take_profit": 0.05, "max_hold_hours": 24},
}


def run_backtest():
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
    step = max(1, int(24 / INTERVAL_H))

    train_end = datetime.fromtimestamp(btc_candles[mid].timestamp_ms / 1000, tz=timezone.utc)
    test_end = datetime.fromtimestamp(btc_candles[-1].timestamp_ms / 1000, tz=timezone.utc)

    print(f"\n{'='*90}")
    print("  TP-TO-TRAILING-STOP BACKTEST")
    print(f"  Train: start → {train_end.date()}")
    print(f"  OOS:   {train_end.date()} → {test_end.date()}")
    print(f"{'='*90}")

    # ── Rank strategies ──
    print("\n  Ranking strategies on training period...")
    t0 = time.time()
    ranked = rank_strategies(result, INTERVAL, FORWARD_HOURS, mid, min_trades=10)
    profitable = [r for r in ranked if r.mean_return > 0 and r.win_rate >= 0.45]
    print(f"  {len(profitable)} profitable combos in {time.time()-t0:.0f}s")

    top20 = profitable[:20]
    print(f"\n  Top 20 combos:")
    for i, r in enumerate(top20, 1):
        print(f"  {i:>3}  {r.sharpe:>+6.3f}  {r.mean_return:>+6.3%}  {r.win_rate:>3.0%}  {r.key}")

    # ── Build evaluators ──
    asset_evaluators: dict[str, dict] = {}
    asset_data: dict[str, HistoricalData] = {}
    for r in top20:
        if r.asset not in asset_evaluators:
            asset_evaluators[r.asset] = build_evaluators(
                result.candles, result.funding, asset=r.asset,
                interval_hours=INTERVAL_H,
            )
            asset_data[r.asset] = HistoricalData(
                result.candles, result.funding,
                primary_asset=r.asset, interval_hours=INTERVAL_H,
            )

    # ── Define variants ──
    variants = [
        ("Baseline (fixed TP)",   "baseline", {}),
        ("TP → Trail 0.5%",      "tp_trail", {"trail_after_tp": 0.005}),
        ("TP → Trail 1.0%",      "tp_trail", {"trail_after_tp": 0.01}),
        ("TP → Trail 1.5%",      "tp_trail", {"trail_after_tp": 0.015}),
        ("TP → Trail 2.0%",      "tp_trail", {"trail_after_tp": 0.02}),
    ]

    # ── Run per-combo ──
    # Aggregate results across all combos
    agg: dict[str, VariantSummary] = {label: VariantSummary(label=label) for label, _, _ in variants}

    # Also per-combo detail
    combo_detail: dict[str, dict[str, VariantSummary]] = {}

    for combo_idx, r in enumerate(top20, 1):
        asset = r.asset
        strategy = r.strategy
        key = r.key
        data = asset_data[asset]
        evaluator = asset_evaluators[asset].get(strategy)
        if evaluator is None:
            continue

        candles = sorted(result.candles[asset], key=lambda c: c.timestamp_ms)
        cap = len(candles)
        oos_start = max(warmup, mid)

        # Get exit params for this strategy
        exit_params = STRATEGY_EXIT_PARAMS.get(strategy, {"stop_loss": 0.03, "take_profit": 0.05, "max_hold_hours": 48})
        sl = exit_params.get("stop_loss")
        tp = exit_params.get("take_profit")
        mh = exit_params.get("max_hold_hours", 48)

        combo_detail[key] = {}

        for vlabel, vtype, vextra in variants:
            combo_detail[key][vlabel] = VariantSummary(label=vlabel)

            for i in range(oos_start, cap, step):
                ts = candles[i].timestamp_ms / 1000.0
                try:
                    ev_pnl = evaluator(ts, FORWARD_HOURS)
                except Exception:
                    continue
                if ev_pnl is None:
                    continue

                direction = infer_direction(data, asset, ts, ev_pnl, FORWARD_HOURS)

                if vtype == "baseline":
                    result_exit = simulate_exit_baseline(
                        data, asset, ts, direction,
                        stop_loss=sl, take_profit=tp, max_hold_hours=mh,
                    )
                else:  # tp_trail
                    result_exit = simulate_exit_tp_to_trail(
                        data, asset, ts, direction,
                        stop_loss=sl, take_profit=tp,
                        trail_after_tp=vextra["trail_after_tp"],
                        max_hold_hours=mh,
                    )

                if result_exit is None:
                    continue

                pnl, hrs, reason = result_exit
                trade = TradeResult(
                    pnl=pnl, hold_hours=hrs, exit_reason=reason,
                    direction=direction, asset=asset, strategy=strategy, ts=ts,
                )
                combo_detail[key][vlabel].trades.append(trade)
                agg[vlabel].trades.append(trade)

        # Print per-combo summary
        print(f"\n  [{combo_idx}/20] {key} (SL={sl}, TP={tp}, MaxH={mh}h)")
        print(f"  {'Variant':<22} {'N':>5} {'WR':>5} {'Mean':>8} {'Total':>8} "
              f"{'AvgH':>5} {'PF':>5} {'Sharpe':>7}  Exit breakdown")
        print(f"  {'─'*22} {'─'*5} {'─'*5} {'─'*8} {'─'*8} {'─'*5} {'─'*5} {'─'*7}  {'─'*35}")

        for vlabel, _, _ in variants:
            vs = combo_detail[key][vlabel]
            if vs.n == 0:
                continue
            ec = vs.exit_counts
            ec_str = ", ".join(f"{k}:{v}" for k, v in sorted(ec.items()))
            print(f"  {vlabel:<22} {vs.n:>5} {vs.wr:>4.0%} {vs.mean_pnl:>+7.3%} "
                  f"{vs.total_pnl:>+7.3%} {vs.avg_hold:>5.1f} {vs.pf:>5.2f} {vs.sharpe:>+6.3f}  {ec_str}")

    # ── Aggregate summary ──
    print(f"\n\n{'='*90}")
    print("  AGGREGATE RESULTS (all 20 combos)")
    print(f"{'='*90}\n")

    print(f"  {'Variant':<22} {'N':>6} {'WR':>5} {'Mean':>8} {'Total':>10} "
          f"{'AvgH':>5} {'PF':>5} {'Sharpe':>7}")
    print(f"  {'─'*22} {'─'*6} {'─'*5} {'─'*8} {'─'*10} {'─'*5} {'─'*5} {'─'*7}")

    for vlabel, _, _ in variants:
        vs = agg[vlabel]
        if vs.n == 0:
            continue
        print(f"  {vlabel:<22} {vs.n:>6} {vs.wr:>4.0%} {vs.mean_pnl:>+7.3%} "
              f"{vs.total_pnl:>+9.3%} {vs.avg_hold:>5.1f} {vs.pf:>5.2f} {vs.sharpe:>+6.3f}")

    # ── Exit reason breakdown ──
    print(f"\n  Exit Reason Breakdown:")
    print(f"  {'Variant':<22} {'SL':>6} {'TP':>6} {'Trail':>6} {'Time':>6} {'TP+Trail':>8}")
    print(f"  {'─'*22} {'─'*6} {'─'*6} {'─'*6} {'─'*6} {'─'*8}")

    for vlabel, _, _ in variants:
        vs = agg[vlabel]
        if vs.n == 0:
            continue
        ec = vs.exit_counts
        n = vs.n
        sl_pct = ec.get("stop_loss", 0) / n * 100
        tp_pct = ec.get("take_profit", 0) / n * 100
        trail_pct = ec.get("tp_trail", 0) / n * 100
        time_pct = (ec.get("time_exit", 0) + ec.get("time_exit_post_tp", 0)) / n * 100
        tp_trail_pct = trail_pct  # TP that converted to trail
        print(f"  {vlabel:<22} {sl_pct:>5.1f}% {tp_pct:>5.1f}% {trail_pct:>5.1f}% "
              f"{time_pct:>5.1f}% {tp_trail_pct:>7.1f}%")

    # ── Detailed: what happens to trades that HIT TP? ──
    print(f"\n\n{'='*90}")
    print("  FOCUS: Trades that reach the TP level")
    print(f"{'='*90}\n")

    # For each trail variant, show the TP-triggered trades only
    for vlabel, vtype, vextra in variants:
        if vtype != "tp_trail":
            continue
        vs = agg[vlabel]
        tp_triggered = [t for t in vs.trades if t.exit_reason in ("tp_trail", "time_exit_post_tp")]
        non_tp = [t for t in vs.trades if t.exit_reason not in ("tp_trail", "time_exit_post_tp")]

        if not tp_triggered:
            continue

        trail_dist = vextra.get("trail_after_tp", 0)
        n_tpt = len(tp_triggered)
        mean_tpt = sum(t.pnl for t in tp_triggered) / n_tpt
        wins_tpt = sum(1 for t in tp_triggered if t.pnl > 0)
        avg_hold_tpt = sum(t.hold_hours for t in tp_triggered) / n_tpt

        # Compare: what would these same trades have made with a hard TP?
        # The hard TP trades made exactly TP - COMMISSION each
        # Find the TP levels for each strategy
        hard_tp_pnls = []
        for t in tp_triggered:
            ep = STRATEGY_EXIT_PARAMS.get(t.strategy, {})
            tp = ep.get("take_profit", 0.05)
            hard_tp_pnls.append(tp - COMMISSION)
        mean_hard_tp = sum(hard_tp_pnls) / len(hard_tp_pnls)

        improvement = mean_tpt - mean_hard_tp

        print(f"  {vlabel} (trail={trail_dist*100:.1f}% after TP):")
        print(f"    TP-triggered trades:    {n_tpt}")
        print(f"    Mean P&L (trail exit):  {mean_tpt:+.3%}")
        print(f"    Mean P&L (hard TP):     {mean_hard_tp:+.3%}")
        print(f"    Improvement:            {improvement:+.3%} per trade")
        print(f"    Win rate:               {wins_tpt/n_tpt:.0%}")
        print(f"    Avg hold hours:         {avg_hold_tpt:.1f}h")

        # Distribution of gains beyond TP
        gains_beyond = [t.pnl - (STRATEGY_EXIT_PARAMS.get(t.strategy, {}).get("take_profit", 0.05) - COMMISSION) for t in tp_triggered]
        positive_beyond = [g for g in gains_beyond if g > 0]
        negative_beyond = [g for g in gains_beyond if g < 0]

        if positive_beyond:
            print(f"    Trades that gained beyond TP:  {len(positive_beyond)} ({len(positive_beyond)/n_tpt*100:.0f}%)")
            print(f"      Mean extra gain:   {sum(positive_beyond)/len(positive_beyond):+.3%}")
            print(f"      Max extra gain:    {max(positive_beyond):+.3%}")
        if negative_beyond:
            print(f"    Trades that gave back:         {len(negative_beyond)} ({len(negative_beyond)/n_tpt*100:.0f}%)")
            print(f"      Mean giveback:     {sum(negative_beyond)/len(negative_beyond):+.3%}")
        print()

    # ── Portfolio simulation comparison ──
    print(f"\n{'='*90}")
    print("  PORTFOLIO SIMULATION: Top-11 with each exit variant")
    print(f"{'='*90}\n")

    top11 = profitable[:11]
    top11_keys = {r.key for r in top11}

    for vlabel, vtype, vextra in variants:
        # Simulate compounding portfolio
        equity = 1000.0
        equity_curve = [(0, equity)]
        trades_taken = []
        position_pct = 0.30
        max_positions = 3

        # Step through OOS daily
        for bar_i in range(mid, total, step):
            ts = btc_candles[bar_i].timestamp_ms / 1000.0

            # Collect signals
            signals = []
            for r in top11:
                evaluator = asset_evaluators[r.asset].get(r.strategy)
                if evaluator is None:
                    continue
                try:
                    ev_pnl = evaluator(ts, FORWARD_HOURS)
                except Exception:
                    continue
                if ev_pnl is None:
                    continue
                direction = infer_direction(
                    asset_data[r.asset], r.asset, ts, ev_pnl, FORWARD_HOURS,
                )
                signals.append((r.asset, r.strategy, direction))

            if not signals:
                continue

            # Deterministic shuffle
            import hashlib
            seed = int(hashlib.md5(str(ts).encode()).hexdigest()[:8], 16)
            indices = list(range(len(signals)))
            for j in range(len(indices) - 1, 0, -1):
                seed = (seed * 1103515245 + 12345) & 0x7fffffff
                k = seed % (j + 1)
                indices[j], indices[k] = indices[k], indices[j]

            # Diversify by asset
            selected = []
            seen = set()
            for idx in indices:
                a, s, d = signals[idx]
                if a not in seen:
                    selected.append((a, s, d))
                    seen.add(a)
                if len(selected) >= max_positions:
                    break

            for asset, strategy, direction in selected:
                ep = STRATEGY_EXIT_PARAMS.get(strategy, {"stop_loss": 0.03, "take_profit": 0.05, "max_hold_hours": 48})
                sl = ep.get("stop_loss")
                tp = ep.get("take_profit")
                mh = ep.get("max_hold_hours", 48)

                d = asset_data[asset]

                if vtype == "baseline":
                    res = simulate_exit_baseline(d, asset, ts, direction, sl, tp, mh)
                else:
                    res = simulate_exit_tp_to_trail(
                        d, asset, ts, direction, sl, tp,
                        trail_after_tp=vextra["trail_after_tp"],
                        max_hold_hours=mh,
                    )

                if res is None:
                    continue

                pnl_pct, hrs, reason = res
                size_usd = equity * position_pct
                pnl_usd = size_usd * pnl_pct
                equity += pnl_usd
                equity = max(equity, 0)
                trades_taken.append(pnl_pct)
                equity_curve.append((bar_i, equity))

            if equity <= 0:
                break

        # Report
        final = equity
        total_ret = (final - 1000) / 1000
        n_trades = len(trades_taken)
        wins = sum(1 for p in trades_taken if p > 0)
        wr = wins / n_trades if n_trades else 0

        peak = 1000
        max_dd = 0
        for _, eq in equity_curve:
            peak = max(peak, eq)
            dd = (peak - eq) / peak
            max_dd = max(max_dd, dd)

        gross_win = sum(p for p in trades_taken if p > 0)
        gross_loss = abs(sum(p for p in trades_taken if p <= 0))
        pf = gross_win / gross_loss if gross_loss > 0 else float("inf")

        print(f"  {vlabel:<22}  Return: {total_ret:>+7.1%}  "
              f"MaxDD: {max_dd:>5.1%}  "
              f"WR: {wr:>4.0%}  "
              f"PF: {pf:>5.2f}  "
              f"Trades: {n_trades:>4}  "
              f"Final: ${final:>8,.0f}")

    print(f"\n{'='*90}")


if __name__ == "__main__":
    run_backtest()
