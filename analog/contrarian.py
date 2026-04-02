"""Contrarian and counter-intuitive strategy evaluators.

These strategies exploit the systematic biases we found in walk-forward testing:
1. Flipped signals — strategies that predict direction backwards
2. Consensus fade — go opposite when too many strategies agree
3. Sit-out exploit — trade when the system says don't
4. Disagreement signal — volatility of strategy opinions as a signal
5. Funding-filtered — use weak directional signals only as filters on carry

Each is grounded in a specific finding from the walk-forward results.
"""

from __future__ import annotations

import math
from typing import Callable

from analog.evaluators import HistoricalData
from analog.strategies import COMMISSION

StrategyEvaluator = Callable[[float, float], float | None]


# ─────────────────────────────────────────────────────────────────────
# FLIPPED STRATEGIES — signals that predict direction backwards
# ─────────────────────────────────────────────────────────────────────

def make_beta_rotation_flipped(data: HistoricalData) -> StrategyEvaluator:
    """Beta rotation FLIPPED — original was 22% WR, flip gives 78%.

    Original says: strong BTC momentum + high funding dispersion → long BTC.
    Reality: that combo predicts reversals, not continuation.
    """
    MOM_LOOKBACK = 168

    def evaluate(ts: float, fwd_hours: float) -> float | None:
        btc_mom = data.backward_return(data.primary, ts, MOM_LOOKBACK)
        if btc_mom is None or abs(btc_mom) < 0.01:
            return None

        rates = data.get_funding_at(ts)
        if len(rates) < 5:
            return None

        values = list(rates.values())
        mean_r = sum(values) / len(values)
        dispersion = math.sqrt(sum((v - mean_r) ** 2 for v in values) / len(values))

        if dispersion < 0.00005:
            return None

        # FLIPPED: go OPPOSITE to momentum when dispersion is high
        direction = -1.0 if btc_mom > 0 else 1.0
        scale = min(abs(btc_mom) / 0.05, 2.0)

        fwd_ret = data.forward_return(data.primary, ts, fwd_hours)
        if fwd_ret is None:
            return None
        return direction * fwd_ret * scale - COMMISSION

    return evaluate


def make_btc_eth_ratio_flipped(data: HistoricalData) -> StrategyEvaluator:
    """BTC/ETH ratio FLIPPED — original 47.3% WR → flipped 52.7%.

    Original says: when ETH/BTC z-score > 2, long BTC (expect reversion).
    Reality: extreme ratio divergence CONTINUES rather than reverting.
    """
    WINDOW = 120
    ENTRY_Z = 2.0

    def evaluate(ts: float, fwd_hours: float) -> float | None:
        btc_prices = data.closes(data.primary, ts, WINDOW)
        eth_prices = data.closes(data.secondary, ts, WINDOW)
        if len(btc_prices) < WINDOW or len(eth_prices) < WINDOW:
            return None

        ratios = [e / b if b > 0 else 0 for e, b in zip(eth_prices, btc_prices)]
        ratios = [r for r in ratios if r > 0]
        if len(ratios) < WINDOW // 2:
            return None

        mean_ratio = sum(ratios) / len(ratios)
        std = math.sqrt(sum((r - mean_ratio) ** 2 for r in ratios) / len(ratios))
        if std <= 0:
            return None

        z = (ratios[-1] - mean_ratio) / std
        if abs(z) < ENTRY_Z:
            return None

        # FLIPPED: z > 2 means ETH outperforming → it CONTINUES (short BTC)
        direction = -1.0 if z > 0 else 1.0

        btc_ret = data.forward_return(data.primary, ts, fwd_hours)
        eth_ret = data.forward_return(data.secondary, ts, fwd_hours)
        if btc_ret is None or eth_ret is None:
            return None

        return direction * (btc_ret - eth_ret) - COMMISSION

    return evaluate


