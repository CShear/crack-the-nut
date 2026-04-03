"""Lead-lag analysis and strategies.

Measures the time-delayed correlation between BTC/ETH and alts to find
exploitable lag structures. Then builds strategies that use the leader's
move to predict the follower's upcoming move.

Usage::

    python3 -m analog.lead_lag --days 730
"""

from __future__ import annotations

import argparse
import asyncio
import math
from dataclasses import dataclass
from typing import Callable

import structlog

from analog.backfill import run_backfill, print_summary, CandleData
from analog.evaluators import HistoricalData
from analog.strategies import COMMISSION

logger = structlog.get_logger()

StrategyEvaluator = Callable[[float, float], float | None]


# ─────────────────────────────────────────────────────────────────────
# LEAD-LAG MEASUREMENT
# ─────────────────────────────────────────────────────────────────────

@dataclass
class LeadLagResult:
    """Lead-lag relationship between two assets."""

    leader: str
    follower: str
    best_lag_bars: int  # how many 4h bars the follower lags
    best_lag_hours: int
    correlation_at_lag: float  # correlation at the optimal lag
    correlation_at_zero: float  # contemporaneous correlation
    improvement: float  # how much better lagged corr is vs zero-lag
    n_observations: int


def measure_lead_lag(
    candles: dict[str, list[CandleData]],
    leader: str,
    follower: str,
    max_lag_bars: int = 12,  # up to 48 hours at 4h bars
) -> LeadLagResult | None:
    """Measure the lead-lag relationship between two assets.

    Computes cross-correlation at each lag offset (0 to max_lag_bars).
    The lag with highest correlation tells us how many bars the follower
    trails the leader.
    """
    leader_candles = sorted(candles.get(leader, []), key=lambda c: c.timestamp_ms)
    follower_candles = sorted(candles.get(follower, []), key=lambda c: c.timestamp_ms)

    if len(leader_candles) < 200 or len(follower_candles) < 200:
        return None

    # Build return series aligned by timestamp
    leader_by_ts: dict[int, float] = {}
    for i in range(1, len(leader_candles)):
        if leader_candles[i - 1].close > 0:
            ts = leader_candles[i].timestamp_ms
            leader_by_ts[ts] = math.log(leader_candles[i].close / leader_candles[i - 1].close)

    follower_by_ts: dict[int, float] = {}
    for i in range(1, len(follower_candles)):
        if follower_candles[i - 1].close > 0:
            ts = follower_candles[i].timestamp_ms
            follower_by_ts[ts] = math.log(follower_candles[i].close / follower_candles[i - 1].close)

    # Get common timestamps
    bar_interval_ms = 4 * 3600 * 1000
    common_ts = sorted(set(leader_by_ts.keys()) & set(follower_by_ts.keys()))

    if len(common_ts) < 100:
        return None

    # Compute cross-correlation at each lag
    best_lag = 0
    best_corr = -1.0
    corr_at_zero = 0.0

    for lag in range(0, max_lag_bars + 1):
        pairs = []
        for ts in common_ts:
            lagged_ts = ts + lag * bar_interval_ms
            leader_ret = leader_by_ts.get(ts)
            follower_ret = follower_by_ts.get(lagged_ts)
            if leader_ret is not None and follower_ret is not None:
                pairs.append((leader_ret, follower_ret))

        if len(pairs) < 50:
            continue

        # Pearson correlation
        n = len(pairs)
        mean_lead = sum(p[0] for p in pairs) / n
        mean_f = sum(p[1] for p in pairs) / n
        cov = sum((lv - mean_lead) * (fv - mean_f) for lv, fv in pairs) / n
        std_l = math.sqrt(sum((lv - mean_lead) ** 2 for lv, _ in pairs) / n)
        std_f = math.sqrt(sum((fv - mean_f) ** 2 for _, fv in pairs) / n)

        if std_l <= 0 or std_f <= 0:
            continue

        corr = cov / (std_l * std_f)

        if lag == 0:
            corr_at_zero = corr

        if corr > best_corr:
            best_corr = corr
            best_lag = lag

    return LeadLagResult(
        leader=leader,
        follower=follower,
        best_lag_bars=best_lag,
        best_lag_hours=best_lag * 4,
        correlation_at_lag=round(best_corr, 4),
        correlation_at_zero=round(corr_at_zero, 4),
        improvement=round(best_corr - corr_at_zero, 4),
        n_observations=len(common_ts),
    )


