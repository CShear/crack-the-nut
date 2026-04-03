"""Beta-adjusted and cross-asset strategies.

These strategies account for the relationship between alts and BTC/ETH:
1. Beta-adjusted versions of winning strategies — trade the residual
2. BTC-led alt strategies — use BTC signals to trade alts
3. Beta-relative strategies — trade alts that move more/less than expected

The key insight: if ARB drops 10% when BTC drops 3%, and ARB's beta to BTC
is 2.5, the "expected" ARB drop is 7.5%. The extra 2.5% is the residual —
that's the idiosyncratic signal we're trading.
"""

from __future__ import annotations

from typing import Callable

from analog.evaluators import HistoricalData
from analog.strategies import COMMISSION

StrategyEvaluator = Callable[[float, float], float | None]


# ─────────────────────────────────────────────────────────────────────
# BETA-ADJUSTED STRATEGIES — trade the residual, not raw returns
# ─────────────────────────────────────────────────────────────────────

def make_residual_mean_reversion(data: HistoricalData) -> StrategyEvaluator:
    """Mean reversion on the RESIDUAL (beta-adjusted) return.

    Original mean_reversion fires when asset drops 3%+. But if BTC
    also dropped 2% and beta is 1.5, the expected drop was 3%.
    Only trade the reversion if the residual is extreme.

    This filters out false signals where the alt is just tracking BTC.
    """
    RESIDUAL_THRESHOLD = 0.02  # 2% residual move (after removing beta)
    VOL_RATIO_MIN = 1.2

    def evaluate(ts: float, fwd_hours: float) -> float | None:
        asset = data.primary
        if asset == "BTC":
            return None  # BTC has no benchmark to adjust against

        # Get 1d return and residual
        ret_1d = data.backward_return(asset, ts, 24)
        if ret_1d is None or abs(ret_1d) < 0.01:
            return None

        beta = data.rolling_beta(asset, "BTC", ts, data.b(42))
        if beta is None:
            return None

        btc_ret = data.backward_return("BTC", ts, 24)
        if btc_ret is None:
            return None

        residual = ret_1d - beta * btc_ret

        if abs(residual) < RESIDUAL_THRESHOLD:
            return None  # move was explained by BTC, no idiosyncratic signal

        # Check vol is elevated (same as original mean_reversion)
        vol = data.realized_vol(asset, ts, data.b(42))
        vol_long = data.realized_vol(asset, ts, data.b(180))
        if vol is None or vol_long is None or vol_long <= 0:
            return None
        if vol / vol_long < VOL_RATIO_MIN:
            return None

        # Fade the residual
        direction = -1.0 if residual > 0 else 1.0

        # Trade the residual return (asset return minus beta * BTC return)
        fwd_residual = data.residual_return(asset, "BTC", ts, fwd_hours)
        if fwd_residual is None:
            return None

        return direction * fwd_residual - COMMISSION

    return evaluate


def make_residual_rsi_regime(data: HistoricalData) -> StrategyEvaluator:
    """RSI regime on residual — the flipped version, beta-adjusted.

    Original rsi_regime_flipped: oversold + uptrend → short.
    This version: only fire when RSI extreme is NOT explained by BTC.
    """
    RSI_EXTREME_LOW = 25.0
    RSI_EXTREME_HIGH = 75.0
    TREND_BARS = 120

    def evaluate(ts: float, fwd_hours: float) -> float | None:
        asset = data.primary
        if asset == "BTC":
            return None

        rsi_val = data.rsi(asset, ts, data.b(14))
        if rsi_val is None:
            return None

        sma_val = data.sma(asset, ts, data.b(TREND_BARS))
        candle = data.get_candle_at(asset, ts)
        if sma_val is None or candle is None:
            return None

        uptrend = candle.close > sma_val

        # Check RSI extreme
        if rsi_val <= RSI_EXTREME_LOW and uptrend:
            direction = -1.0
        elif rsi_val >= RSI_EXTREME_HIGH and not uptrend:
            direction = 1.0
        else:
            return None

        # Beta filter: is BTC also extreme? If so, this is just beta.
        btc_rsi = data.rsi("BTC", ts, data.b(14))
        if btc_rsi is not None:
            # If BTC RSI is similarly extreme, the alt is just tracking BTC
            if rsi_val <= RSI_EXTREME_LOW and btc_rsi <= 35:
                return None  # both oversold, it's beta
            if rsi_val >= RSI_EXTREME_HIGH and btc_rsi >= 65:
                return None  # both overbought, it's beta

        fwd_ret = data.forward_return(asset, ts, fwd_hours)
        if fwd_ret is None:
            return None
        return direction * fwd_ret - COMMISSION

    return evaluate