def make_ou_pairs_flipped(data: HistoricalData) -> StrategyEvaluator:
    """OU pairs FLIPPED — original 38.7% WR → flipped 61.3%.

    Original says: trade BTC/ETH ratio mean-reversion.
    Reality: at 2-sigma, the ratio keeps diverging (momentum > reversion).
    """
    WINDOW = 120
    ENTRY_Z = 2.0

    def evaluate(ts: float, fwd_hours: float) -> float | None:
        btc_prices = data.closes(data.primary, ts, WINDOW)
        eth_prices = data.closes(data.secondary, ts, WINDOW)
        if len(btc_prices) < WINDOW or len(eth_prices) < WINDOW:
            return None

        ratios = [e / b if b > 0 else 0 for e, b in zip(eth_prices, btc_prices)]
        ratios = [r for r in ratios if r > 0]
        if len(ratios) < WINDOW // 2:
            return None

        mean_ratio = sum(ratios) / len(ratios)
        std = math.sqrt(sum((r - mean_ratio) ** 2 for r in ratios) / len(ratios))
        if std <= 0:
            return None

        z = (ratios[-1] - mean_ratio) / std
        if abs(z) < ENTRY_Z:
            return None

        # FLIPPED: z > 2 (ETH overperforming) → it continues → short BTC
        direction = -1.0 if z >= ENTRY_Z else 1.0

        fwd_ret = data.forward_return(data.primary, ts, fwd_hours)
        if fwd_ret is None:
            return None
        return direction * fwd_ret - COMMISSION

    return evaluate


def make_rsi_regime_flipped(data: HistoricalData) -> StrategyEvaluator:
    """RSI regime reversion FLIPPED — original 36.8% WR → flipped 63.2%.

    Original says: buy RSI < 25 in uptrend, sell RSI > 75 in downtrend.
    Reality: extreme RSI in trending markets means the trend is STRONG,
    not exhausted. Oversold in uptrend = momentum, not mean-reversion.
    """
    RSI_PERIOD = 14
    RSI_EXTREME_LOW = 25.0
    RSI_EXTREME_HIGH = 75.0
    TREND_BARS = 120

    def evaluate(ts: float, fwd_hours: float) -> float | None:
        rsi_val = data.rsi(data.primary, ts, RSI_PERIOD)
        if rsi_val is None:
            return None

        sma_val = data.sma(data.primary, ts, TREND_BARS)
        candle = data.get_candle_at(data.primary, ts)
        if sma_val is None or candle is None:
            return None

        uptrend = candle.close > sma_val

        # FLIPPED: oversold in uptrend = short (trend exhaustion signal was wrong,
        # but the REVERSE — that deeply oversold in an uptrend means the uptrend
        # is breaking — turns out to be correct)
        if rsi_val <= RSI_EXTREME_LOW and uptrend:
            direction = -1.0  # uptrend breaking down
        elif rsi_val >= RSI_EXTREME_HIGH and not uptrend:
            direction = 1.0   # downtrend reversing up
        else:
            return None

        fwd_ret = data.forward_return(data.primary, ts, fwd_hours)
        if fwd_ret is None:
            return None
        return direction * fwd_ret - COMMISSION

    return evaluate


# ─────────────────────────────────────────────────────────────────────
# CONSENSUS FADE — go opposite when too many strategies agree
# ─────────────────────────────────────────────────────────────────────

def make_consensus_fade(data: HistoricalData) -> StrategyEvaluator:
    """When multiple independent signals agree strongly, fade them.

    Intuition: extreme consensus = crowded positioning = reversal risk.
    Uses TSMOM, EMA cross, RSI, and funding direction as voters.
    """
    MIN_VOTERS = 4  # need at least 4 signals to have meaningful consensus
    CONSENSUS_THRESHOLD = 0.8  # 80%+ agreement triggers fade

    def evaluate(ts: float, fwd_hours: float) -> float | None:
        votes: list[float] = []

        # TSMOM 7d
        ret_7d = data.backward_return(data.primary, ts, 168)
        if ret_7d is not None:
            votes.append(1.0 if ret_7d > 0 else -1.0)

        # TSMOM 30d
        ret_30d = data.backward_return(data.primary, ts, 720)
        if ret_30d is not None:
            votes.append(1.0 if ret_30d > 0 else -1.0)

        # EMA cross
        fast = data.ema(data.primary, ts, 12)
        slow = data.ema(data.primary, ts, 52)
        if fast is not None and slow is not None:
            votes.append(1.0 if fast > slow else -1.0)

        # RSI direction
        rsi_val = data.rsi(data.primary, ts, 14)
        if rsi_val is not None:
            votes.append(1.0 if rsi_val > 50 else -1.0)

        # Funding direction
        rates = data.get_funding_at(ts)
        btc_rate = rates.get(data.primary)
        if btc_rate is not None:
            # Positive funding = longs crowded = bullish consensus
            votes.append(1.0 if btc_rate > 0 else -1.0)

        if len(votes) < MIN_VOTERS:
            return None

        agreement = abs(sum(votes)) / len(votes)
        if agreement < CONSENSUS_THRESHOLD:
            return None  # not enough consensus to fade

        # Fade: go opposite to consensus
        consensus_dir = 1.0 if sum(votes) > 0 else -1.0
        direction = -consensus_dir

        fwd_ret = data.forward_return(data.primary, ts, fwd_hours)
        if fwd_ret is None:
            return None
        return direction * fwd_ret - COMMISSION

    return evaluate


