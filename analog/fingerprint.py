"""Market state fingerprinting.

Computes a dense, continuous feature vector representing the current market
state across multiple timeframes. Unlike discrete regime labels, fingerprints
preserve the full information — a market that's 51% "bull" and one that's 99%
"bull" produce different vectors instead of collapsing to the same label.

Features are normalized to z-scores or percentiles so they're comparable
across time and suitable for distance-based similarity search.

Usage::

    engine = FingerprintEngine()

    # Feed OHLCV data as it arrives
    engine.update_candles("BTC", candles_4h)
    engine.update_candles("ETH", candles_4h)

    # Optionally feed funding surface features
    engine.update_funding(surface_engine.features())

    # Compute fingerprint
    fp = engine.compute()
    # fp.vector = {"btc_return_4h": -0.012, "realized_vol_1d_pct": 0.82, ...}
"""

from __future__ import annotations

import math
import time
from collections import deque
from dataclasses import dataclass, field

import structlog

logger = structlog.get_logger()


@dataclass
class Fingerprint:
    """A point-in-time market state fingerprint."""

    timestamp: float
    vector: dict[str, float]  # feature_name → value (normalized)

    def to_list(self, feature_order: list[str]) -> list[float]:
        """Convert to ordered list for distance computation."""
        return [self.vector.get(f, 0.0) for f in feature_order]

    def to_dict(self) -> dict[str, float | str]:
        """Serialize for storage."""
        out: dict[str, float | str] = {"timestamp": self.timestamp}
        out.update(self.vector)
        return out


@dataclass
class _AssetHistory:
    """Rolling price history for one asset."""

    closes: deque[float] = field(default_factory=lambda: deque(maxlen=500))
    highs: deque[float] = field(default_factory=lambda: deque(maxlen=500))
    lows: deque[float] = field(default_factory=lambda: deque(maxlen=500))
    volumes: deque[float] = field(default_factory=lambda: deque(maxlen=500))
    timestamps: deque[float] = field(default_factory=lambda: deque(maxlen=500))


def _returns(prices: list[float], period: int) -> float | None:
    """Log return over `period` bars. None if insufficient data."""
    if len(prices) <= period:
        return None
    return math.log(prices[-1] / prices[-1 - period]) if prices[-1 - period] > 0 else None


def _realized_vol(prices: list[float], period: int) -> float | None:
    """Realized volatility (std of log returns) over `period` bars."""
    if len(prices) <= period:
        return None
    log_returns = []
    for i in range(-period, 0):
        if prices[i - 1] > 0 and prices[i] > 0:
            log_returns.append(math.log(prices[i] / prices[i - 1]))
    if len(log_returns) < 2:
        return None
    mean = sum(log_returns) / len(log_returns)
    var = sum((r - mean) ** 2 for r in log_returns) / len(log_returns)
    return math.sqrt(var)


def _percentile_rank(value: float, history: list[float]) -> float:
    """Rank value as percentile [0, 1] within history."""
    if not history:
        return 0.5
    below = sum(1 for h in history if h < value)
    return below / len(history)


def _atr(highs: list[float], lows: list[float], closes: list[float], period: int) -> float | None:
    """Average true range over `period` bars."""
    if len(highs) <= period or len(lows) <= period or len(closes) <= period:
        return None
    trs = []
    for i in range(-period, 0):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        trs.append(tr)
    return sum(trs) / len(trs) if trs else None


