"""Run the full analog memory analysis on real market data.

Backfills data → computes fingerprints → finds analogs → scores strategies.

Usage::

    python3 -m analog.run_analysis
    python3 -m analog.run_analysis --days 365 --k 30
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone

import structlog

from analog.backfill import run_backfill, print_summary
from analog.finder import AnalogFinder
from analog.scorer import AnalogScorer
from analog.evaluators import build_evaluators

logger = structlog.get_logger()


async def main(lookback_days: int = 730, k: int = 20, forward_hours: float = 4.0):
    print("=" * 60)
    print("  ANALOG MEMORY TRADING — Real Data Analysis")
    print("=" * 60)

    # --- Step 1: Backfill ---
    print("\n[1/5] Backfilling historical data...")
    result = await run_backfill(lookback_days=lookback_days)
    print_summary(result)

    if not result.fingerprints:
        print("\nERROR: No fingerprints computed. Check data fetch errors above.")
        return

    # --- Step 2: Load fingerprints ---
    print(f"\n[2/5] Loading {len(result.fingerprints)} fingerprints...")
    fingerprints = result.fingerprints

    # --- Step 3: Find analogs for the latest state ---
    print(f"\n[3/5] Finding {k} analogs for current market state...")
    finder = AnalogFinder(k=k, recency_halflife_days=180, min_gap_hours=8.0)
    finder.fit(fingerprints)

    # Query = most recent fingerprint
    query = fingerprints[-1]
    query_dt = datetime.fromtimestamp(query.timestamp, tz=timezone.utc)
    matches = finder.query(query)
    quality = finder.analog_quality(matches)

    print(f"\n  Query time: {query_dt.strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"  Analogs found: {len(matches)}")
    print(f"  Mean similarity: {quality['mean_similarity']:.4f}")
    print(f"  Similarity spread: {quality['similarity_spread']:.4f}")
    print(f"  Time span: {quality['time_span_days']:.0f} days")
    print(f"  Analog confidence: {quality['confidence']:.4f}")

    print("\n  Top 10 analogs:")
    print(f"  {'Rank':>4}  {'Date':>12}  {'Sim':>6}  {'Weight':>6}  {'Age':>6}")
    print(f"  {'─' * 4}  {'─' * 12}  {'─' * 6}  {'─' * 6}  {'─' * 6}")
    for m in matches[:10]:
        dt = datetime.fromtimestamp(m.fingerprint.timestamp, tz=timezone.utc)
        age_days = (query.timestamp - m.fingerprint.timestamp) / 86400
        print(f"  {m.rank:>4}  {dt.strftime('%Y-%m-%d'):>12}  {m.similarity:>6.3f}  {m.weight:>6.3f}  {age_days:>5.0f}d")

    # --- Step 4: Score strategies ---
    print(f"\n[4/5] Scoring strategies across analog set (forward={forward_hours}h)...")
    scorer = AnalogScorer(forward_hours=forward_hours)
    evaluators = build_evaluators(result.candles, result.funding)

    for name, evaluator in evaluators.items():
        scorer.register_strategy(name, evaluator)

    scores = scorer.score(matches, forward_hours=forward_hours)

    print("\n  Strategy Rankings:")
    print(f"  {'Strategy':>22}  {'WR':>6}  {'Mean':>8}  {'Worst':>8}  {'Consist':>8}  {'Risk':>5}  {'Conf':>5}  {'N':>3}")
    print(f"  {'─' * 22}  {'─' * 6}  {'─' * 8}  {'─' * 8}  {'─' * 8}  {'─' * 5}  {'─' * 5}  {'─' * 3}")
    for s in scores:
        print(
            f"  {s.strategy_name:>22}  "
            f"{s.win_rate:>5.1%}  "
            f"{s.mean_return:>+7.4f}  "
            f"{s.worst_return:>+7.4f}  "
            f"{s.consistency:>7.2f}  "
            f"{s.risk_bucket:>5}  "
            f"{s.confidence:>5.2f}  "
            f"{s.n_analogs:>3}"
        )

    # --- Step 5: Recommendation ---
    best = scorer.recommend(matches, forward_hours=forward_hours)
    print("\n[5/5] Recommendation:")
    if best:
        print(f"\n  >>> Run '{best.strategy_name}' at {best.risk_bucket} risk")
        print(f"      Win rate: {best.win_rate:.1%} across {best.n_analogs} analogs")
        print(f"      Mean return: {best.mean_return:+.4f}")
        print(f"      Consistency: {best.consistency:.2f}")
        print(f"      Confidence: {best.confidence:.2f}")
    else:
        print("\n  >>> SIT OUT (0x) — no strategy above threshold")
        print(f"      Analog confidence: {quality['confidence']:.4f}")
        if quality['mean_similarity'] < 0.3:
            print("      Reason: Low analog similarity — market state is relatively novel")

    # --- Bonus: Feature importance ---
    print("\n--- Current Market Fingerprint ---")
    for k_name in sorted(query.vector.keys()):
        print(f"  {k_name:>30}: {query.vector[k_name]:>12.6f}")

    # --- Walk-forward preview ---
    print("\n--- Walk-Forward Spot Check (last 10 bars) ---")
    print("  Checking what the system would have recommended at each of the last 10 bars...\n")
    print(f"  {'Date':>12}  {'Best Strategy':>22}  {'Risk':>5}  {'WR':>6}  {'Conf':>5}  {'Actual 4h':>10}")
    print(f"  {'─' * 12}  {'─' * 22}  {'─' * 5}  {'─' * 6}  {'─' * 5}  {'─' * 10}")

    for fp in fingerprints[-11:-1]:
        fp_dt = datetime.fromtimestamp(fp.timestamp, tz=timezone.utc)
        m = finder.query(fp, k=k)
        rec = scorer.recommend(m, forward_hours=forward_hours)

        # Get actual forward return
        from analog.evaluators import HistoricalData
        hist = HistoricalData(result.candles, result.funding)
        actual_ret = hist.forward_return("BTC", fp.timestamp, forward_hours)
        actual_str = f"{actual_ret:+.4f}" if actual_ret is not None else "n/a"

        if rec:
            print(
                f"  {fp_dt.strftime('%Y-%m-%d'):>12}  "
                f"{rec.strategy_name:>22}  "
                f"{rec.risk_bucket:>5}  "
                f"{rec.win_rate:>5.1%}  "
                f"{rec.confidence:>5.2f}  "
                f"{actual_str:>10}"
            )
        else:
            print(f"  {fp_dt.strftime('%Y-%m-%d'):>12}  {'(sit out)':>22}  {'0x':>5}  {'—':>6}  {'—':>5}  {actual_str:>10}")

    print(f"\n{'=' * 60}")
    print(f"  Analysis complete. {len(result.fingerprints)} fingerprints across "
          f"{len(result.candles)} assets.")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Analog Memory Trading — Real Data Analysis")
    parser.add_argument("--days", type=int, default=730, help="Lookback days (default: 730)")
    parser.add_argument("--k", type=int, default=20, help="Number of analogs (default: 20)")
    parser.add_argument("--forward", type=float, default=4.0, help="Forward hours (default: 4.0)")
    args = parser.parse_args()

    asyncio.run(main(lookback_days=args.days, k=args.k, forward_hours=args.forward))