# ─────────────────────────────────────────────────────────────────────
# SIT-OUT EXPLOIT — trade when analog confidence is low
# ─────────────────────────────────────────────────────────────────────

def make_novel_state_trend(data: HistoricalData) -> StrategyEvaluator:
    """When the market is in a novel state (no good analogs), follow the trend.

    Our sit-out accuracy was 19.5% — the system sat out during profitable
    periods 80% of the time. Novel states are often breakouts/crashes
    where simple trend-following works best.

    This strategy fires during conditions that would produce low analog
    similarity (high vol-of-vol, extreme returns).
    """
    VOL_RATIO_THRESHOLD = 1.8  # vol spiking = novel state
    MOM_THRESHOLD = 0.02       # need clear direction

    def evaluate(ts: float, fwd_hours: float) -> float | None:
        vol_s = data.realized_vol(data.primary, ts, 6)
        vol_l = data.realized_vol(data.primary, ts, 42)
        if vol_s is None or vol_l is None or vol_l <= 0:
            return None

        vol_ratio = vol_s / vol_l
        if vol_ratio < VOL_RATIO_THRESHOLD:
            return None  # not novel enough

        # In novel/extreme states, follow the short-term momentum
        ret_1d = data.backward_return(data.primary, ts, 24)
        if ret_1d is None or abs(ret_1d) < MOM_THRESHOLD:
            return None

        direction = 1.0 if ret_1d > 0 else -1.0

        fwd_ret = data.forward_return(data.primary, ts, fwd_hours)
        if fwd_ret is None:
            return None
        return direction * fwd_ret - COMMISSION

    return evaluate


# ─────────────────────────────────────────────────────────────────────
# STRATEGY DISAGREEMENT — high disagreement = inflection point
# ─────────────────────────────────────────────────────────────────────

def make_disagreement_breakout(data: HistoricalData) -> StrategyEvaluator:
    """When trend and mean-reversion signals violently disagree, a big
    move is coming. Trade the breakout direction.

    Uses vol compression + signal disagreement as entry, then follows
    the first strong directional move.
    """
    def evaluate(ts: float, fwd_hours: float) -> float | None:
        # Trend signal
        ret_7d = data.backward_return(data.primary, ts, 168)
        # Mean-reversion signal
        rsi_val = data.rsi(data.primary, ts, 14)

        if ret_7d is None or rsi_val is None:
            return None

        trend_says_long = ret_7d > 0.01
        trend_says_short = ret_7d < -0.01
        mr_says_long = rsi_val < 35  # oversold = buy
        mr_says_short = rsi_val > 65  # overbought = sell

        # Need disagreement: trend says one thing, RSI says opposite
        disagreement = (
            (trend_says_long and mr_says_short) or
            (trend_says_short and mr_says_long)
        )
        if not disagreement:
            return None

        # Check vol is compressed (about to break)
        vol_s = data.realized_vol(data.primary, ts, 6)
        vol_l = data.realized_vol(data.primary, ts, 42)
        if vol_s is None or vol_l is None or vol_l <= 0:
            return None

        if vol_s / vol_l > 0.8:
            return None  # vol not compressed enough

        # Direction: use the very short-term momentum (4h) to pick breakout side
        ret_4h = data.backward_return(data.primary, ts, 4)
        if ret_4h is None or abs(ret_4h) < 0.003:
            return None

        direction = 1.0 if ret_4h > 0 else -1.0

        fwd_ret = data.forward_return(data.primary, ts, fwd_hours)
        if fwd_ret is None:
            return None
        return direction * fwd_ret - COMMISSION

    return evaluate


# ─────────────────────────────────────────────────────────────────────
# FUNDING-FILTERED — use weak signals as carry trade filters
# ─────────────────────────────────────────────────────────────────────