class FingerprintEngine:
    """Computes market state fingerprints from OHLCV data + funding surface.

    The engine maintains rolling history for configured assets and produces
    a normalized feature vector on demand.

    Args:
        primary_asset: The main asset for price structure features (default "BTC").
        secondary_asset: Secondary asset for relative strength (default "ETH").
        interval_hours: Bar interval in hours (default 4).
    """

    # Return horizons in bars (for a 4h bar: 1=4h, 6=1d, 42=7d, 180=30d)
    RETURN_HORIZONS = {
        "4h": 1,
        "1d": 6,
        "7d": 42,
        "30d": 180,
    }

    # Volatility windows in bars
    VOL_WINDOWS = {
        "1d": 6,
        "7d": 42,
        "30d": 180,
    }

    def __init__(
        self,
        primary_asset: str = "BTC",
        secondary_asset: str = "ETH",
        interval_hours: float = 4.0,
    ):
        self.primary_asset = primary_asset
        self.secondary_asset = secondary_asset
        self.interval_hours = interval_hours
        self._assets: dict[str, _AssetHistory] = {}
        self._funding_features: dict[str, float] = {}
        # Rolling vol history for percentile ranking
        self._vol_history: deque[float] = deque(maxlen=500)

    def update_candles(
        self,
        asset: str,
        closes: list[float],
        highs: list[float] | None = None,
        lows: list[float] | None = None,
        volumes: list[float] | None = None,
        timestamps: list[float] | None = None,
    ) -> None:
        """Feed OHLCV data for an asset. Appends to rolling history.

        For initial backfill, pass the full history. For live updates, pass
        just the latest bar(s).
        """
        if asset not in self._assets:
            self._assets[asset] = _AssetHistory()

        hist = self._assets[asset]
        hist.closes.extend(closes)
        if highs:
            hist.highs.extend(highs)
        if lows:
            hist.lows.extend(lows)
        if volumes:
            hist.volumes.extend(volumes)
        if timestamps:
            hist.timestamps.extend(timestamps)

    def update_funding(self, features: dict[str, float]) -> None:
        """Update funding surface features (from FundingSurfaceEngine.features())."""
        self._funding_features = features

    def compute(self, timestamp: float | None = None) -> Fingerprint:
        """Compute the current market fingerprint.

        Returns a Fingerprint with all available features. Missing data
        produces 0.0 for those features rather than raising.
        """
        ts = timestamp or time.time()
        vector: dict[str, float] = {}

        # --- Price returns (multi-timeframe) ---
        for asset_key, asset_name in [("btc", self.primary_asset), ("eth", self.secondary_asset)]:
            hist = self._assets.get(asset_name)
            if hist is None or len(hist.closes) < 2:
                for horizon in self.RETURN_HORIZONS:
                    vector[f"{asset_key}_return_{horizon}"] = 0.0
                continue

            closes = list(hist.closes)
            for horizon_name, bars in self.RETURN_HORIZONS.items():
                ret = _returns(closes, bars)
                vector[f"{asset_key}_return_{horizon_name}"] = round(ret, 6) if ret is not None else 0.0

        # --- Volatility structure (primary asset) ---
        btc_hist = self._assets.get(self.primary_asset)
        if btc_hist and len(btc_hist.closes) > 6:
            closes = list(btc_hist.closes)
            for vol_name, bars in self.VOL_WINDOWS.items():
                vol = _realized_vol(closes, bars)
                if vol is not None:
                    vector[f"realized_vol_{vol_name}"] = round(vol, 6)
                    # Percentile rank vs trailing history
                    self._vol_history.append(vol)
                    pct = _percentile_rank(vol, list(self._vol_history))
                    vector[f"realized_vol_{vol_name}_pct"] = round(pct, 4)
                else:
                    vector[f"realized_vol_{vol_name}"] = 0.0
                    vector[f"realized_vol_{vol_name}_pct"] = 0.5

            # Vol term structure: ratio of short to long vol
            vol_1d = _realized_vol(closes, 6)
            vol_7d = _realized_vol(closes, 42)
            if vol_1d is not None and vol_7d is not None and vol_7d > 0:
                vector["vol_term_structure"] = round(vol_1d / vol_7d, 4)
            else:
                vector["vol_term_structure"] = 1.0

            # ATR-based range compression
            if btc_hist.highs and btc_hist.lows:
                highs = list(btc_hist.highs)
                lows = list(btc_hist.lows)
                atr_short = _atr(highs, lows, closes, 6)
                atr_long = _atr(highs, lows, closes, 42)
                if atr_short is not None and atr_long is not None and atr_long > 0:
                    vector["range_compression"] = round(atr_short / atr_long, 4)
                else:
                    vector["range_compression"] = 1.0
        else:
            for vol_name in self.VOL_WINDOWS:
                vector[f"realized_vol_{vol_name}"] = 0.0
                vector[f"realized_vol_{vol_name}_pct"] = 0.5
            vector["vol_term_structure"] = 1.0
            vector["range_compression"] = 1.0

        # --- Cross-asset relative strength ---
        btc_closes = list(self._assets[self.primary_asset].closes) if self.primary_asset in self._assets else []
        eth_closes = list(self._assets[self.secondary_asset].closes) if self.secondary_asset in self._assets else []
        if len(btc_closes) > 42 and len(eth_closes) > 42 and btc_closes[-1] > 0:
            ratio_now = eth_closes[-1] / btc_closes[-1]
            ratio_7d = eth_closes[-42] / btc_closes[-42] if btc_closes[-42] > 0 else ratio_now
            vector["eth_btc_ratio_change_7d"] = round(ratio_now - ratio_7d, 6) if ratio_7d > 0 else 0.0
        else:
            vector["eth_btc_ratio_change_7d"] = 0.0

        # --- Volume features (primary asset) ---
        if btc_hist and len(btc_hist.volumes) > 42:
            vols = list(btc_hist.volumes)
            recent_avg = sum(vols[-6:]) / 6  # 1d avg
            longer_avg = sum(vols[-42:]) / 42  # 7d avg
            if longer_avg > 0:
                vector["volume_ratio_1d_7d"] = round(recent_avg / longer_avg, 4)
            else:
                vector["volume_ratio_1d_7d"] = 1.0
        else:
            vector["volume_ratio_1d_7d"] = 1.0

        # --- Funding surface features ---
        for k, v in self._funding_features.items():
            vector[k] = round(v, 8) if isinstance(v, float) else v

        return Fingerprint(timestamp=ts, vector=vector)

    @property
    def feature_names(self) -> list[str]:
        """Ordered list of all feature names this engine produces.

        Useful for consistent vector ordering in distance computations.
        """
        # Compute a dummy to discover all keys
        fp = self.compute(timestamp=0.0)
        return sorted(fp.vector.keys())
