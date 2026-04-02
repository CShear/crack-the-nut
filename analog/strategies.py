"""25 established trading strategies as analog evaluators.

Each factory function takes a HistoricalData instance and returns a
StrategyEvaluator: Callable[[float, float], float | None].

Strategy families:
  1-4:   Time-series momentum (TSMOM)
  5-6:   Cross-sectional momentum
  7-10:  Carry / funding rate
  11-13: Mean reversion
  14-16: Volatility-based
  17-19: Trend following
  20-21: Pairs / relative value
  22-25: Crypto-native

Sources cited in docstrings. All strategies use 0.06% round-trip commission.
"""

from __future__ import annotations

import math
from typing import Callable

from analog.evaluators import HistoricalData

StrategyEvaluator = Callable[[float, float], float | None]

COMMISSION = 0.0006  # 0.03% each way


# ─────────────────────────────────────────────────────────────────────
# FAMILY 1: TIME-SERIES MOMENTUM (TSMOM)
# ─────────────────────────────────────────────────────────────────────

def make_tsmom_classic(data: HistoricalData) -> StrategyEvaluator:
    """Classic TSMOM — Moskowitz, Ooi & Pedersen (2012).

    Go long if past N-day return is positive, short if negative.
    Vol-target the position.
    """
    LOOKBACK_BARS = 42  # 7 days at 4h
    VOL_TARGET = 0.15  # 15% annualized target

    def evaluate(ts: float, fwd_hours: float) -> float | None:
        ret = data.backward_return(data.primary, ts, LOOKBACK_BARS * 4)
        if ret is None:
            return None
        vol = data.realized_vol(data.primary, ts, LOOKBACK_BARS)
        if vol is None or vol <= 0:
            return None

        direction = 1.0 if ret > 0 else -1.0
        # Vol-scale: target / realized (annualized)
        ann_vol = vol * math.sqrt(6 * 365)  # 6 bars/day, 365 days
        scale = min(VOL_TARGET / ann_vol, 2.0) if ann_vol > 0 else 1.0

        fwd_ret = data.forward_return(data.primary, ts, fwd_hours)
        if fwd_ret is None:
            return None
        return direction * fwd_ret * scale - COMMISSION

    return evaluate


def make_tsmom_multi_horizon(data: HistoricalData) -> StrategyEvaluator:
    """Multi-horizon TSMOM ensemble — Baltas & Kosowski (2013), CFM.

    Blend signals across 1-week, 1-month, 3-month lookbacks.
    """
    HORIZONS = [42, 180, 540]  # 7d, 30d, 90d in 4h bars
    HORIZON_HOURS = [h * 4 for h in HORIZONS]

    def evaluate(ts: float, fwd_hours: float) -> float | None:
        signals = []
        for bars, hours in zip(HORIZONS, HORIZON_HOURS):
            ret = data.backward_return(data.primary, ts, hours)
            if ret is not None:
                signals.append(1.0 if ret > 0 else -1.0)

        if len(signals) < 2:
            return None

        # Blended direction: average of signs
        direction = sum(signals) / len(signals)
        if abs(direction) < 0.3:
            return None  # conflicting signals, sit out

        direction = 1.0 if direction > 0 else -1.0
        fwd_ret = data.forward_return(data.primary, ts, fwd_hours)
        if fwd_ret is None:
            return None
        return direction * fwd_ret - COMMISSION

    return evaluate