# ─────────────────────────────────────────────────────────────────────
# LEAD-LAG STRATEGIES
# ─────────────────────────────────────────────────────────────────────

def make_btc_leads_lagged(data: HistoricalData, lag_hours: int = 4) -> StrategyEvaluator:
    """Trade the alt based on BTC's move `lag_hours` ago.

    If BTC moved 2% four hours ago and the alt hasn't caught up yet,
    the alt is likely to follow.
    """
    BTC_MOVE_THRESHOLD = 0.008  # 0.8% BTC move

    def evaluate(ts: float, fwd_hours: float) -> float | None:
        asset = data.primary
        if asset == "BTC":
            return None

        # What did BTC do `lag_hours` ago?
        btc_ret = data.backward_return("BTC", ts - lag_hours * 3600, lag_hours)
        if btc_ret is None or abs(btc_ret) < BTC_MOVE_THRESHOLD:
            return None

        # What has the alt done since then? (has it caught up?)
        alt_ret = data.backward_return(asset, ts, lag_hours)
        if alt_ret is None:
            return None

        beta = data.rolling_beta(asset, "BTC", ts, 42)
        if beta is None or beta <= 0:
            return None

        expected_alt_move = beta * btc_ret
        alt_gap = expected_alt_move - alt_ret  # positive = alt hasn't caught up

        if abs(alt_gap) < 0.005:
            return None  # already caught up, no trade

        # Direction: alt should catch up toward expected move
        direction = 1.0 if alt_gap > 0 else -1.0

        fwd_ret = data.forward_return(asset, ts, fwd_hours)
        if fwd_ret is None:
            return None
        return direction * fwd_ret - COMMISSION

    return evaluate


def make_eth_leads_lagged(data: HistoricalData, lag_hours: int = 4) -> StrategyEvaluator:
    """Same as BTC-leads but using ETH as the leader.

    ETH sometimes leads alts on DeFi-specific narratives.
    """
    ETH_MOVE_THRESHOLD = 0.01  # 1% ETH move (more volatile than BTC)

    def evaluate(ts: float, fwd_hours: float) -> float | None:
        asset = data.primary
        if asset in ("BTC", "ETH"):
            return None

        eth_ret = data.backward_return("ETH", ts - lag_hours * 3600, lag_hours)
        if eth_ret is None or abs(eth_ret) < ETH_MOVE_THRESHOLD:
            return None

        alt_ret = data.backward_return(asset, ts, lag_hours)
        if alt_ret is None:
            return None

        beta = data.rolling_beta(asset, "ETH", ts, 42)
        if beta is None or beta <= 0:
            return None

        expected = beta * eth_ret
        gap = expected - alt_ret

        if abs(gap) < 0.005:
            return None

        direction = 1.0 if gap > 0 else -1.0

        fwd_ret = data.forward_return(asset, ts, fwd_hours)
        if fwd_ret is None:
            return None
        return direction * fwd_ret - COMMISSION

    return evaluate


def make_multi_lag_ensemble(data: HistoricalData) -> StrategyEvaluator:
    """Ensemble of BTC lead signals at multiple lag offsets.

    Checks BTC moves at 4h, 8h, and 12h lags. If multiple lags agree
    on direction and the alt hasn't caught up, trade with higher conviction.
    """
    LAGS = [4, 8, 12]  # hours
    BTC_THRESHOLD = 0.006

    def evaluate(ts: float, fwd_hours: float) -> float | None:
        asset = data.primary
        if asset == "BTC":
            return None

        beta = data.rolling_beta(asset, "BTC", ts, 42)
        if beta is None or beta <= 0:
            return None

        signals: list[float] = []
        for lag in LAGS:
            btc_ret = data.backward_return("BTC", ts - lag * 3600, lag)
            if btc_ret is None or abs(btc_ret) < BTC_THRESHOLD:
                continue

            alt_ret = data.backward_return(asset, ts, lag)
            if alt_ret is None:
                continue

            expected = beta * btc_ret
            gap = expected - alt_ret
            if abs(gap) > 0.003:
                signals.append(1.0 if gap > 0 else -1.0)

        if len(signals) < 2:
            return None  # need at least 2 lags agreeing

        agreement = sum(signals) / len(signals)
        if abs(agreement) < 0.5:
            return None  # conflicting lags

        direction = 1.0 if agreement > 0 else -1.0

        fwd_ret = data.forward_return(asset, ts, fwd_hours)
        if fwd_ret is None:
            return None
        return direction * fwd_ret - COMMISSION

    return evaluate


