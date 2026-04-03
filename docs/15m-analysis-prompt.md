# Prompt: 15-Minute Candle Analysis — All Strategies × All Assets

Copy everything below the line into a new Claude session.

---

## Task

I need you to run a comprehensive backtesting analysis of my trading strategy library against 2 years of 15-minute candle data across 13 crypto perpetual futures assets on Hyperliquid. This is a long-running task — the backfill alone will take 30-60 minutes. That's fine, let it run.

## Context

The repo is at `/home/odinsuncle/crack-the-nut/`. It's a Python trading strategy toolkit. The `analog/` module contains:

- **55 trading strategies** across 5 families (established, contrarian, beta-adjusted, lead-lag, original)
- A **backfill system** that pulls OHLCV candles and funding rates from Hyperliquid's API
- A **multi-asset evaluation runner** (`analog/run_multi_asset.py`) that tests all strategies across all assets
- A **lead-lag analyzer** (`analog/lead_lag.py`) that measures BTC/ETH → alt correlation at various time offsets

Currently the system uses **4-hour candles**. We've found strong signals (55+ strategies, 105 profitable combos across 9 assets) but the 4h resolution is too coarse to capture lead-lag effects between BTC and alts, which likely happen on a 15m-1h timescale.

## What to do

### Step 1: Add new assets and switch to 15m bars

Edit `/home/odinsuncle/crack-the-nut/analog/backfill.py`:

1. Change `CANDLE_ASSETS` to include 4 new assets:
```python
CANDLE_ASSETS = ["BTC", "ETH", "SOL", "DOGE", "ARB", "OP", "AVAX", "LINK", "WIF", "TAO", "HYPE", "SPX", "FARTCOIN"]
```

2. Also add them to `FUNDING_ASSETS`:
```python
FUNDING_ASSETS = ["BTC", "ETH", "SOL", "DOGE", "ARB", "OP", "AVAX", "LINK", "WIF", "PEPE", "TAO", "HYPE", "SPX", "FARTCOIN"]
```

3. Change `INTERVAL` from `"4h"` to `"15m"` — but **DO NOT change it permanently**. Instead, make the interval configurable:
   - Add an `interval` parameter to `run_backfill()` (default "4h")
   - Pass it through to `fetch_hl_candles()`
   - The `compute_fingerprints()` function uses hardcoded 4h bar alignment — this needs to become interval-aware

4. The `FingerprintEngine` in `fingerprint.py` has hardcoded constants for 4h bars (e.g., `RETURN_HORIZONS = {"4h": 1, "1d": 6, "7d": 42, "30d": 180}`). These need to scale with the interval. For 15m bars:
   - 1h = 4 bars, 4h = 16 bars, 1d = 96 bars, 7d = 672 bars, 30d = 2880 bars

5. The strategy evaluators in `evaluators.py`, `strategies.py`, `contrarian.py`, `beta_strategies.py`, and `lead_lag.py` all have bar counts calibrated for 4h bars (e.g., "42 bars = 7 days"). These need to scale proportionally for 15m bars, OR you can keep the strategies operating at the same time horizons by multiplying all bar counts by 16 (since 4h/15m = 16).

**IMPORTANT**: The simplest correct approach is to add a `bars_per_hour` parameter to `HistoricalData` and have the helper methods (backward_return, forward_return, etc.) accept hours and convert to bars internally. Many already do this — `backward_return(asset, ts, lookback_hours)` works in hours. But `get_recent_candles(asset, ts, n_bars)` and `realized_vol(asset, ts, n_bars)` work in bars. The strategies that call these directly need their bar counts scaled.

### Step 2: Backfill 2 years of 15m data

Run the backfill for all 13 assets. This will be a LOT of data:
- 13 assets × 2 years × 96 bars/day × 730 days = ~9.1 million candles
- Plus funding rates for 14 assets

The Hyperliquid API returns up to 5000 candles per request, so this will require ~1,820 API calls just for candles. At 1 second between requests with rate limiting, that's ~30-40 minutes minimum.

Let it run. Save the data to `data/fingerprints_15m/` (separate from the 4h data).

### Step 3: Run multi-asset analysis

After backfill completes, run the equivalent of `python3 -m analog.run_multi_asset` but with 15m data. The key output we need:

1. **Per-strategy, per-asset results table**: strategy name, asset, N trades, win rate, mean return, total PnL, worst trade
2. **Top 50 combinations** ranked by mean return (N >= 10 trades)
3. **Strategy robustness**: how many assets is each strategy profitable on
4. **Asset summary**: which assets have the most winning strategies

### Step 4: Run lead-lag analysis at 15m resolution

This is the most important new analysis. Run `measure_lead_lag()` from `analog/lead_lag.py` with 15m candles. At this resolution we should be able to see the actual time lag between BTC/ETH moves and alt responses.

The analysis should show:
- Cross-correlation at lags from 0 to 48 bars (0 to 12 hours at 15m)
- Which alts have the strongest lagged response to BTC
- Which alts have the strongest lagged response to ETH
- Optimal lag per asset pair

Then test the lead-lag strategies at various lag offsets (15m, 30m, 1h, 2h, 4h) to find the optimal trading horizon.

### Step 5: Report

After all analysis is complete, print a comprehensive report with:

1. **Lead-lag correlation table** — all leader/follower pairs with optimal lag and correlation improvement
2. **Top 50 strategy-asset combinations** at 15m resolution
3. **Comparison with 4h results** — which strategies got better or worse at higher resolution
4. **Strategy robustness ranking** — strategies positive on the most assets
5. **New asset results** — how TAO, HYPE, SPX, and FARTCOIN compare to the existing 9

Commit all changes and push to origin.

## Technical notes

- The repo uses Python 3.12 with a venv at `.venv/`. Activate it: `source .venv/bin/activate`
- Install: `pip install -e ".[dev]"`
- Lint with: `python3 -m ruff check analog/`
- The Hyperliquid API is public, no API key needed
- Rate limit: 1200 weight/min. The backfill already has 1-second delays between requests.
- PEPE funding sometimes returns a 500 error — that's fine, skip it
- Some of the new assets (TAO, HYPE, SPX, FARTCOIN) may not have the full 2 years of history. That's fine — use whatever is available.
- The `run_multi_asset.py` evaluates strategies by stepping through every 6th candle at 4h resolution. At 15m, stepping every 6th bar would be every 1.5 hours — that's a lot of evaluations. Consider stepping every 96th bar (once per day) to keep runtime manageable, or every 24th bar (every 6 hours) for more granularity.
- The forward_hours parameter in strategy evaluators determines how far forward to measure P&L. At 4h resolution we used forward_hours=4. At 15m resolution, test with forward_hours=1, 2, and 4 to see which works best.

## Files you'll need to modify

1. `analog/backfill.py` — interval parameter, new assets
2. `analog/fingerprint.py` — interval-aware bar counts
3. `analog/evaluators.py` — bars_per_hour scaling or interval parameter in HistoricalData
4. `analog/strategies.py` — bar count scaling (all 25 strategies)
5. `analog/contrarian.py` — bar count scaling (9 strategies)
6. `analog/beta_strategies.py` — bar count scaling (8 strategies)
7. `analog/lead_lag.py` — bar count scaling, finer lag measurement
8. `analog/run_multi_asset.py` — step interval adjustment, output formatting
9. `analog/surface.py` — funding bar alignment

The highest-risk changes are in the bar count scaling. Every strategy has numbers like `42` (7 days at 4h = 42 bars) that need to become `672` (7 days at 15m). The safest approach: add an `interval_bars_per_day` constant to HistoricalData (96 for 15m, 6 for 4h) and compute bar counts as `days * interval_bars_per_day`.

## Expected results

At 4h resolution we found:
- 105 of 299 viable combinations profitable
- Best strategies: rsi_regime_reversion (8/8 alts), residual_breakout (7/8), mean_reversion (7/9)
- Best combos: ARB:rsi_regime_reversion (81.2% WR), WIF:residual_breakout (+74.5% total)
- Lead-lag: zero measurable lag at 4h — all correlations peak at lag=0

At 15m we expect:
- The lead-lag to become visible (probably 15m-1h lag for most alts)
- More trades per strategy (16x more evaluation points)
- Some strategies to improve (timing-sensitive ones) and some to degrade (noise at higher frequency)
- The new assets (especially FARTCOIN, WIF) to show strong lead-lag effects due to higher retail participation