def make_tsmom_adaptive(data: HistoricalData) -> StrategyEvaluator:
    """Adaptive TSMOM — Levine & Pedersen (2016), AQR.

    Shorten lookback in high-vol, lengthen in low-vol.
    """
    BASE_BARS = 42  # 7 days
    VOL_SHORT = 6   # 1 day
    VOL_LONG = 180   # 30 days

    def evaluate(ts: float, fwd_hours: float) -> float | None:
        vol_short = data.realized_vol(data.primary, ts, VOL_SHORT)
        vol_long = data.realized_vol(data.primary, ts, VOL_LONG)
        if vol_short is None or vol_long is None or vol_long <= 0:
            return None

        vol_ratio = vol_short / vol_long
        # High vol → shorter lookback, low vol → longer
        if vol_ratio > 1.5:
            lookback = max(12, BASE_BARS // 2)  # ~2 days
        elif vol_ratio < 0.7:
            lookback = min(120, BASE_BARS * 2)  # ~20 days
        else:
            lookback = BASE_BARS

        ret = data.backward_return(data.primary, ts, lookback * 4)
        if ret is None:
            return None

        direction = 1.0 if ret > 0 else -1.0
        fwd_ret = data.forward_return(data.primary, ts, fwd_hours)
        if fwd_ret is None:
            return None
        return direction * fwd_ret - COMMISSION

    return evaluate


def make_tsmom_volscaled(data: HistoricalData) -> StrategyEvaluator:
    """TSMOM with vol-scaling — Barroso & Santa-Clara (2015).

    Constant-risk targeting to avoid momentum crashes.
    """
    LOOKBACK_BARS = 42
    VOL_TARGET = 0.10  # 10% annualized target (conservative)
    VOL_WINDOW = 30     # bars for vol estimate

    def evaluate(ts: float, fwd_hours: float) -> float | None:
        ret = data.backward_return(data.primary, ts, LOOKBACK_BARS * 4)
        if ret is None:
            return None

        vol = data.realized_vol(data.primary, ts, VOL_WINDOW)
        if vol is None or vol <= 0:
            return None

        ann_vol = vol * math.sqrt(6 * 365)
        scale = min(VOL_TARGET / ann_vol, 3.0) if ann_vol > 0 else 1.0

        direction = 1.0 if ret > 0 else -1.0
        fwd_ret = data.forward_return(data.primary, ts, fwd_hours)
        if fwd_ret is None:
            return None
        return direction * fwd_ret * scale - COMMISSION

    return evaluate


# ─────────────────────────────────────────────────────────────────────
# FAMILY 2: CROSS-SECTIONAL MOMENTUM
# ─────────────────────────────────────────────────────────────────────

def make_xsection_momentum(data: HistoricalData) -> StrategyEvaluator:
    """Cross-sectional momentum — Liu, Tsyvinski & Wu (2022).

    Rank assets by cumulative funding (proxy for price momentum).
    Long high-momentum, short low-momentum.
    """
    LOOKBACK_BARS = 42  # 7 days of funding
    MIN_ASSETS = 5

    def evaluate(ts: float, fwd_hours: float) -> float | None:
        scores: dict[str, float] = {}
        for asset_name in ["BTC", "ETH", "SOL", "DOGE", "ARB", "OP", "AVAX", "LINK", "WIF"]:
            hist = data.funding_asset_history(asset_name, ts, LOOKBACK_BARS)
            if len(hist) >= LOOKBACK_BARS // 2:
                scores[asset_name] = sum(hist)

        if len(scores) < MIN_ASSETS:
            return None

        ranked = sorted(scores.items(), key=lambda x: x[1])
        # Short the bottom third (most negative funding = most shorted)
        # Long the top third (most positive funding = most longed... wait)
        # Actually: high cumulative funding = longs paying → crowded long
        # Cross-section momentum: the DIRECTION of funding indicates positioning
        # But funding momentum in crypto means: assets where longs are paying
        # have been trending up. Go long winners (high cum funding) and short losers.

        n_tercile = max(1, len(ranked) // 3)
        # We can only trade BTC/ETH with candle data
        # Use the signal to decide direction on BTC
        btc_score = scores.get("BTC")
        if btc_score is None:
            return None

        btc_rank = [name for name, _ in ranked].index("BTC")
        n = len(ranked)

        if btc_rank < n_tercile:
            direction = -1.0  # BTC is bottom tercile (losers) → short
        elif btc_rank >= n - n_tercile:
            direction = 1.0   # BTC is top tercile (winners) → long
        else:
            return None  # middle tercile, no signal

        fwd_ret = data.forward_return(data.primary, ts, fwd_hours)
        if fwd_ret is None:
            return None
        return direction * fwd_ret - COMMISSION

    return evaluate


def make_52w_high_momentum(data: HistoricalData) -> StrategyEvaluator:
    """Nearness to high — George & Hwang (2004).

    Assets near their recent high continue outperforming.
    """
    HIGH_WINDOW = 540  # ~90 days at 4h (proxy for 52w at this timeframe)
    NEAR_HIGH_PCT = 0.95  # within 5% of high
    FAR_FROM_HIGH_PCT = 0.80  # more than 20% below high

    def evaluate(ts: float, fwd_hours: float) -> float | None:
        candles = data.get_recent_candles(data.primary, ts, HIGH_WINDOW)
        if len(candles) < HIGH_WINDOW // 2:
            return None

        high = max(c.high for c in candles)
        current = candles[-1].close
        if high <= 0:
            return None

        ratio = current / high

        if ratio >= NEAR_HIGH_PCT:
            direction = 1.0  # near high → momentum continues
        elif ratio <= FAR_FROM_HIGH_PCT:
            direction = -1.0  # far from high → momentum continues down
        else:
            return None

        fwd_ret = data.forward_return(data.primary, ts, fwd_hours)
        if fwd_ret is None:
            return None
        return direction * fwd_ret - COMMISSION

    return evaluate


# ─────────────────────────────────────────────────────────────────────
# FAMILY 3: CARRY / FUNDING RATE
# ─────────────────────────────────────────────────────────────────────

def make_funding_carry_voladj(data: HistoricalData) -> StrategyEvaluator:
    """Funding carry, vol-adjusted — Koijen et al. (2018).

    Harvest extreme funding, size by inverse realized vol.
    """
    THRESHOLD = 0.0003  # 0.03% per period
    VOL_WINDOW = 42

    def evaluate(ts: float, fwd_hours: float) -> float | None:
        rates = data.get_funding_at(ts)
        btc_rate = rates.get(data.primary)
        if btc_rate is None or abs(btc_rate) < THRESHOLD:
            return None

        vol = data.realized_vol(data.primary, ts, VOL_WINDOW)
        if vol is None or vol <= 0:
            return None

        # Inverse vol sizing (higher vol → smaller position)
        ann_vol = vol * math.sqrt(6 * 365)
        vol_scale = min(0.15 / ann_vol, 2.0) if ann_vol > 0 else 1.0

        direction = -1.0 if btc_rate > 0 else 1.0
        fwd_ret = data.forward_return(data.primary, ts, fwd_hours)
        if fwd_ret is None:
            return None

        funding_collected = abs(btc_rate) * (fwd_hours / 8)
        return (direction * fwd_ret + funding_collected) * vol_scale - COMMISSION

    return evaluate


def make_funding_surface_regime(data: HistoricalData) -> StrategyEvaluator:
    """Funding surface regime — Alameda/Jump practitioner knowledge.

    Uses aggregate funding mean, dispersion, and skew across 9 assets.
    """
    MEAN_EXTREME = 0.0002  # aggregate mean threshold
    DISPERSION_HIGH = 0.0003

    def evaluate(ts: float, fwd_hours: float) -> float | None:
        rates = data.get_funding_at(ts)
        if len(rates) < 5:
            return None

        values = list(rates.values())
        mean_rate = sum(values) / len(values)
        dispersion = math.sqrt(sum((v - mean_rate) ** 2 for v in values) / len(values))

        # High mean = market overheated (crowded longs) → tilt short
        if abs(mean_rate) < MEAN_EXTREME:
            return None  # no signal when funding is balanced

        direction = -1.0 if mean_rate > 0 else 1.0

        # Size based on dispersion: high dispersion = more conviction
        # (clear divergence across assets supports the signal)
        size_mult = 1.0
        if dispersion > DISPERSION_HIGH:
            size_mult = 1.5

        fwd_ret = data.forward_return(data.primary, ts, fwd_hours)
        if fwd_ret is None:
            return None

        funding_collected = abs(mean_rate) * (fwd_hours / 8)
        return (direction * fwd_ret + funding_collected) * size_mult - COMMISSION

    return evaluate


def make_funding_momentum_filter(data: HistoricalData) -> StrategyEvaluator:
    """Funding carry + momentum filter — Cartea et al. (2015).

    Only harvest carry when momentum doesn't oppose you.
    """
    FUNDING_THRESHOLD = 0.0003
    MOM_LOOKBACK_HOURS = 168  # 7 days

    def evaluate(ts: float, fwd_hours: float) -> float | None:
        rates = data.get_funding_at(ts)
        btc_rate = rates.get(data.primary)
        if btc_rate is None or abs(btc_rate) < FUNDING_THRESHOLD:
            return None

        # Funding says go short (positive funding) or long (negative)
        carry_direction = -1.0 if btc_rate > 0 else 1.0

        # Check momentum doesn't oppose
        mom = data.backward_return(data.primary, ts, MOM_LOOKBACK_HOURS)
        if mom is None:
            return None

        mom_direction = 1.0 if mom > 0 else -1.0

        # Only trade if momentum is aligned or neutral
        if carry_direction != mom_direction and abs(mom) > 0.02:
            return None  # strong opposing momentum, skip

        fwd_ret = data.forward_return(data.primary, ts, fwd_hours)
        if fwd_ret is None:
            return None

        funding_collected = abs(btc_rate) * (fwd_hours / 8)
        return carry_direction * fwd_ret + funding_collected - COMMISSION

    return evaluate


def make_funding_term_structure(data: HistoricalData) -> StrategyEvaluator:
    """Funding rate term structure trade — Bitmex Research / Wintermute.

    When short-term funding diverges from long-term, trade the reversion.
    """
    SHORT_BARS = 2   # ~8h of funding
    LONG_BARS = 42    # ~7d of funding
    DIVERGENCE_THRESHOLD = 0.0005

    def evaluate(ts: float, fwd_hours: float) -> float | None:
        hist = data.funding_asset_history(data.primary, ts, LONG_BARS)
        if len(hist) < LONG_BARS:
            return None

        short_avg = sum(hist[-SHORT_BARS:]) / SHORT_BARS
        long_avg = sum(hist) / len(hist)
        divergence = short_avg - long_avg

        if abs(divergence) < DIVERGENCE_THRESHOLD:
            return None

        # Short-term funding spiked above long-term → transient, fade it
        direction = -1.0 if divergence > 0 else 1.0

        fwd_ret = data.forward_return(data.primary, ts, fwd_hours)
        if fwd_ret is None:
            return None

        # Collect the funding reversion
        funding_collected = abs(short_avg) * (fwd_hours / 8)
        return direction * fwd_ret + funding_collected - COMMISSION

    return evaluate


# ─────────────────────────────────────────────────────────────────────
# FAMILY 4: MEAN REVERSION
# ─────────────────────────────────────────────────────────────────────

def make_bollinger_reversion(data: HistoricalData) -> StrategyEvaluator:
    """Bollinger Band mean reversion — Bollinger (2001), Gerritsen (2020).

    Buy at lower band + oversold RSI, sell at upper band + overbought RSI.
    """
    BB_PERIOD = 20
    BB_MULT = 2.0
    RSI_OVERSOLD = 30.0
    RSI_OVERBOUGHT = 70.0

    def evaluate(ts: float, fwd_hours: float) -> float | None:
        bb = data.bollinger(data.primary, ts, BB_PERIOD, BB_MULT)
        if bb is None:
            return None
        lower, _mid, upper = bb

        candle = data.get_candle_at(data.primary, ts)
        if candle is None:
            return None

        rsi_val = data.rsi(data.primary, ts, 14)
        if rsi_val is None:
            return None

        if candle.close <= lower and rsi_val <= RSI_OVERSOLD:
            direction = 1.0  # oversold → buy
        elif candle.close >= upper and rsi_val >= RSI_OVERBOUGHT:
            direction = -1.0  # overbought → sell
        else:
            return None

        fwd_ret = data.forward_return(data.primary, ts, fwd_hours)
        if fwd_ret is None:
            return None
        return direction * fwd_ret - COMMISSION

    return evaluate


def make_rsi_regime_reversion(data: HistoricalData) -> StrategyEvaluator:
    """RSI mean reversion with regime filter — Faber (2007), Bianchi (2023).

    Only buy dips in uptrends, only sell rips in downtrends.
    """
    RSI_PERIOD = 14
    RSI_BUY = 25.0
    RSI_SELL = 75.0
    TREND_BARS = 120  # ~20 days SMA for trend filter

    def evaluate(ts: float, fwd_hours: float) -> float | None:
        rsi_val = data.rsi(data.primary, ts, RSI_PERIOD)
        if rsi_val is None:
            return None

        sma_val = data.sma(data.primary, ts, TREND_BARS)
        candle = data.get_candle_at(data.primary, ts)
        if sma_val is None or candle is None:
            return None

        uptrend = candle.close > sma_val

        if rsi_val <= RSI_BUY and uptrend:
            direction = 1.0  # oversold in uptrend → buy dip
        elif rsi_val >= RSI_SELL and not uptrend:
            direction = -1.0  # overbought in downtrend → sell rip
        else:
            return None

        fwd_ret = data.forward_return(data.primary, ts, fwd_hours)
        if fwd_ret is None:
            return None
        return direction * fwd_ret - COMMISSION

    return evaluate


def make_ou_pairs(data: HistoricalData) -> StrategyEvaluator:
    """OU-process pairs trading on BTC/ETH — Avellaneda & Lee (2010).

    Trade mean-reversion of the BTC/ETH ratio.
    """
    WINDOW = 120  # 20 days for z-score calculation
    ENTRY_Z = 2.0

    def evaluate(ts: float, fwd_hours: float) -> float | None:
        btc_prices = data.closes(data.primary, ts, WINDOW)
        eth_prices = data.closes(data.secondary, ts, WINDOW)
        if len(btc_prices) < WINDOW or len(eth_prices) < WINDOW:
            return None

        # Compute ratio series
        ratios = [e / b if b > 0 else 0 for e, b in zip(eth_prices, btc_prices)]
        ratios = [r for r in ratios if r > 0]
        if len(ratios) < WINDOW // 2:
            return None

        mean_ratio = sum(ratios) / len(ratios)
        var = sum((r - mean_ratio) ** 2 for r in ratios) / len(ratios)
        std = math.sqrt(var) if var > 0 else 0
        if std <= 0:
            return None

        current_ratio = ratios[-1]
        z = (current_ratio - mean_ratio) / std

        if abs(z) < ENTRY_Z:
            return None  # no signal

        # z > 2: ETH overperforming → short ETH, long BTC → bet on BTC
        # z < -2: BTC overperforming → long ETH, short BTC → bet on ETH
        # We trade as a BTC position for simplicity
        if z >= ENTRY_Z:
            direction = 1.0  # long BTC (expect ratio to revert = ETH falls / BTC rises)
        else:
            direction = -1.0  # short BTC (expect ratio to revert = BTC falls / ETH rises)

        fwd_ret = data.forward_return(data.primary, ts, fwd_hours)
        if fwd_ret is None:
            return None
        return direction * fwd_ret - COMMISSION

    return evaluate


# ─────────────────────────────────────────────────────────────────────
# FAMILY 5: VOLATILITY-BASED
# ─────────────────────────────────────────────────────────────────────

def make_donchian_breakout(data: HistoricalData) -> StrategyEvaluator:
    """Donchian Channel breakout — Turtle trading (1960s), Fil (2020).

    Buy on 20-period high breakout, short on 20-period low breakout.
    """
    PERIOD = 20

    def evaluate(ts: float, fwd_hours: float) -> float | None:
        channel = data.donchian(data.primary, ts, PERIOD)
        if channel is None:
            return None
        low, high = channel

        candle = data.get_candle_at(data.primary, ts)
        if candle is None:
            return None

        # Breakout: close above high or below low of the lookback
        # Compare to *previous* channel (exclude current bar)
        prev_channel = data.donchian(data.primary, ts - 4 * 3600, PERIOD)
        if prev_channel is None:
            return None
        prev_low, prev_high = prev_channel

        if candle.close > prev_high:
            direction = 1.0
        elif candle.close < prev_low:
            direction = -1.0
        else:
            return None

        fwd_ret = data.forward_return(data.primary, ts, fwd_hours)
        if fwd_ret is None:
            return None
        return direction * fwd_ret - COMMISSION

    return evaluate


def make_volatility_squeeze(data: HistoricalData) -> StrategyEvaluator:
    """Volatility squeeze (BBands inside Keltner) — Carter (2012), Caporale (2021).

    When BBands contract inside Keltner Channels, a big move is coming.
    Trade the direction of the breakout.
    """
    BB_PERIOD = 20
    BB_MULT = 2.0
    KELT_PERIOD = 20
    KELT_MULT = 1.5

    def evaluate(ts: float, fwd_hours: float) -> float | None:
        bb = data.bollinger(data.primary, ts, BB_PERIOD, BB_MULT)
        kelt = data.keltner(data.primary, ts, KELT_PERIOD, KELT_MULT)
        if bb is None or kelt is None:
            return None

        bb_lower, _bb_mid, bb_upper = bb
        kelt_lower, _kelt_mid, kelt_upper = kelt

        # Squeeze: BBands inside Keltner
        in_squeeze = bb_lower > kelt_lower and bb_upper < kelt_upper

        # Check if squeeze just released (was in squeeze previously)
        prev_bb = data.bollinger(data.primary, ts - 4 * 3600, BB_PERIOD, BB_MULT)
        prev_kelt = data.keltner(data.primary, ts - 4 * 3600, KELT_PERIOD, KELT_MULT)
        if prev_bb is None or prev_kelt is None:
            return None

        was_in_squeeze = prev_bb[0] > prev_kelt[0] and prev_bb[2] < prev_kelt[2]

        if not was_in_squeeze or in_squeeze:
            return None  # only trade on squeeze release

        # Direction: use momentum (close vs midline)
        candle = data.get_candle_at(data.primary, ts)
        if candle is None:
            return None

        direction = 1.0 if candle.close > bb[1] else -1.0

        fwd_ret = data.forward_return(data.primary, ts, fwd_hours)
        if fwd_ret is None:
            return None
        return direction * fwd_ret - COMMISSION

    return evaluate


def make_vol_term_structure(data: HistoricalData) -> StrategyEvaluator:
    """Realized vol term structure trade — Gatheral (2006).

    When short vol >> long vol, expect vol to revert → reduce size.
    When short vol << long vol, expect breakout → increase size.
    Combined with trend direction for a directional trade.
    """
    VOL_SHORT = 6    # 1 day
    VOL_LONG = 42     # 7 days
    INVERSION_THRESHOLD = 1.8  # short/long > 1.8 = spiking
    COMPRESSION_THRESHOLD = 0.6  # short/long < 0.6 = compressing

    def evaluate(ts: float, fwd_hours: float) -> float | None:
        vol_s = data.realized_vol(data.primary, ts, VOL_SHORT)
        vol_l = data.realized_vol(data.primary, ts, VOL_LONG)
        if vol_s is None or vol_l is None or vol_l <= 0:
            return None

        ratio = vol_s / vol_l

        # Get trend direction
        ret_7d = data.backward_return(data.primary, ts, 168)
        if ret_7d is None:
            return None

        if ratio > INVERSION_THRESHOLD:
            # Vol spiking → mean reversion likely, fade the move
            direction = -1.0 if ret_7d > 0 else 1.0
            scale = 0.5  # small size
        elif ratio < COMPRESSION_THRESHOLD:
            # Vol compressed → breakout likely, follow the trend
            direction = 1.0 if ret_7d > 0 else -1.0
            scale = 1.5  # bigger size
        else:
            return None  # normal vol, no edge

        fwd_ret = data.forward_return(data.primary, ts, fwd_hours)
        if fwd_ret is None:
            return None
        return direction * fwd_ret * scale - COMMISSION

    return evaluate


# ─────────────────────────────────────────────────────────────────────
# FAMILY 6: TREND FOLLOWING
# ─────────────────────────────────────────────────────────────────────

def make_dual_ma_crossover(data: HistoricalData) -> StrategyEvaluator:
    """Dual EMA crossover — Brock et al. (1992), Detzel (2021).

    Fast EMA crosses slow EMA. Magnitude filter to avoid whipsaws.
    """
    FAST = 12   # ~2 days at 4h
    SLOW = 52   # ~8.7 days at 4h
    MIN_GAP_PCT = 0.002  # 0.2% minimum gap between EMAs

    def evaluate(ts: float, fwd_hours: float) -> float | None:
        fast_ema = data.ema(data.primary, ts, FAST)
        slow_ema = data.ema(data.primary, ts, SLOW)
        if fast_ema is None or slow_ema is None or slow_ema <= 0:
            return None

        gap = (fast_ema - slow_ema) / slow_ema

        if abs(gap) < MIN_GAP_PCT:
            return None  # too close, whipsaw zone

        direction = 1.0 if gap > 0 else -1.0

        fwd_ret = data.forward_return(data.primary, ts, fwd_hours)
        if fwd_ret is None:
            return None
        return direction * fwd_ret - COMMISSION

    return evaluate


def make_kaufman_adaptive(data: HistoricalData) -> StrategyEvaluator:
    """Kaufman Adaptive MA (KAMA) — Kaufman (1995).

    Efficiency Ratio adapts smoothing: responsive in trends, flat in chop.
    """
    ER_PERIOD = 20  # bars for efficiency ratio
    FAST_SC = 2.0 / (2 + 1)    # fast smoothing constant
    SLOW_SC = 2.0 / (30 + 1)   # slow smoothing constant

    def evaluate(ts: float, fwd_hours: float) -> float | None:
        prices = data.closes(data.primary, ts, ER_PERIOD + 30)  # extra for KAMA warmup
        if len(prices) < ER_PERIOD + 10:
            return None

        # Compute KAMA
        kama = prices[0]
        kama_prev = kama
        for i in range(ER_PERIOD, len(prices)):
            # Efficiency Ratio
            net_change = abs(prices[i] - prices[i - ER_PERIOD])
            sum_changes = sum(abs(prices[j] - prices[j - 1])
                              for j in range(i - ER_PERIOD + 1, i + 1))
            er = net_change / sum_changes if sum_changes > 0 else 0

            # Smoothing constant
            sc = (er * (FAST_SC - SLOW_SC) + SLOW_SC) ** 2

            kama_prev = kama
            kama = kama_prev + sc * (prices[i] - kama_prev)

        current = prices[-1]
        if kama <= 0:
            return None

        # Signal: price crosses KAMA
        gap = (current - kama) / kama
        if abs(gap) < 0.001:
            return None

        direction = 1.0 if current > kama else -1.0

        fwd_ret = data.forward_return(data.primary, ts, fwd_hours)
        if fwd_ret is None:
            return None
        return direction * fwd_ret - COMMISSION

    return evaluate


def make_atr_breakout(data: HistoricalData) -> StrategyEvaluator:
    """ATR-based breakout with trailing stop — Kestner (2003), Clenow (2012).

    Enter on N-bar high breakout. Simulated trailing stop at 3x ATR.
    """
    BREAKOUT_PERIOD = 50  # ~8 days at 4h
    ATR_PERIOD = 14
    ATR_STOP_MULT = 3.0

    def evaluate(ts: float, fwd_hours: float) -> float | None:
        candles = data.get_recent_candles(data.primary, ts, BREAKOUT_PERIOD)
        if len(candles) < BREAKOUT_PERIOD:
            return None

        current = candles[-1].close
        prev_high = max(c.high for c in candles[:-1])
        prev_low = min(c.low for c in candles[:-1])

        atr_val = data.atr(data.primary, ts, ATR_PERIOD)
        if atr_val is None:
            return None

        if current > prev_high:
            direction = 1.0
            stop = current - ATR_STOP_MULT * atr_val
        elif current < prev_low:
            direction = -1.0
            stop = current + ATR_STOP_MULT * atr_val
        else:
            return None

        # Simulate forward: check if stopped out
        fwd_bars = max(1, int(fwd_hours / 4))
        fwd_candles = data.get_recent_candles(data.primary, ts + fwd_hours * 3600, fwd_bars + 1)
        if len(fwd_candles) < 2:
            return None

        exit_price = fwd_candles[-1].close
        for c in fwd_candles[1:]:
            if direction > 0 and c.low <= stop:
                exit_price = stop
                break
            elif direction < 0 and c.high >= stop:
                exit_price = stop
                break
            # Trail the stop
            if direction > 0:
                stop = max(stop, c.close - ATR_STOP_MULT * atr_val)
            else:
                stop = min(stop, c.close + ATR_STOP_MULT * atr_val)

        pnl = direction * (exit_price - current) / current
        return pnl - COMMISSION

    return evaluate


# ─────────────────────────────────────────────────────────────────────
# FAMILY 7: PAIRS / RELATIVE VALUE
# ─────────────────────────────────────────────────────────────────────

def make_btc_eth_ratio(data: HistoricalData) -> StrategyEvaluator:
    """BTC/ETH ratio mean reversion — Gatev et al. (2006).

    Z-score on ETH/BTC ratio. Trade convergence.
    """
    WINDOW = 120   # 20 days
    ENTRY_Z = 2.0

    def evaluate(ts: float, fwd_hours: float) -> float | None:
        btc_prices = data.closes(data.primary, ts, WINDOW)
        eth_prices = data.closes(data.secondary, ts, WINDOW)
        if len(btc_prices) < WINDOW or len(eth_prices) < WINDOW:
            return None

        ratios = []
        for b, e in zip(btc_prices, eth_prices):
            if b > 0:
                ratios.append(e / b)
        if len(ratios) < WINDOW // 2:
            return None

        mean_r = sum(ratios) / len(ratios)
        std_r = math.sqrt(sum((r - mean_r) ** 2 for r in ratios) / len(ratios))
        if std_r <= 0:
            return None

        z = (ratios[-1] - mean_r) / std_r

        if abs(z) < ENTRY_Z:
            return None

        # z > 2: ETH expensive vs BTC → long BTC
        direction = 1.0 if z > 0 else -1.0

        # Trade as spread: BTC return - ETH return
        btc_ret = data.forward_return(data.primary, ts, fwd_hours)
        eth_ret = data.forward_return(data.secondary, ts, fwd_hours)
        if btc_ret is None or eth_ret is None:
            return None

        spread_pnl = direction * (btc_ret - eth_ret)
        return spread_pnl - COMMISSION

    return evaluate


def make_funding_rv_cross(data: HistoricalData) -> StrategyEvaluator:
    """Funding rate relative value — Two Sigma/Wintermute framework.

    Find extreme funding divergence between two assets, trade convergence.
    """
    Z_THRESHOLD = 2.0
    MIN_ASSETS = 5

    def evaluate(ts: float, fwd_hours: float) -> float | None:
        rates = data.get_funding_at(ts)
        if len(rates) < MIN_ASSETS:
            return None

        values = list(rates.values())
        mean_rate = sum(values) / len(values)
        std_rate = math.sqrt(sum((v - mean_rate) ** 2 for v in values) / len(values))
        if std_rate <= 0:
            return None

        # BTC's z-score within the cross-section
        btc_rate = rates.get(data.primary)
        if btc_rate is None:
            return None

        z = (btc_rate - mean_rate) / std_rate

        if abs(z) < Z_THRESHOLD:
            return None

        # BTC funding is extreme positive → crowded longs → short BTC
        direction = -1.0 if z > 0 else 1.0

        fwd_ret = data.forward_return(data.primary, ts, fwd_hours)
        if fwd_ret is None:
            return None

        funding_collected = abs(btc_rate) * (fwd_hours / 8)
        return direction * fwd_ret + funding_collected - COMMISSION

    return evaluate


# ─────────────────────────────────────────────────────────────────────
# FAMILY 8: CRYPTO-NATIVE
# ─────────────────────────────────────────────────────────────────────

def make_liquidation_cascade(data: HistoricalData) -> StrategyEvaluator:
    """Liquidation cascade detector — Kou et al. (2023).

    Extreme funding + expanding vol = cascade imminent/in-progress.
    """
    FUNDING_PERCENTILE = 0.95
    VOL_RATIO_THRESHOLD = 1.5

    def evaluate(ts: float, fwd_hours: float) -> float | None:
        rates = data.get_funding_at(ts)
        btc_rate = rates.get(data.primary)
        if btc_rate is None:
            return None

        # Check if funding is extreme (use historical context)
        hist = data.funding_asset_history(data.primary, ts, 180)  # 30 days
        if len(hist) < 90:
            return None

        sorted_hist = sorted(abs(r) for r in hist)
        threshold = sorted_hist[int(len(sorted_hist) * FUNDING_PERCENTILE)]
        if abs(btc_rate) < threshold:
            return None  # not extreme enough

        # Check vol expansion
        vol_short = data.realized_vol(data.primary, ts, 6)   # 1 day
        vol_long = data.realized_vol(data.primary, ts, 42)    # 7 days
        if vol_short is None or vol_long is None or vol_long <= 0:
            return None

        if vol_short / vol_long < VOL_RATIO_THRESHOLD:
            return None  # vol not expanding

        # Cascade direction: positive funding = longs will be liquidated → short
        direction = -1.0 if btc_rate > 0 else 1.0

        fwd_ret = data.forward_return(data.primary, ts, fwd_hours)
        if fwd_ret is None:
            return None

        return direction * fwd_ret - COMMISSION

    return evaluate


def make_settlement_calendar(data: HistoricalData) -> StrategyEvaluator:
    """Funding settlement calendar effect — Baur et al. (2019).

    Predictable flow patterns around 8h funding settlement times.
    """
    SETTLEMENT_HOURS = {0, 8, 16}  # UTC funding settlements on most exchanges

    def evaluate(ts: float, fwd_hours: float) -> float | None:
        from datetime import datetime, timezone
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)

        # Only trade 2-4 hours before settlement
        hours_to_settlement = None
        for sh in SETTLEMENT_HOURS:
            diff = sh - dt.hour
            if diff < 0:
                diff += 24
            if 2 <= diff <= 4:
                hours_to_settlement = diff
                break

        if hours_to_settlement is None:
            return None

        rates = data.get_funding_at(ts)
        btc_rate = rates.get(data.primary)
        if btc_rate is None or abs(btc_rate) < 0.0001:
            return None  # need meaningful funding to trade

        # Before settlement: the paying side tends to unwind
        # Positive funding → longs will pay → they sell before settlement → price dips
        direction = -1.0 if btc_rate > 0 else 1.0

        fwd_ret = data.forward_return(data.primary, ts, fwd_hours)
        if fwd_ret is None:
            return None

        funding_collected = abs(btc_rate) * (fwd_hours / 8)
        return direction * fwd_ret + funding_collected - COMMISSION

    return evaluate


def make_beta_rotation(data: HistoricalData) -> StrategyEvaluator:
    """Altcoin beta rotation — Bianchi (2020), Liu et al. (2022).

    Estimate alt betas from funding co-movement. In BTC uptrends,
    overweight high-beta alts (via funding signal on BTC).
    """
    BETA_WINDOW = 42  # 7 days of funding for beta estimation
    MOM_LOOKBACK = 168  # 7 day momentum

    def evaluate(ts: float, fwd_hours: float) -> float | None:
        btc_mom = data.backward_return(data.primary, ts, MOM_LOOKBACK)
        if btc_mom is None or abs(btc_mom) < 0.01:
            return None  # need clear directional signal

        # Estimate betas from funding rate co-movement
        btc_hist = data.funding_asset_history(data.primary, ts, BETA_WINDOW)
        if len(btc_hist) < BETA_WINDOW // 2:
            return None

        # Use aggregate funding dispersion as a beta signal
        # High dispersion + BTC momentum = high-beta alts will amplify
        rates = data.get_funding_at(ts)
        if len(rates) < 5:
            return None

        values = list(rates.values())
        dispersion = math.sqrt(sum((v - sum(values) / len(values)) ** 2
                                   for v in values) / len(values))

        # In strong BTC trends + high beta dispersion: the move continues
        if dispersion < 0.00005:
            return None  # consensus, no beta divergence

        direction = 1.0 if btc_mom > 0 else -1.0
        # Scale by strength of momentum
        scale = min(abs(btc_mom) / 0.05, 2.0)

        fwd_ret = data.forward_return(data.primary, ts, fwd_hours)
        if fwd_ret is None:
            return None
        return direction * fwd_ret * scale - COMMISSION

    return evaluate


def make_funding_dispersion_overlay(data: HistoricalData) -> StrategyEvaluator:
    """Funding dispersion as vol predictor — Galaxy Digital framework.

    High dispersion → vol expansion → reduce directional exposure.
    Low dispersion → calm market → trend trades are more reliable.
    Combined with TSMOM for a complete strategy.
    """
    MOM_BARS = 42
    HIGH_DISPERSION = 0.0003
    LOW_DISPERSION = 0.00005

    def evaluate(ts: float, fwd_hours: float) -> float | None:
        # Base signal: TSMOM
        ret = data.backward_return(data.primary, ts, MOM_BARS * 4)
        if ret is None:
            return None

        direction = 1.0 if ret > 0 else -1.0

        # Overlay: size by inverse dispersion
        rates = data.get_funding_at(ts)
        if len(rates) < 5:
            return None

        values = list(rates.values())
        mean_r = sum(values) / len(values)
        dispersion = math.sqrt(sum((v - mean_r) ** 2 for v in values) / len(values))

        if dispersion > HIGH_DISPERSION:
            scale = 0.25  # high dispersion → incoming vol → small size
        elif dispersion < LOW_DISPERSION:
            scale = 1.5   # low dispersion → calm market → full size
        else:
            scale = 1.0

        fwd_ret = data.forward_return(data.primary, ts, fwd_hours)
        if fwd_ret is None:
            return None
        return direction * fwd_ret * scale - COMMISSION

    return evaluate


# ─────────────────────────────────────────────────────────────────────
# Registry — all 25 strategies
# ─────────────────────────────────────────────────────────────────────

ALL_STRATEGIES: dict[str, Callable[[HistoricalData], StrategyEvaluator]] = {
    # TSMOM
    "tsmom_classic": make_tsmom_classic,
    "tsmom_multi_horizon": make_tsmom_multi_horizon,
    "tsmom_adaptive": make_tsmom_adaptive,
    "tsmom_volscaled": make_tsmom_volscaled,
    # Cross-sectional
    "xsection_momentum": make_xsection_momentum,
    "52w_high_momentum": make_52w_high_momentum,
    # Carry / funding
    "funding_carry_voladj": make_funding_carry_voladj,
    "funding_surface_regime": make_funding_surface_regime,
    "funding_momentum_filter": make_funding_momentum_filter,
    "funding_term_structure": make_funding_term_structure,
    # Mean reversion
    "bollinger_reversion": make_bollinger_reversion,
    "rsi_regime_reversion": make_rsi_regime_reversion,
    "ou_pairs_btc_eth": make_ou_pairs,
    # Volatility
    "donchian_breakout": make_donchian_breakout,
    "volatility_squeeze": make_volatility_squeeze,
    "vol_term_structure": make_vol_term_structure,
    # Trend following
    "dual_ma_crossover": make_dual_ma_crossover,
    "kaufman_adaptive": make_kaufman_adaptive,
    "atr_breakout": make_atr_breakout,
    # Pairs / RV
    "btc_eth_ratio_rv": make_btc_eth_ratio,
    "funding_rv_cross": make_funding_rv_cross,
    # Crypto-native
    "liquidation_cascade": make_liquidation_cascade,
    "settlement_calendar": make_settlement_calendar,
    "beta_rotation": make_beta_rotation,
    "funding_dispersion_overlay": make_funding_dispersion_overlay,
}


def build_all_evaluators(data: HistoricalData) -> dict[str, StrategyEvaluator]:
    """Build all 25 strategy evaluators from a HistoricalData instance."""
    return {name: factory(data) for name, factory in ALL_STRATEGIES.items()}