def make_btc_impulse_alt_catch_up(data: HistoricalData) -> StrategyEvaluator:
    """Trade the alt catch-up after a large BTC impulse move.

    When BTC has a sharp 4h move (>1.5%), high-beta alts often lag
    by 1-2 bars then catch up with amplified magnitude.
    """
    BTC_IMPULSE = 0.015  # 1.5% in 4h is a sharp move
    BETA_MIN = 1.3

    def evaluate(ts: float, fwd_hours: float) -> float | None:
        asset = data.primary
        if asset == "BTC":
            return None

        # Was there a BTC impulse 1 bar ago?
        btc_prev = data.backward_return("BTC", ts - 4 * 3600, 4)
        if btc_prev is None or abs(btc_prev) < BTC_IMPULSE:
            return None

        # Is the alt lagging? (didn't move as much as beta predicts)
        beta = data.rolling_beta(asset, "BTC", ts, 42)
        if beta is None or abs(beta) < BETA_MIN:
            return None

        alt_prev = data.backward_return(asset, ts - 4 * 3600, 4)
        if alt_prev is None:
            return None

        expected = beta * btc_prev
        lag_ratio = alt_prev / expected if expected != 0 else 1.0

        if lag_ratio > 0.8:
            return None  # alt already caught up (moved >= 80% of expected)

        # Alt is lagging → expect catch-up
        direction = 1.0 if btc_prev > 0 else -1.0

        fwd_ret = data.forward_return(asset, ts, fwd_hours)
        if fwd_ret is None:
            return None
        return direction * fwd_ret - COMMISSION

    return evaluate


def make_btc_reversal_alt_still_moving(data: HistoricalData) -> StrategyEvaluator:
    """Fade the alt when BTC has already reversed but the alt hasn't.

    BTC often reverses first. If BTC peaked and started falling but
    the alt is still pushing higher, the alt will follow BTC down.
    """
    BTC_REVERSAL_THRESHOLD = 0.008

    def evaluate(ts: float, fwd_hours: float) -> float | None:
        asset = data.primary
        if asset == "BTC":
            return None

        # BTC direction over last 12h vs last 4h
        btc_12h = data.backward_return("BTC", ts, 12)
        btc_4h = data.backward_return("BTC", ts, 4)
        if btc_12h is None or btc_4h is None:
            return None

        # Detect BTC reversal: 12h trend is one direction, 4h is opposite
        btc_reversed = (btc_12h > BTC_REVERSAL_THRESHOLD and btc_4h < -0.003) or \
                       (btc_12h < -BTC_REVERSAL_THRESHOLD and btc_4h > 0.003)
        if not btc_reversed:
            return None

        # Is the alt still moving in the OLD BTC direction?
        alt_4h = data.backward_return(asset, ts, 4)
        if alt_4h is None:
            return None

        # Alt should be moving same direction as BTC's 12h (the old trend)
        if btc_12h > 0 and alt_4h <= 0:
            return None  # alt already reversed
        if btc_12h < 0 and alt_4h >= 0:
            return None  # alt already reversed

        # Fade the alt — it should follow BTC's reversal
        direction = -1.0 if btc_12h > 0 else 1.0  # opposite of old BTC trend

        fwd_ret = data.forward_return(asset, ts, fwd_hours)
        if fwd_ret is None:
            return None
        return direction * fwd_ret - COMMISSION

    return evaluate


