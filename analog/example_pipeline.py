"""Example: end-to-end analog memory pipeline.

Shows how to wire up fingerprinting → storage → analog search → strategy
scoring into a complete decision loop. This is the template for building
the live system.

Run with::

    python -m analog.example_pipeline
"""

from __future__ import annotations

import math
import random
import time

from analog.fingerprint import FingerprintEngine
from analog.surface import FundingSurfaceEngine
from analog.store import FingerprintStore
from analog.finder import AnalogFinder
from analog.scorer import AnalogScorer


def _generate_synthetic_data(
    n_bars: int = 2000,
    interval_hours: float = 4.0,
) -> tuple[list[float], list[float], list[float], list[float], list[float], list[dict[str, float]]]:
    """Generate synthetic BTC price data + funding snapshots for demo.

    Returns: (btc_closes, btc_highs, btc_lows, eth_closes, timestamps, funding_snapshots)
    """
    random.seed(42)
    start_ts = time.time() - (n_bars * interval_hours * 3600)

    btc_price = 50000.0
    eth_price = 3000.0
    btc_closes, btc_highs, btc_lows = [], [], []
    eth_closes = []
    timestamps = []
    funding_snapshots = []

    # Simple regime-switching random walk
    regime = "bull"
    regime_counter = 0

    for i in range(n_bars):
        ts = start_ts + i * interval_hours * 3600

        # Switch regime occasionally
        regime_counter += 1
        if regime_counter > random.randint(50, 150):
            regime = random.choice(["bull", "bear", "chop"])
            regime_counter = 0

        # Price dynamics depend on regime
        if regime == "bull":
            btc_drift = 0.0003
            vol = 0.015
        elif regime == "bear":
            btc_drift = -0.0003
            vol = 0.02
        else:
            btc_drift = 0.0
            vol = 0.01

        btc_ret = btc_drift + vol * random.gauss(0, 1)
        btc_price *= math.exp(btc_ret)

        # ETH loosely follows BTC with its own noise
        eth_ret = btc_ret * 1.2 + 0.005 * random.gauss(0, 1)
        eth_price *= math.exp(eth_ret)

        high = btc_price * (1 + abs(random.gauss(0, vol * 0.5)))
        low = btc_price * (1 - abs(random.gauss(0, vol * 0.5)))

        btc_closes.append(btc_price)
        btc_highs.append(high)
        btc_lows.append(low)
        eth_closes.append(eth_price)
        timestamps.append(ts)

        # Funding rates: correlated with recent return direction
        assets = ["BTC", "ETH", "SOL", "DOGE", "ARB", "OP", "AVAX", "LINK", "MATIC", "APT"]
        rates = {}
        for asset in assets:
            base_rate = btc_ret * 5 + random.gauss(0, 0.0005)
            rates[asset] = round(base_rate, 6)
        funding_snapshots.append(rates)

    return btc_closes, btc_highs, btc_lows, eth_closes, timestamps, funding_snapshots