def make_residual_breakout(data: HistoricalData) -> StrategyEvaluator:
    """Breakout on residual returns — the asset is breaking out vs BTC.

    A Donchian breakout on the raw price might just be BTC-driven.
    This checks if the asset is making NEW HIGHS *relative to BTC*.
    """
    PERIOD = 50

    def evaluate(ts: float, fwd_hours: float) -> float | None:
        asset = data.primary
        if asset == "BTC":
            return None

        asset_candles = data.get_recent_candles(asset, ts, data.b(PERIOD))
        btc_candles = data.get_recent_candles("BTC", ts, data.b(PERIOD))

        if len(asset_candles) < data.b(PERIOD) or len(btc_candles) < data.b(PERIOD):
            return None

        # Compute ratio series (asset / BTC)
        ratios = []
        for a, b in zip(asset_candles, btc_candles):
            if b.close > 0:
                ratios.append(a.close / b.close)
        if len(ratios) < PERIOD // 2:
            return None

        current_ratio = ratios[-1]
        prev_high = max(ratios[:-1])
        prev_low = min(ratios[:-1])

        if current_ratio > prev_high:
            direction = 1.0  # asset outperforming BTC → breakout up relative
        elif current_ratio < prev_low:
            direction = -1.0  # asset underperforming BTC → breakout down relative
        else:
            return None

        fwd_ret = data.forward_return(asset, ts, fwd_hours)
        if fwd_ret is None:
            return None
        return direction * fwd_ret - COMMISSION

    return evaluate


# ─────────────────────────────────────────────────────────────────────
# BTC-LED ALT STRATEGIES — use BTC as the signal, trade the alt
# ─────────────────────────────────────────────────────────────────────

def make_btc_leads_alt_momentum(data: HistoricalData) -> StrategyEvaluator:
    """BTC momentum applied to alt execution.

    BTC often moves first, alts follow with a lag and amplified magnitude.
    Use BTC 4h-1d momentum as the signal, trade the alt.
    """
    BTC_MOM_THRESHOLD = 0.01  # 1% BTC move
    BETA_MIN = 1.2  # only trade alts with beta > 1.2 (amplifiers)

    def evaluate(ts: float, fwd_hours: float) -> float | None:
        asset = data.primary
        if asset == "BTC":
            return None

        btc_mom = data.backward_return("BTC", ts, 24)
        if btc_mom is None or abs(btc_mom) < BTC_MOM_THRESHOLD:
            return None

        beta = data.rolling_beta(asset, "BTC", ts, data.b(42))
        if beta is None or abs(beta) < BETA_MIN:
            return None  # not a high-beta alt

        # Follow BTC momentum on the high-beta alt (amplified)
        direction = 1.0 if btc_mom > 0 else -1.0

        fwd_ret = data.forward_return(asset, ts, fwd_hours)
        if fwd_ret is None:
            return None
        return direction * fwd_ret - COMMISSION

    return evaluate


def make_btc_leads_alt_reversal(data: HistoricalData) -> StrategyEvaluator:
    """BTC momentum applied to alt MEAN REVERSION.

    When BTC has a big move, high-beta alts overshoot. Fade the alt's
    overreaction after a BTC impulse.
    """
    BTC_IMPULSE = 0.02  # 2% BTC move in 4h
    BETA_MIN = 1.5  # high beta = bigger overshoot

    def evaluate(ts: float, fwd_hours: float) -> float | None:
        asset = data.primary
        if asset == "BTC":
            return None

        btc_4h = data.backward_return("BTC", ts, 4)
        if btc_4h is None or abs(btc_4h) < BTC_IMPULSE:
            return None

        beta = data.rolling_beta(asset, "BTC", ts, data.b(42))
        if beta is None or abs(beta) < BETA_MIN:
            return None

        # Check alt actually overshot (moved more than beta * BTC)
        alt_4h = data.backward_return(asset, ts, 4)
        if alt_4h is None:
            return None

        expected_move = beta * btc_4h
        overshoot = alt_4h - expected_move
        if abs(overshoot) < 0.005:
            return None  # didn't overshoot enough

        # Fade the overshoot
        direction = -1.0 if overshoot > 0 else 1.0

        fwd_ret = data.forward_return(asset, ts, fwd_hours)
        if fwd_ret is None:
            return None
        return direction * fwd_ret - COMMISSION

    return evaluate


def make_btc_divergence(data: HistoricalData) -> StrategyEvaluator:
    """Trade when alt diverges significantly from BTC direction.

    When BTC is up but the alt is down (or vice versa), the divergence
    usually resolves — the alt catches up. But sometimes the alt is
    leading. Use funding rates to disambiguate.
    """
    DIVERGENCE_THRESHOLD = 0.015  # 1.5% difference in opposite directions

    def evaluate(ts: float, fwd_hours: float) -> float | None:
        asset = data.primary
        if asset == "BTC":
            return None

        btc_1d = data.backward_return("BTC", ts, 24)
        alt_1d = data.backward_return(asset, ts, 24)
        if btc_1d is None or alt_1d is None:
            return None

        # Need opposite directions with meaningful magnitude
        if btc_1d * alt_1d >= 0:
            return None  # same direction, no divergence
        if abs(btc_1d) < DIVERGENCE_THRESHOLD or abs(alt_1d) < DIVERGENCE_THRESHOLD:
            return None

        # Check funding to see who's right
        rates = data.get_funding_at(ts)
        btc_funding = rates.get("BTC", 0)
        alt_funding = rates.get(asset, 0)

        # If alt's funding is more extreme, the alt position is more crowded
        # → expect the alt to revert toward BTC
        if abs(alt_funding) > abs(btc_funding):
            # Alt is crowded → it will catch up to BTC direction
            direction = 1.0 if btc_1d > 0 else -1.0
        else:
            # BTC is crowded → alt might be leading
            direction = 1.0 if alt_1d > 0 else -1.0

        fwd_ret = data.forward_return(asset, ts, fwd_hours)
        if fwd_ret is None:
            return None
        return direction * fwd_ret - COMMISSION

    return evaluate