# ─────────────────────────────────────────────────────────────────────
# Registry
# ─────────────────────────────────────────────────────────────────────

LEAD_LAG_STRATEGIES: dict[str, Callable[[HistoricalData], StrategyEvaluator]] = {
    "btc_leads_4h": lambda data: make_btc_leads_lagged(data, lag_hours=4),
    "btc_leads_8h": lambda data: make_btc_leads_lagged(data, lag_hours=8),
    "btc_leads_12h": lambda data: make_btc_leads_lagged(data, lag_hours=12),
    "eth_leads_4h": lambda data: make_eth_leads_lagged(data, lag_hours=4),
    "eth_leads_8h": lambda data: make_eth_leads_lagged(data, lag_hours=8),
    "multi_lag_ensemble": make_multi_lag_ensemble,
    "btc_impulse_catch_up": make_btc_impulse_alt_catch_up,
    "btc_reversal_fade_alt": make_btc_reversal_alt_still_moving,
}


def build_lead_lag_evaluators(data: HistoricalData) -> dict[str, StrategyEvaluator]:
    """Build all lead-lag evaluators."""
    return {name: factory(data) for name, factory in LEAD_LAG_STRATEGIES.items()}


# ─────────────────────────────────────────────────────────────────────
# Analysis CLI
# ─────────────────────────────────────────────────────────────────────

async def main(lookback_days: int = 730):
    print("=" * 70)
    print("  LEAD-LAG ANALYSIS")
    print("=" * 70)

    result = await run_backfill(lookback_days=lookback_days)
    print_summary(result)

    alts = [a for a in sorted(result.candles.keys()) if a not in ("BTC", "ETH")]
    leaders = ["BTC", "ETH"]

    print(f"\n  Measuring lead-lag: {leaders} → {alts}")
    print(f"\n  {'Leader':>6}  {'Follower':>8}  {'Lag':>5}  {'Corr@Lag':>9}  {'Corr@0':>7}  {'Improve':>8}  {'N':>6}")
    print(f"  {'─'*6}  {'─'*8}  {'─'*5}  {'─'*9}  {'─'*7}  {'─'*8}  {'─'*6}")

    for leader in leaders:
        for follower in alts:
            ll = measure_lead_lag(result.candles, leader, follower)
            if ll is None:
                continue
            lag_str = f"{ll.best_lag_hours}h" if ll.best_lag_bars > 0 else "0h"
            print(
                f"  {ll.leader:>6}  {ll.follower:>8}  {lag_str:>5}  "
                f"{ll.correlation_at_lag:>8.4f}  {ll.correlation_at_zero:>6.4f}  "
                f"{ll.improvement:>+7.4f}  {ll.n_observations:>6}"
            )

    # Now test strategies
    print("\n  Testing lead-lag strategies across all alts...\n")

    from analog.evaluators import HistoricalData as HD

    for alt in alts:
        if len(result.candles.get(alt, [])) < 200:
            continue

        data = HD(result.candles, result.funding, primary_asset=alt)
        ll_evals = build_lead_lag_evaluators(data)

        candles = sorted(result.candles[alt], key=lambda c: c.timestamp_ms)
        warmup = 180

        print(f"  {alt}:")
        for name, evaluator in ll_evals.items():
            pnls = []
            for i in range(warmup, len(candles), 6):
                ts = candles[i].timestamp_ms / 1000.0
                try:
                    pnl = evaluator(ts, 4.0)
                except Exception:
                    continue
                if pnl is not None:
                    pnls.append(pnl)

            if len(pnls) >= 10:
                wr = sum(1 for p in pnls if p > 0) / len(pnls)
                mean_ret = sum(pnls) / len(pnls)
                marker = " ★" if mean_ret > 0 and wr > 0.5 else ""
                print(f"    {name:>25}: N={len(pnls):>4}  WR={wr:>5.1%}  Mean={mean_ret:>+7.4f}{marker}")

    print(f"\n{'='*70}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Lead-Lag Analysis")
    parser.add_argument("--days", type=int, default=730)
    args = parser.parse_args()
    asyncio.run(main(lookback_days=args.days))