def main():
    print("=== Analog Memory Trading — Pipeline Demo ===\n")

    # --- Generate data ---
    print("1. Generating synthetic market data (2000 bars × 4h = ~333 days)...")
    btc_closes, btc_highs, btc_lows, eth_closes, timestamps, funding_snaps = _generate_synthetic_data()
    print(f"   BTC range: ${min(btc_closes):,.0f} – ${max(btc_closes):,.0f}")
    print(f"   {len(timestamps)} bars from {len(funding_snaps)} funding snapshots\n")

    # --- Build fingerprints ---
    print("2. Computing fingerprints...")
    fp_engine = FingerprintEngine(primary_asset="BTC", secondary_asset="ETH")
    surface_engine = FundingSurfaceEngine(top_n=10)
    store = FingerprintStore("data/demo_fingerprints")

    # Feed historical data incrementally (simulating live updates)
    fingerprints = []
    for i in range(len(timestamps)):
        # Feed candles up to this point
        fp_engine.update_candles(
            "BTC",
            closes=[btc_closes[i]],
            highs=[btc_highs[i]],
            lows=[btc_lows[i]],
            timestamps=[timestamps[i]],
        )
        fp_engine.update_candles("ETH", closes=[eth_closes[i]], timestamps=[timestamps[i]])

        # Feed funding
        surface = surface_engine.record(funding_snaps[i], timestamp=timestamps[i])
        fp_engine.update_funding(surface_engine.features())

        # Compute fingerprint every bar (in production: every 4h)
        if i >= 180:  # need enough history for 30d features
            fp = fp_engine.compute(timestamp=timestamps[i])
            fingerprints.append(fp)
            store.append(fp)

    store.flush()
    print(f"   {len(fingerprints)} fingerprints computed and stored")
    print(f"   Features per fingerprint: {len(fingerprints[0].vector)}")
    print(f"   Feature names: {sorted(fingerprints[0].vector.keys())[:8]}...\n")

    # --- Find analogs ---
    print("3. Finding analogs for the latest market state...")
    finder = AnalogFinder(k=20, recency_halflife_days=180)
    finder.fit(fingerprints)

    query = fingerprints[-1]
    matches = finder.query(query)
    quality = finder.analog_quality(matches)

    print(f"   Query timestamp: {query.timestamp:.0f}")
    print(f"   Found {len(matches)} analogs")
    print(f"   Mean similarity: {quality['mean_similarity']:.4f}")
    print(f"   Analog confidence: {quality['confidence']:.4f}")
    print("   Top 5 analogs:")
    for m in matches[:5]:
        age_days = (query.timestamp - m.fingerprint.timestamp) / 86400
        print(f"     rank={m.rank} sim={m.similarity:.3f} weight={m.weight:.3f} age={age_days:.0f}d")

    # --- Score strategies ---
    print("\n4. Scoring strategies across analog set...")
    scorer = AnalogScorer(forward_hours=4.0)

    # Create simple evaluators using forward returns from our synthetic data
    ts_to_idx = {ts: i for i, ts in enumerate(timestamps)}

    def _forward_return(analog_ts: float, forward_hours: float) -> float | None:
        """Get forward return from synthetic data."""
        idx = ts_to_idx.get(analog_ts)
        if idx is None:
            # Find closest
            diffs = [(abs(ts - analog_ts), i) for i, ts in enumerate(timestamps)]
            _, idx = min(diffs)
        fwd_bars = max(1, int(forward_hours / 4))
        if idx + fwd_bars >= len(btc_closes):
            return None
        return (btc_closes[idx + fwd_bars] - btc_closes[idx]) / btc_closes[idx]

    def funding_arb_eval(analog_ts: float, forward_hours: float) -> float | None:
        """Simulate funding arb: profit when funding is extreme and mean-reverts."""
        idx = ts_to_idx.get(analog_ts)
        if idx is None:
            diffs = [(abs(ts - analog_ts), i) for i, ts in enumerate(timestamps)]
            _, idx = min(diffs)
        if idx >= len(funding_snaps):
            return None
        btc_funding = funding_snaps[idx].get("BTC", 0)
        if abs(btc_funding) < 0.0005:  # threshold
            return None  # wouldn't trade
        # Funding arb profits when funding normalizes
        fwd_ret = _forward_return(analog_ts, forward_hours)
        if fwd_ret is None:
            return None
        # Short when funding positive, long when negative
        direction = -1 if btc_funding > 0 else 1
        return direction * fwd_ret + abs(btc_funding)  # collect funding + directional

    def trend_follow_eval(analog_ts: float, forward_hours: float) -> float | None:
        """Simulate trend following: profit when momentum continues."""
        idx = ts_to_idx.get(analog_ts)
        if idx is None:
            diffs = [(abs(ts - analog_ts), i) for i, ts in enumerate(timestamps)]
            _, idx = min(diffs)
        if idx < 6:
            return None
        # 1d momentum
        mom = (btc_closes[idx] - btc_closes[idx - 6]) / btc_closes[idx - 6]
        if abs(mom) < 0.005:  # no clear trend
            return None
        fwd_ret = _forward_return(analog_ts, forward_hours)
        if fwd_ret is None:
            return None
        direction = 1 if mom > 0 else -1
        return direction * fwd_ret

    def mean_reversion_eval(analog_ts: float, forward_hours: float) -> float | None:
        """Simulate mean reversion: profit when extended moves snap back."""
        idx = ts_to_idx.get(analog_ts)
        if idx is None:
            diffs = [(abs(ts - analog_ts), i) for i, ts in enumerate(timestamps)]
            _, idx = min(diffs)
        if idx < 6:
            return None
        mom = (btc_closes[idx] - btc_closes[idx - 6]) / btc_closes[idx - 6]
        if abs(mom) < 0.02:  # need an extended move to fade
            return None
        fwd_ret = _forward_return(analog_ts, forward_hours)
        if fwd_ret is None:
            return None
        direction = -1 if mom > 0 else 1  # fade the move
        return direction * fwd_ret

    scorer.register_strategy("funding_arb", funding_arb_eval)
    scorer.register_strategy("trend_follow", trend_follow_eval)
    scorer.register_strategy("mean_reversion", mean_reversion_eval)

    scores = scorer.score(matches)
    print("\n   Strategy rankings:")
    for s in scores:
        print(f"     {s.strategy_name:20s} WR={s.win_rate:.1%}  mean={s.mean_return:+.4f}  "
              f"risk={s.risk_bucket:5s}  confidence={s.confidence:.2f}  (n={s.n_analogs})")

    best = scorer.recommend(matches)
    if best:
        print(f"\n   >>> RECOMMENDATION: Run '{best.strategy_name}' at {best.risk_bucket} risk")
        print(f"       Win rate across analogs: {best.win_rate:.1%}")
        print(f"       Confidence: {best.confidence:.2f}")
    else:
        print("\n   >>> RECOMMENDATION: Sit out (0x) — no strategy scores above threshold")

    # --- Surface snapshot ---
    print("\n5. Current funding surface:")
    surface = surface_engine.current()
    if surface:
        print(f"   Mean funding:  {surface.mean:+.6f}")
        print(f"   Dispersion:    {surface.dispersion:.6f}")
        print(f"   Skew:          {surface.skew:+.4f}")
        print(f"   Extremes:      {surface.extreme_count} assets")
        mom = surface_engine.momentum(lookback_hours=8.0)
        print(f"   8h momentum:   {mom:+.8f}" if mom else "   8h momentum:   n/a")

    print("\n=== Pipeline complete ===")


if __name__ == "__main__":
    main()
