"""Strategy evaluators for analog scoring with real data.

Each evaluator takes (analog_timestamp, forward_hours) and returns the
P&L that strategy would have produced in that window, or None if it
wouldn't have traded.

These evaluators work against the backfilled candle + funding data stored
in memory (dicts keyed by timestamp). They simulate the actual strategy
logic from strategies/examples/ against historical conditions.

Usage::

    from analog.evaluators import build_evaluators

    evaluators = build_evaluators(candles, funding)
    scorer.register_strategy("funding_arb", evaluators["funding_arb"])
    scorer.register_strategy("trend_follow", evaluators["trend_follow"])
"""

from __future__ import annotations

import bisect
import math
from typing import Callable

import structlog

from analog.backfill import CandleData, FundingSnapshot

logger = structlog.get_logger()

# Type alias matching AnalogScorer's StrategyEvaluator
StrategyEvaluator = Callable[[float, float], float | None]


class HistoricalData:
    """Indexed historical data for fast lookups by timestamp.

    Builds sorted arrays and uses binary search for O(log n) lookups.

    The ``interval_hours`` parameter controls bar sizing. All bar-count
    parameters in strategy evaluators were calibrated for 4h bars.  Use
    ``b(n)`` to scale a 4h bar count to the current interval::

        data.realized_vol(asset, ts, data.b(42))  # always = 7 days
    """

    def __init__(
        self,
        candles: dict[str, list[CandleData]],
        funding: dict[str, list[FundingSnapshot]],
        primary_asset: str = "BTC",
        interval_hours: float = 4.0,
    ):
        self.primary = primary_asset
        self.secondary = "ETH" if primary_asset != "ETH" else "BTC"
        self.interval_hours = interval_hours
        # Scale factor: how many current-interval bars fit in one 4h bar
        self.bar_scale = 4.0 / interval_hours  # 1 for 4h, 16 for 15m
        self._bar_ms = int(interval_hours * 3600 * 1000)

        # Build sorted timestamp → index maps for candles
        self._candle_ts: dict[str, list[float]] = {}
        self._candle_data: dict[str, list[CandleData]] = {}
        for asset, clist in candles.items():
            sorted_c = sorted(clist, key=lambda c: c.timestamp_ms)
            self._candle_ts[asset] = [c.timestamp_ms / 1000.0 for c in sorted_c]
            self._candle_data[asset] = sorted_c

        # Build sorted funding data per asset, aligned to bars
        self._funding_by_bar: dict[int, dict[str, float]] = {}
        for asset, flist in funding.items():
            for f in flist:
                bar_ms = (f.timestamp_ms // self._bar_ms) * self._bar_ms
                if bar_ms not in self._funding_by_bar:
                    self._funding_by_bar[bar_ms] = {}
                # Average if multiple funding snapshots per bar
                if asset in self._funding_by_bar[bar_ms]:
                    self._funding_by_bar[bar_ms][asset] = (
                        self._funding_by_bar[bar_ms][asset] + f.rate
                    ) / 2
                else:
                    self._funding_by_bar[bar_ms][asset] = f.rate

    def b(self, n_4h_bars: int) -> int:
        """Scale a 4h-calibrated bar count to the current interval.

        ``data.b(42)`` returns 42 at 4h, 672 at 15m — always 7 days.
        """
        return max(1, int(n_4h_bars * self.bar_scale))

    def get_candle_at(self, asset: str, ts: float) -> CandleData | None:
        """Find the candle closest to timestamp ts (within one bar interval)."""
        timestamps = self._candle_ts.get(asset)
        if not timestamps:
            return None
        idx = bisect.bisect_left(timestamps, ts)
        # Check both neighbors
        best_idx = None
        best_diff = float("inf")
        for candidate in [max(0, idx - 1), min(idx, len(timestamps) - 1)]:
            diff = abs(timestamps[candidate] - ts)
            if diff < best_diff:
                best_diff = diff
                best_idx = candidate
        tolerance = self.interval_hours * 3600  # within one bar
        if best_idx is not None and best_diff < tolerance:
            return self._candle_data[asset][best_idx]
        return None

    def get_candle_index(self, asset: str, ts: float) -> int | None:
        """Get the index of the candle closest to ts."""
        timestamps = self._candle_ts.get(asset)
        if not timestamps:
            return None
        idx = bisect.bisect_left(timestamps, ts)
        if idx >= len(timestamps):
            idx = len(timestamps) - 1
        if idx > 0 and abs(timestamps[idx - 1] - ts) < abs(timestamps[idx] - ts):
            idx = idx - 1
        if abs(timestamps[idx] - ts) < self.interval_hours * 3600:
            return idx
        return None

    def forward_return(self, asset: str, ts: float, forward_hours: float) -> float | None:
        """Get forward return from ts over forward_hours."""
        idx = self.get_candle_index(asset, ts)
        if idx is None:
            return None
        forward_bars = max(1, int(forward_hours / self.interval_hours))
        candles = self._candle_data[asset]
        if idx + forward_bars >= len(candles):
            return None
        entry = candles[idx].close
        exit_ = candles[idx + forward_bars].close
        if entry <= 0:
            return None
        return (exit_ - entry) / entry

    def get_funding_at(self, ts: float) -> dict[str, float]:
        """Get funding rates for all assets at the bar containing ts."""
        bar_ms = int((ts * 1000) // self._bar_ms) * self._bar_ms
        return self._funding_by_bar.get(bar_ms, {})

    def get_recent_candles(self, asset: str, ts: float, n_bars: int) -> list[CandleData]:
        """Get the n_bars candles ending at or before ts."""
        idx = self.get_candle_index(asset, ts)
        if idx is None:
            return []
        start = max(0, idx - n_bars + 1)
        return self._candle_data[asset][start:idx + 1]

    def realized_vol(self, asset: str, ts: float, n_bars: int) -> float | None:
        """Realized vol (std of log returns) over n_bars ending at ts."""
        candles = self.get_recent_candles(asset, ts, n_bars + 1)
        if len(candles) < n_bars + 1:
            return None
        log_rets = []
        for i in range(1, len(candles)):
            if candles[i - 1].close > 0 and candles[i].close > 0:
                log_rets.append(math.log(candles[i].close / candles[i - 1].close))
        if len(log_rets) < 2:
            return None
        mean = sum(log_rets) / len(log_rets)
        var = sum((r - mean) ** 2 for r in log_rets) / len(log_rets)
        return math.sqrt(var)

    def closes(self, asset: str, ts: float, n_bars: int) -> list[float]:
        """Get the last n_bars close prices ending at or before ts."""
        candles = self.get_recent_candles(asset, ts, n_bars)
        return [c.close for c in candles]

    def backward_return(self, asset: str, ts: float, lookback_hours: float) -> float | None:
        """Return over the past lookback_hours ending at ts."""
        return self.forward_return(asset, ts - lookback_hours * 3600, lookback_hours)

    def sma(self, asset: str, ts: float, period: int) -> float | None:
        """Simple moving average of close prices over `period` bars."""
        prices = self.closes(asset, ts, period)
        if len(prices) < period:
            return None
        return sum(prices) / len(prices)

    def ema(self, asset: str, ts: float, period: int) -> float | None:
        """Exponential moving average of close prices."""
        prices = self.closes(asset, ts, period * 2)  # need extra for warmup
        if len(prices) < period:
            return None
        alpha = 2.0 / (period + 1)
        ema_val = prices[0]
        for p in prices[1:]:
            ema_val = alpha * p + (1 - alpha) * ema_val
        return ema_val

    def rsi(self, asset: str, ts: float, period: int = 14) -> float | None:
        """Relative Strength Index."""
        candles = self.get_recent_candles(asset, ts, period + 2)
        if len(candles) < period + 1:
            return None
        gains = []
        losses = []
        for i in range(1, len(candles)):
            diff = candles[i].close - candles[i - 1].close
            if diff > 0:
                gains.append(diff)
                losses.append(0.0)
            else:
                gains.append(0.0)
                losses.append(abs(diff))
        if not gains:
            return 50.0
        avg_gain = sum(gains[-period:]) / period
        avg_loss = sum(losses[-period:]) / period
        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return 100.0 - (100.0 / (1.0 + rs))

    def bollinger(self, asset: str, ts: float, period: int = 20, mult: float = 2.0
                  ) -> tuple[float, float, float] | None:
        """Bollinger Bands: (lower, middle, upper)."""
        prices = self.closes(asset, ts, period)
        if len(prices) < period:
            return None
        mid = sum(prices) / len(prices)
        var = sum((p - mid) ** 2 for p in prices) / len(prices)
        std = math.sqrt(var)
        return (mid - mult * std, mid, mid + mult * std)

    def atr(self, asset: str, ts: float, period: int) -> float | None:
        """Average True Range over `period` bars."""
        candles = self.get_recent_candles(asset, ts, period + 1)
        if len(candles) < period + 1:
            return None
        trs = []
        for i in range(1, len(candles)):
            tr = max(
                candles[i].high - candles[i].low,
                abs(candles[i].high - candles[i - 1].close),
                abs(candles[i].low - candles[i - 1].close),
            )
            trs.append(tr)
        return sum(trs) / len(trs) if trs else None

    def donchian(self, asset: str, ts: float, period: int
                 ) -> tuple[float, float] | None:
        """Donchian Channel: (lowest low, highest high) over period bars."""
        candles = self.get_recent_candles(asset, ts, period)
        if len(candles) < period:
            return None
        return (min(c.low for c in candles), max(c.high for c in candles))

    def keltner(self, asset: str, ts: float, period: int = 20, mult: float = 1.5
                ) -> tuple[float, float, float] | None:
        """Keltner Channel: (lower, middle, upper)."""
        ema_val = self.ema(asset, ts, period)
        atr_val = self.atr(asset, ts, period)
        if ema_val is None or atr_val is None:
            return None
        return (ema_val - mult * atr_val, ema_val, ema_val + mult * atr_val)

    def simulate_exit(
        self,
        asset: str,
        entry_ts: float,
        direction: float,  # +1.0 for long, -1.0 for short
        stop_loss: float | None = None,    # e.g., 0.03 = 3% stop
        take_profit: float | None = None,  # e.g., 0.05 = 5% TP
        trailing_stop: float | None = None,  # e.g., 0.02 = 2% trail from peak
        max_hold_hours: float = 48.0,      # max hold time before forced exit
    ) -> tuple[float, float, str] | None:
        """Walk forward bar-by-bar from entry, checking exit conditions.

        Returns (pnl_pct, hold_hours, exit_reason) or None if no entry candle found.
        exit_reason is one of: "stop_loss", "take_profit", "trailing_stop", "time_exit"
        """
        COMMISSION = 0.0006

        entry_idx = self.get_candle_index(asset, entry_ts)
        if entry_idx is None:
            return None

        candles = self._candle_data[asset]
        entry_price = candles[entry_idx].close
        if entry_price <= 0:
            return None

        max_bars = max(1, int(max_hold_hours / self.interval_hours))
        end_idx = min(entry_idx + max_bars, len(candles) - 1)

        if entry_idx + 1 > end_idx:
            return None

        peak_pnl = 0.0  # best unrealized P&L so far (for trailing stop)

        for i in range(entry_idx + 1, end_idx + 1):
            candle = candles[i]
            hold_hours = (i - entry_idx) * self.interval_hours

            # --- Check stop-loss using intra-bar extremes ---
            if stop_loss is not None:
                if direction > 0:
                    # Long: stopped if low dips enough
                    worst_pnl = (candle.low - entry_price) / entry_price
                else:
                    # Short: stopped if high rises enough
                    worst_pnl = -(candle.high - entry_price) / entry_price

                if worst_pnl <= -stop_loss:
                    exit_pnl = -stop_loss - COMMISSION
                    return (exit_pnl, hold_hours, "stop_loss")

            # --- Check take-profit using intra-bar extremes ---
            if take_profit is not None:
                if direction > 0:
                    best_pnl_bar = (candle.high - entry_price) / entry_price
                else:
                    best_pnl_bar = -(candle.low - entry_price) / entry_price

                if best_pnl_bar >= take_profit:
                    exit_pnl = take_profit - COMMISSION
                    return (exit_pnl, hold_hours, "take_profit")

            # --- Update peak P&L for trailing stop ---
            if direction > 0:
                bar_peak = (candle.high - entry_price) / entry_price
            else:
                bar_peak = -(candle.low - entry_price) / entry_price

            peak_pnl = max(peak_pnl, bar_peak)

            # --- Check trailing stop ---
            if trailing_stop is not None and peak_pnl > 0:
                # Check if price retraced trailing_stop from peak within this bar
                if direction > 0:
                    # For longs, the worst point in this bar is the low
                    bar_worst = (candle.low - entry_price) / entry_price
                else:
                    # For shorts, the worst point is the high
                    bar_worst = -(candle.high - entry_price) / entry_price

                if peak_pnl - bar_worst >= trailing_stop:
                    # Trailing stop triggered; exit at peak - trail
                    exit_pnl = peak_pnl - trailing_stop - COMMISSION
                    return (exit_pnl, hold_hours, "trailing_stop")

        # --- Time exit: close at last bar ---
        last_candle = candles[end_idx]
        if direction > 0:
            final_pnl = (last_candle.close - entry_price) / entry_price
        else:
            final_pnl = -(last_candle.close - entry_price) / entry_price

        final_hold = (end_idx - entry_idx) * self.interval_hours
        return (final_pnl - COMMISSION, final_hold, "time_exit")

    def funding_window(self, ts: float, n_bars: int) -> list[dict[str, float]]:
        """Get funding rate snapshots for the last n_bars."""
        result = []
        bar_ms_now = int((ts * 1000) // self._bar_ms) * self._bar_ms
        for i in range(n_bars):
            bar_ms = bar_ms_now - i * self._bar_ms
            rates = self._funding_by_bar.get(bar_ms, {})
            if rates:
                result.append(rates)
        result.reverse()  # chronological order
        return result

    def funding_asset_history(self, asset: str, ts: float, n_bars: int) -> list[float]:
        """Get funding rate history for one asset over n_bars."""
        result = []
        bar_ms_now = int((ts * 1000) // self._bar_ms) * self._bar_ms
        for i in range(n_bars):
            bar_ms = bar_ms_now - i * self._bar_ms
            rates = self._funding_by_bar.get(bar_ms, {})
            if asset in rates:
                result.append(rates[asset])
        result.reverse()
        return result

    def rolling_beta(self, asset: str, benchmark: str, ts: float, n_bars: int = 42
                     ) -> float | None:
        """Rolling beta of `asset` vs `benchmark` over n_bars.

        beta = cov(asset, benchmark) / var(benchmark)
        """
        asset_candles = self.get_recent_candles(asset, ts, n_bars + 1)
        bench_candles = self.get_recent_candles(benchmark, ts, n_bars + 1)
        if len(asset_candles) < n_bars + 1 or len(bench_candles) < n_bars + 1:
            return None

        a_rets = []
        b_rets = []
        for i in range(1, min(len(asset_candles), len(bench_candles))):
            if (asset_candles[i - 1].close > 0 and asset_candles[i].close > 0 and
                    bench_candles[i - 1].close > 0 and bench_candles[i].close > 0):
                a_rets.append(math.log(asset_candles[i].close / asset_candles[i - 1].close))
                b_rets.append(math.log(bench_candles[i].close / bench_candles[i - 1].close))

        if len(a_rets) < 10:
            return None

        mean_a = sum(a_rets) / len(a_rets)
        mean_b = sum(b_rets) / len(b_rets)
        cov = sum((a - mean_a) * (b - mean_b) for a, b in zip(a_rets, b_rets)) / len(a_rets)
        var_b = sum((b - mean_b) ** 2 for b in b_rets) / len(b_rets)

        if var_b <= 0:
            return None
        return cov / var_b

    def residual_return(self, asset: str, benchmark: str, ts: float,
                        forward_hours: float) -> float | None:
        """Forward return of `asset` minus beta * forward return of `benchmark`.

        This is the idiosyncratic return — the part NOT explained by BTC moves.
        """
        beta = self.rolling_beta(asset, benchmark, ts)
        if beta is None:
            return None

        asset_ret = self.forward_return(asset, ts, forward_hours)
        bench_ret = self.forward_return(benchmark, ts, forward_hours)
        if asset_ret is None or bench_ret is None:
            return None

        return asset_ret - beta * bench_ret

    def btc_signal(self, ts: float, lookback_hours: float) -> float | None:
        """BTC momentum as a leading indicator for alts."""
        return self.backward_return("BTC", ts, lookback_hours)


# --- Strategy Evaluators ---

def _make_funding_arb(data: HistoricalData) -> StrategyEvaluator:
    """Funding arbitrage: short when funding very positive, long when very negative.

    Collects funding payment + directional P&L. Models the actual
    FundingArbStrategy logic.
    """
    THRESHOLD = 0.0003  # 0.03% per period = significant for Binance 8h rates
    COMMISSION = 0.0006  # 0.03% each way round-trip

    def evaluate(analog_ts: float, forward_hours: float) -> float | None:
        rates = data.get_funding_at(analog_ts)
        btc_rate = rates.get(data.primary)
        if btc_rate is None or abs(btc_rate) < THRESHOLD:
            return None  # wouldn't trade

        fwd_ret = data.forward_return(data.primary, analog_ts, forward_hours)
        if fwd_ret is None:
            return None

        # Short when longs are paying (positive funding), long when shorts pay
        direction = -1.0 if btc_rate > 0 else 1.0

        # P&L = directional return + funding collected - commission
        # Funding collected = we're on the receiving side
        directional_pnl = direction * fwd_ret
        funding_collected = abs(btc_rate) * (forward_hours / 8)  # scale to period
        pnl = directional_pnl + funding_collected - COMMISSION

        return pnl

    return evaluate


def _make_multi_asset_funding(data: HistoricalData) -> StrategyEvaluator:
    """Multi-asset funding arb: harvest extreme funding across multiple assets.

    This is the funding *surface* strategy — looks for the best opportunity
    across all assets, not just BTC.
    """
    THRESHOLD = 0.0005  # higher threshold for best-of-N selection
    COMMISSION = 0.0006

    def evaluate(analog_ts: float, forward_hours: float) -> float | None:
        rates = data.get_funding_at(analog_ts)
        if not rates:
            return None

        # Find asset with most extreme funding
        best_asset = None
        best_rate = 0.0
        for asset, rate in rates.items():
            if abs(rate) > abs(best_rate) and abs(rate) >= THRESHOLD:
                best_asset = asset
                best_rate = rate

        if best_asset is None:
            return None

        fwd_ret = data.forward_return(best_asset, analog_ts, forward_hours)
        if fwd_ret is None:
            # Fall back to BTC if the specific asset doesn't have candle data
            fwd_ret = data.forward_return(data.primary, analog_ts, forward_hours)
            if fwd_ret is None:
                return None

        direction = -1.0 if best_rate > 0 else 1.0
        pnl = direction * fwd_ret + abs(best_rate) * (forward_hours / 8) - COMMISSION
        return pnl

    return evaluate


def _make_trend_follow(data: HistoricalData) -> StrategyEvaluator:
    """Trend following: go with momentum over the last 1-7 days.

    Uses a simple dual-timeframe confirmation: 1d and 7d momentum must agree.
    """
    MIN_MOMENTUM_1D = 0.005  # 0.5% in 1 day
    MIN_MOMENTUM_7D = 0.02   # 2% in 7 days
    COMMISSION = 0.0006

    def evaluate(analog_ts: float, forward_hours: float) -> float | None:
        # 1d momentum (6 bars at 4h)
        ret_1d = data.forward_return(data.primary, analog_ts - 6 * 4 * 3600, 24)
        # 7d momentum (42 bars at 4h)
        ret_7d = data.forward_return(data.primary, analog_ts - 42 * 4 * 3600, 168)

        if ret_1d is None or ret_7d is None:
            return None

        # Both timeframes must agree on direction
        if abs(ret_1d) < MIN_MOMENTUM_1D or abs(ret_7d) < MIN_MOMENTUM_7D:
            return None
        if (ret_1d > 0) != (ret_7d > 0):
            return None  # conflicting signals

        direction = 1.0 if ret_1d > 0 else -1.0
        fwd_ret = data.forward_return(data.primary, analog_ts, forward_hours)
        if fwd_ret is None:
            return None

        return direction * fwd_ret - COMMISSION

    return evaluate


def _make_mean_reversion(data: HistoricalData) -> StrategyEvaluator:
    """Mean reversion: fade extended moves when vol is elevated.

    Requires: large 1d move + elevated volatility (suggesting overextension,
    not a new trend).
    """
    MOVE_THRESHOLD = 0.03  # 3% in 1 day
    COMMISSION = 0.0006

    def evaluate(analog_ts: float, forward_hours: float) -> float | None:
        ret_1d = data.forward_return(data.primary, analog_ts - 6 * 4 * 3600, 24)
        if ret_1d is None or abs(ret_1d) < MOVE_THRESHOLD:
            return None

        # Check volatility is elevated
        vol = data.realized_vol(data.primary, analog_ts, data.b(42))  # 7d vol
        if vol is None:
            return None

        # Simple percentile check: we need vol to be relatively high
        vol_long = data.realized_vol(data.primary, analog_ts, data.b(180))  # 30d vol
        if vol_long is None or vol_long <= 0:
            return None

        vol_ratio = vol / vol_long
        if vol_ratio < 1.2:  # vol not elevated enough
            return None

        # Fade the move
        direction = -1.0 if ret_1d > 0 else 1.0
        fwd_ret = data.forward_return(data.primary, analog_ts, forward_hours)
        if fwd_ret is None:
            return None

        return direction * fwd_ret - COMMISSION

    return evaluate


def _make_breakout(data: HistoricalData) -> StrategyEvaluator:
    """Breakout: trade range expansions after compression.

    When range compression (ATR short / ATR long < threshold) resolves
    with a directional move, follow it.
    """
    COMPRESSION_THRESHOLD = 0.7  # short ATR < 70% of long ATR
    BREAKOUT_THRESHOLD = 0.01  # 1% move in 4h to confirm breakout
    COMMISSION = 0.0006

    def evaluate(analog_ts: float, forward_hours: float) -> float | None:
        n_short = data.b(7)   # ~1d
        n_long = data.b(43)   # ~7d
        candles_short = data.get_recent_candles(data.primary, analog_ts, n_short)
        candles_long = data.get_recent_candles(data.primary, analog_ts, n_long)

        if len(candles_short) < n_short or len(candles_long) < n_long:
            return None

        # ATR short
        trs_short = []
        for i in range(1, len(candles_short)):
            tr = max(
                candles_short[i].high - candles_short[i].low,
                abs(candles_short[i].high - candles_short[i - 1].close),
                abs(candles_short[i].low - candles_short[i - 1].close),
            )
            trs_short.append(tr)
        atr_short = sum(trs_short) / len(trs_short) if trs_short else 0

        # ATR long
        trs_long = []
        for i in range(1, len(candles_long)):
            tr = max(
                candles_long[i].high - candles_long[i].low,
                abs(candles_long[i].high - candles_long[i - 1].close),
                abs(candles_long[i].low - candles_long[i - 1].close),
            )
            trs_long.append(tr)
        atr_long = sum(trs_long) / len(trs_long) if trs_long else 0

        if atr_long <= 0:
            return None

        compression = atr_short / atr_long
        if compression > COMPRESSION_THRESHOLD:
            return None  # not compressed enough

        # Check for breakout in the current bar
        ret_4h = data.forward_return(data.primary, analog_ts - data.interval_hours * 3600, data.interval_hours)
        if ret_4h is None or abs(ret_4h) < BREAKOUT_THRESHOLD:
            return None

        direction = 1.0 if ret_4h > 0 else -1.0
        fwd_ret = data.forward_return(data.primary, analog_ts, forward_hours)
        if fwd_ret is None:
            return None

        return direction * fwd_ret - COMMISSION

    return evaluate


def build_evaluators(
    candles: dict[str, list[CandleData]],
    funding: dict[str, list[FundingSnapshot]],
    asset: str = "BTC",
    interval_hours: float = 4.0,
) -> dict[str, StrategyEvaluator]:
    """Build all strategy evaluators from backfilled data.

    Args:
        candles: OHLCV data per asset.
        funding: Funding snapshots per asset.
        asset: Which asset to trade. Strategies use this as the primary asset.
        interval_hours: Bar interval in hours (4.0 for 4h, 0.25 for 15m).

    Returns a dict of name → evaluator suitable for AnalogScorer.register_strategy().
    Includes the original 5 + all 25 established + 9 contrarian strategies.
    """
    from analog.strategies import build_all_evaluators
    from analog.contrarian import build_contrarian_evaluators
    from analog.beta_strategies import build_beta_evaluators
    from analog.lead_lag import build_lead_lag_evaluators

    data = HistoricalData(candles, funding, primary_asset=asset,
                          interval_hours=interval_hours)

    # Original 5 (kept for backwards compatibility)
    original = {
        "funding_arb": _make_funding_arb(data),
        "multi_asset_funding": _make_multi_asset_funding(data),
        "trend_follow": _make_trend_follow(data),
        "mean_reversion": _make_mean_reversion(data),
        "breakout": _make_breakout(data),
    }

    # 25 established strategies
    established = build_all_evaluators(data)

    # 9 contrarian strategies (flipped signals, consensus fade, etc.)
    contrarian = build_contrarian_evaluators(data)

    # 8 beta-adjusted strategies (residual, BTC-led, beta-relative)
    beta = build_beta_evaluators(data)

    # 8 lead-lag strategies (BTC/ETH lead, alts follow)
    lead_lag = build_lead_lag_evaluators(data)

    return {**original, **established, **contrarian, **beta, **lead_lag}


def build_multi_asset_evaluators(
    candles: dict[str, list[CandleData]],
    funding: dict[str, list[FundingSnapshot]],
    assets: list[str] | None = None,
    interval_hours: float = 4.0,
) -> dict[str, StrategyEvaluator]:
    """Build evaluators for ALL assets, with asset prefix in names.

    Returns e.g. {"BTC:mean_reversion": ..., "ETH:mean_reversion": ..., "SOL:funding_arb": ...}
    """
    if assets is None:
        assets = sorted(candles.keys())

    all_evals: dict[str, StrategyEvaluator] = {}
    for asset in assets:
        if asset not in candles or len(candles[asset]) < 200:
            continue  # skip assets without enough data
        asset_evals = build_evaluators(candles, funding, asset=asset,
                                       interval_hours=interval_hours)
        for name, evaluator in asset_evals.items():
            all_evals[f"{asset}:{name}"] = evaluator

    return all_evals