# ─────────────────────────────────────────────────────────────────────
# BETA-RELATIVE STRATEGIES
# ─────────────────────────────────────────────────────────────────────

def make_beta_compression(data: HistoricalData) -> StrategyEvaluator:
    """Trade beta regime changes.

    When rolling beta compresses (alt becoming less sensitive to BTC),
    it often means the alt is about to have an idiosyncratic move.
    When beta expands, the alt is re-coupling with BTC.
    """
    BETA_SHORT = 12   # ~2 days
    BETA_LONG = 42    # ~7 days
    COMPRESSION_THRESHOLD = 0.6

    def evaluate(ts: float, fwd_hours: float) -> float | None:
        asset = data.primary
        if asset == "BTC":
            return None

        beta_short = data.rolling_beta(asset, "BTC", ts, data.b(BETA_SHORT))
        beta_long = data.rolling_beta(asset, "BTC", ts, data.b(BETA_LONG))
        if beta_short is None or beta_long is None or abs(beta_long) < 0.1:
            return None

        ratio = abs(beta_short) / abs(beta_long)
        if ratio > COMPRESSION_THRESHOLD:
            return None  # beta not compressed enough

        # Beta compressed → expect idiosyncratic move
        # Use short-term alt momentum for direction
        alt_mom = data.backward_return(asset, ts, 24)
        if alt_mom is None or abs(alt_mom) < 0.005:
            return None

        direction = 1.0 if alt_mom > 0 else -1.0

        fwd_ret = data.forward_return(asset, ts, fwd_hours)
        if fwd_ret is None:
            return None
        return direction * fwd_ret - COMMISSION

    return evaluate


def make_high_beta_funding_carry(data: HistoricalData) -> StrategyEvaluator:
    """Funding carry sized by beta.

    High-beta alts with extreme funding are the best carry targets:
    the funding rate is higher AND the directional move when funding
    unwinds is amplified by beta.
    """
    FUNDING_THRESHOLD = 0.0002
    BETA_MIN = 1.3

    def evaluate(ts: float, fwd_hours: float) -> float | None:
        asset = data.primary
        if asset == "BTC":
            return None

        rates = data.get_funding_at(ts)
        rate = rates.get(asset)
        if rate is None or abs(rate) < FUNDING_THRESHOLD:
            return None

        beta = data.rolling_beta(asset, "BTC", ts, data.b(42))
        if beta is None or abs(beta) < BETA_MIN:
            return None  # not high-beta enough

        # Carry direction
        direction = -1.0 if rate > 0 else 1.0

        # BTC momentum filter — don't fight a strong BTC trend
        btc_mom = data.backward_return("BTC", ts, 168)
        if btc_mom is not None:
            btc_dir = 1.0 if btc_mom > 0 else -1.0
            if btc_dir != direction and abs(btc_mom) > 0.03:
                return None  # BTC momentum opposes carry, skip

        fwd_ret = data.forward_return(asset, ts, fwd_hours)
        if fwd_ret is None:
            return None

        funding_collected = abs(rate) * (fwd_hours / 8)
        return direction * fwd_ret + funding_collected - COMMISSION

    return evaluate


# ─────────────────────────────────────────────────────────────────────
# Registry
# ─────────────────────────────────────────────────────────────────────

BETA_STRATEGIES: dict[str, Callable[[HistoricalData], StrategyEvaluator]] = {
    # Beta-adjusted (residual)
    "residual_mean_reversion": make_residual_mean_reversion,
    "residual_rsi_regime": make_residual_rsi_regime,
    "residual_breakout": make_residual_breakout,
    # BTC-led
    "btc_leads_alt_momentum": make_btc_leads_alt_momentum,
    "btc_leads_alt_reversal": make_btc_leads_alt_reversal,
    "btc_divergence": make_btc_divergence,
    # Beta-relative
    "beta_compression": make_beta_compression,
    "high_beta_funding_carry": make_high_beta_funding_carry,
}


def build_beta_evaluators(data: HistoricalData) -> dict[str, StrategyEvaluator]:
    """Build all beta-adjusted evaluators."""
    return {name: factory(data) for name, factory in BETA_STRATEGIES.items()}
