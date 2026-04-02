"""Run walk-forward evaluation and champion/challenger comparison.

Usage::

    # Walk-forward evaluation with default settings
    python3 -m analog.run_walkforward

    # Walk-forward with custom parameters
    python3 -m analog.run_walkforward --days 365 --step 6 --forward 4

    # Champion/challenger arena — compare multiple configurations
    python3 -m analog.run_walkforward --arena
"""

from __future__ import annotations

import argparse
import asyncio

import structlog

from analog.backfill import run_backfill, print_summary
from analog.champion import Arena, ChallengerConfig
from analog.evaluators import build_evaluators
from analog.walkforward import WalkForward, print_walkforward_report

logger = structlog.get_logger()


async def main(
    lookback_days: int = 730,
    step_bars: int = 6,
    forward_hours: float = 4.0,
    k: int = 20,
    arena_mode: bool = False,
):
    print("=" * 60)
    print("  ANALOG MEMORY — Walk-Forward Evaluation")
    print("=" * 60)

    # --- Backfill ---
    print("\n[1/3] Backfilling historical data...")
    result = await run_backfill(lookback_days=lookback_days)
    print_summary(result)

    if not result.fingerprints:
        print("\nERROR: No fingerprints. Check data fetch errors.")
        return

    evaluators = build_evaluators(result.candles, result.funding)
    fingerprints = result.fingerprints

    if arena_mode:
        # --- Champion/Challenger Arena ---
        print("\n[2/3] Running champion/challenger arena...")

        arena = Arena(verbose=True)

        # Current baseline
        arena.add_challenger(ChallengerConfig(
            name="baseline",
            k=20,
            recency_halflife_days=180,
            forward_hours=forward_hours,
            step_bars=step_bars,
        ))

        # More analogs, longer memory
        arena.add_challenger(ChallengerConfig(
            name="wide_k30_long",
            k=30,
            recency_halflife_days=365,
            forward_hours=forward_hours,
            step_bars=step_bars,
        ))

        # Fewer analogs, shorter memory (more reactive)
        arena.add_challenger(ChallengerConfig(
            name="tight_k10_short",
            k=10,
            recency_halflife_days=90,
            forward_hours=forward_hours,
            step_bars=step_bars,
        ))

        # Longer forward horizon
        arena.add_challenger(ChallengerConfig(
            name="fwd_8h",
            k=20,
            recency_halflife_days=180,
            forward_hours=8.0,
            step_bars=step_bars,
        ))

        # Shorter forward horizon
        arena.add_challenger(ChallengerConfig(
            name="fwd_2h",
            k=20,
            recency_halflife_days=180,
            forward_hours=2.0,
            step_bars=step_bars,
        ))

        results = arena.evaluate(fingerprints, evaluators)

        print("\n[3/3] Arena complete.")
        print(f"\n  Champion: {results.champion.name if results.champion else 'none'}")
        print(f"  {results.recommendation}")

    else:
        # --- Single Walk-Forward ---
        print(f"\n[2/3] Running walk-forward (k={k}, step={step_bars}, fwd={forward_hours}h)...")

        wf = WalkForward(
            fingerprints=fingerprints,
            evaluators=evaluators,
            step_bars=step_bars,
            k=k,
            forward_hours=forward_hours,
        )
        results = wf.run(verbose=True)

        print("\n[3/3] Results:")
        print_walkforward_report(results)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Analog Memory — Walk-Forward Evaluation")
    parser.add_argument("--days", type=int, default=730, help="Lookback days (default: 730)")
    parser.add_argument("--step", type=int, default=6, help="Eval every N bars (default: 6 = daily)")
    parser.add_argument("--forward", type=float, default=4.0, help="Forward hours (default: 4.0)")
    parser.add_argument("--k", type=int, default=20, help="Number of analogs (default: 20)")
    parser.add_argument("--arena", action="store_true", help="Run champion/challenger arena")
    args = parser.parse_args()

    asyncio.run(main(
        lookback_days=args.days,
        step_bars=args.step,
        forward_hours=args.forward,
        k=args.k,
        arena_mode=args.arena,
    ))