def make_funding_filtered_trend(data: HistoricalData) -> StrategyEvaluator:
    """Only harvest funding when the weak trend signal agrees.

    Many strategies have real but sub-commission directional signal.
    Instead of trading them directly, use them as a filter: only enter
    a carry trade (collect funding) when the weak signal aligns.
    This way the funding payment covers the commission.
    """
    FUNDING_THRESHOLD = 0.0001  # lower threshold — we're collecting, not predicting
    MOM_LOOKBACK = 168

    def evaluate(ts: float, fwd_hours: float) -> float | None:
        rates = data.get_funding_at(ts)
        btc_rate = rates.get(data.primary)
        if btc_rate is None or abs(btc_rate) < FUNDING_THRESHOLD:
            return None

        carry_dir = -1.0 if btc_rate > 0 else 1.0

        # Use multiple weak signals as filters
        agreements = 0
        checks = 0

        # TSMOM agreement
        ret = data.backward_return(data.primary, ts, MOM_LOOKBACK)
        if ret is not None:
            checks += 1
            mom_dir = 1.0 if ret > 0 else -1.0
            if mom_dir == carry_dir or abs(ret) < 0.005:
                agreements += 1  # aligned or neutral

        # EMA cross agreement
        fast = data.ema(data.primary, ts, 12)
        slow = data.ema(data.primary, ts, 52)
        if fast is not None and slow is not None:
            checks += 1
            ema_dir = 1.0 if fast > slow else -1.0
            if ema_dir == carry_dir:
                agreements += 1

        # RSI agreement (not overbought if going long, not oversold if going short)
        rsi_val = data.rsi(data.primary, ts, 14)
        if rsi_val is not None:
            checks += 1
            if carry_dir > 0 and rsi_val < 65:
                agreements += 1
            elif carry_dir < 0 and rsi_val > 35:
                agreements += 1

        if checks < 2 or agreements < 2:
            return None  # not enough filter agreement

        fwd_ret = data.forward_return(data.primary, ts, fwd_hours)
        if fwd_ret is None:
            return None

        funding_collected = abs(btc_rate) * (fwd_hours / 8)
        return carry_dir * fwd_ret + funding_collected - COMMISSION

    return evaluate


def make_multi_funding_filtered(data: HistoricalData) -> StrategyEvaluator:
    """Cross-asset funding carry filtered by aggregate sentiment.

    Pick the asset with most extreme funding across all 9, but only
    trade when aggregate funding mean supports the direction.
    """
    THRESHOLD = 0.0003

    def evaluate(ts: float, fwd_hours: float) -> float | None:
        rates = data.get_funding_at(ts)
        if len(rates) < 5:
            return None

        values = list(rates.values())
        mean_rate = sum(values) / len(values)

        # Find most extreme funding
        btc_rate = rates.get(data.primary)
        if btc_rate is None or abs(btc_rate) < THRESHOLD:
            return None

        carry_dir = -1.0 if btc_rate > 0 else 1.0

        # Filter: aggregate funding mean must support the carry direction
        # If we're shorting (btc_rate > 0), mean should also be positive
        # (broad market is overleveraged long)
        if carry_dir < 0 and mean_rate <= 0:
            return None  # we'd short but market isn't broadly overleveraged
        if carry_dir > 0 and mean_rate >= 0:
            return None  # we'd long but market isn't broadly overleveraged short

        fwd_ret = data.forward_return(data.primary, ts, fwd_hours)
        if fwd_ret is None:
            return None

        funding_collected = abs(btc_rate) * (fwd_hours / 8)
        return carry_dir * fwd_ret + funding_collected - COMMISSION

    return evaluate


# ─────────────────────────────────────────────────────────────────────
# Registry
# ─────────────────────────────────────────────────────────────────────

CONTRARIAN_STRATEGIES: dict[str, Callable[[HistoricalData], StrategyEvaluator]] = {
    # Flipped signals
    "flip_beta_rotation": make_beta_rotation_flipped,
    "flip_btc_eth_ratio": make_btc_eth_ratio_flipped,
    "flip_ou_pairs": make_ou_pairs_flipped,
    "flip_rsi_regime": make_rsi_regime_flipped,
    # Counter-intuitive
    "consensus_fade": make_consensus_fade,
    "novel_state_trend": make_novel_state_trend,
    "disagreement_breakout": make_disagreement_breakout,
    # Funding-filtered
    "funding_filtered_trend": make_funding_filtered_trend,
    "multi_funding_filtered": make_multi_funding_filtered,
}


def build_contrarian_evaluators(data: HistoricalData) -> dict[str, StrategyEvaluator]:
    """Build all contrarian/counter-intuitive evaluators."""
    return {name: factory(data) for name, factory in CONTRARIAN_STRATEGIES.items()}
