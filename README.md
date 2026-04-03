# crack-the-nut

A strategy research toolkit for crypto perpetual futures on Hyperliquid. 63 backtestable strategies, walk-forward validation, multi-resolution analysis (15m to 4h), and bar-by-bar exit logic simulation.

**Goal:** Systematically identify profitable trading strategies across crypto assets, validate them out-of-sample, and optimize exit logic -- all without lookahead bias.

## What Is This?

This repo has two layers:

1. **A shared trading toolkit** (`config/`, `data/`, `strategies/`, `exchanges/`, `execution/`, etc.) -- composable modules for building trading bots. Config, database, scheduling, notifications, risk management.

2. **A strategy research engine** (`analog/`) -- the main focus of recent work. 63 strategies evaluated across 13 assets with walk-forward backtesting, multi-resolution analysis, and exit optimization. This is where the research happens.

## Quick Start

```bash
git clone https://github.com/CShear/crack-the-nut.git
cd crack-the-nut
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

## Architecture: The `analog/` Module

The research engine lives in `analog/`. Here is what each file does:

| File | Purpose |
|------|---------|
| `backfill.py` | Pulls OHLCV candles + funding rates from Hyperliquid API. Supports configurable intervals (15m, 1h, 4h, etc). 13 assets: BTC, ETH, SOL, DOGE, ARB, OP, AVAX, LINK, WIF, TAO, HYPE, SPX, FARTCOIN. Caches to pickle for fast re-runs. |
| `fingerprint.py` | Market state fingerprinting engine. Dense feature vectors (returns, vol, cross-asset, funding surface) for similarity search. Interval-aware. |
| `evaluators.py` | `HistoricalData` class with O(log n) lookups, 20+ technical indicators (RSI, Bollinger, ATR, Donchian, Keltner, EMA, SMA), and the `simulate_exit()` bar-by-bar exit simulator with SL/TP/trailing stops. Contains the `b()` method for interval-agnostic bar scaling. |
| `strategies.py` | 25 established strategies across 8 families: TSMOM, cross-sectional momentum, carry/funding, mean reversion, volatility, trend following, pairs/RV, crypto-native. |
| `contrarian.py` | 9 contrarian strategies: flipped signals (strategies that predict backwards), consensus fade, disagreement breakout, funding-filtered trend. |
| `beta_strategies.py` | 8 beta-adjusted strategies: residual mean reversion, residual breakout, BTC-led alt trading, beta compression, high-beta funding carry. |
| `lead_lag.py` | 16 lead-lag strategies at multiple lag offsets (15m to 12h for BTC and ETH as leaders), plus `measure_lead_lag()` cross-correlation analysis. |
| `surface.py` | Funding rate surface engine -- treats cross-asset funding rates as a yield curve with mean, dispersion, skew, momentum, and extreme count features. |
| `store.py` | Parquet-backed fingerprint storage. |
| `run_multi_asset.py` | Evaluate all strategies across all assets at 4h resolution. |
| `run_15m_analysis.py` | Dual-resolution analysis: 1h strategies + 15m lead-lag measurement. |
| `simulate_top_n.py` | Walk-forward portfolio simulation with concentrated strategy selection (rank by Sharpe on training period, trade top N on out-of-sample). |
| `simulate_portfolio.py` | Full portfolio simulation with position sizing, compounding, walk-forward validation, monthly/strategy/asset breakdowns. |
| `exit_analysis.py` | Exhaustive exit strategy comparison: 20 exit variants tested on top 20 strategy-asset combos. SL, TP, trailing stops, combinations. |

### Supporting Modules (Trading Toolkit)

| Module | What it does |
|--------|-------------|
| `config/` | Pydantic-settings base class. Loads from `.env`. |
| `data/` | Async SQLite wrapper with upsert, batch operations, trade/signal schemas. |
| `strategies/` | Abstract `Strategy` base class -- implement `on_data`, `should_enter`, `should_exit`. Same interface for backtesting and live. |
| `exchanges/` | Exchange adapters: Hyperliquid (perps), Polymarket (prediction markets), DEX/Web3. |
| `execution/` | Risk gates, half-Kelly sizing, correlation tracking, gas guards. |
| `scoring/` | Composite scorer: register weighted sub-scores, get 0-100 signal. |
| `backtest/` | Feed candles to a Strategy, get back win rate, P&L, Sharpe, drawdown, profit factor. |
| `agents/` | Ensemble LLM predictions (3 temps, take median). |
| `scheduler/` | APScheduler wrapper with graceful shutdown. |
| `notify/` | Telegram alerts with rate limiting. |

## How to Use -- Step by Step

### 1. Backfill Data

```bash
# Default 4h candles, 730 days (Hyperliquid has 2+ years at 4h)
python3 -m analog.backfill

# 1h resolution (~7 months available)
python3 -m analog.backfill --interval 1h

# Data is cached to data/backfill_{interval}.pkl for fast re-runs
```

### 2. Run Multi-Asset Analysis

```bash
# 4h bars, 730 days, 4h forward evaluation window
python3 -m analog.run_multi_asset --days 730 --forward 4.0

# Show top 40 combos
python3 -m analog.run_multi_asset --days 730 --top 40
```

### 3. Run High-Resolution Analysis

```bash
# 1h strategy eval + 15m lead-lag, dual resolution
python3 -m analog.run_15m_analysis

# Reuse cached data (skip backfill)
python3 -m analog.run_15m_analysis --skip-backfill
```

### 4. Find Top Strategies (Walk-Forward)

```bash
# Rank by Sharpe on training half, simulate top N on out-of-sample half
python3 -m analog.simulate_top_n
```

### 5. Test Exit Logic

```bash
# 20 exit variants across top 20 strategy-asset combos
python3 -m analog.exit_analysis
```

### 6. Simulate Full Portfolio

```bash
# Walk-forward portfolio with position sizing
python3 -m analog.simulate_portfolio --capital 1000 --interval 4h

# Adjust position sizing and max concurrent positions
python3 -m analog.simulate_portfolio --capital 5000 --pos-pct 0.20 --max-pos 5
```

### 7. Lead-Lag Analysis

```bash
# Measure BTC/ETH -> alt cross-correlations at various lags
python3 -m analog.lead_lag --days 730
```

## Using with Claude

This repo is designed to be explored and extended with Claude Code. Useful prompts:

- "Add a new strategy based on [paper/idea] to strategies.py"
- "Run the portfolio simulation with $5000 starting capital and 20% position sizing"
- "What would happen if we only traded mean reversion strategies?"
- "Backfill 1h data and compare strategy performance at 1h vs 4h"
- "Analyze which strategies work best on high-volatility altcoins like WIF and FARTCOIN"
- "Build a regime detector that switches between trend and mean-reversion strategies"

## Strategy Library (63 strategies, 5 families)

| Family | File | Count | Description |
|--------|------|:-----:|-------------|
| **Original** | `evaluators.py` | 5 | Funding arb, multi-asset funding, trend follow, mean reversion, breakout |
| **Established** | `strategies.py` | 25 | Academic/practitioner: TSMOM, cross-sectional momentum, funding carry, mean reversion, volatility, trend following, pairs, crypto-native |
| **Contrarian** | `contrarian.py` | 9 | Flipped signals, consensus fade, novel-state trend, disagreement breakout, funding-filtered |
| **Beta-adjusted** | `beta_strategies.py` | 8 | Residual strategies (strip out BTC beta), BTC-led alt trading, beta-relative |
| **Lead-lag** | `lead_lag.py` | 16 | BTC/ETH as leading indicators at 7 lag offsets (15m to 12h), multi-lag ensemble, impulse catch-up, reversal fade |

## Key Findings

Walk-forward backtesting across 13 assets, Apr 2024 -- Apr 2026:

- **138 profitable strategy-asset combos** identified via walk-forward (Sharpe-ranked on training data, validated out-of-sample)
- **Top 10 portfolio**: +26.4% annual return, 4.9% max drawdown, 1.55 profit factor
- **Optimal exits** (2% SL + 5% TP) improve returns from +5.9% to +13.1% on the same trades
- **Lead-lag between BTC and alts**: zero measurable lag even at 15-minute resolution
- **Trailing stops underperform** on crypto due to high intra-bar volatility
- **Best strategies**: mean_reversion, residual_breakout, btc_eth_ratio_rv, flip_beta_rotation, rsi_regime_reversion
- **Alts are where the edge is**: strategies that break even on BTC are profitable on less efficient altcoin markets
- **Beta-adjusted strategies are more robust** than raw price strategies -- trading the residual (after removing BTC influence) filters false signals

## Data Availability (Hyperliquid)

| Interval | History Available | Bars per Day |
|----------|-------------------|:------------:|
| 4h | 2+ years | 6 |
| 1h | ~7 months | 24 |
| 30m | ~3.5 months | 48 |
| 15m | ~53 days | 96 |

## Stack

- Python 3.12+, async throughout
- `pydantic-settings` -- config from `.env`
- `httpx` -- HTTP client for Hyperliquid API
- `structlog` -- structured logging
- `pandas` -- data analysis
- Exchange SDKs: `hyperliquid-python-sdk`, `py-clob-client`, `web3` (all optional)

## Ground Rules

1. **No secrets in the repo.** API keys, private keys go in `.env` (gitignored).
2. **Backtest before you deploy.** Walk-forward validation is non-negotiable.
3. **No lookahead bias.** Training/test split. Deterministic shuffling. No sorting by future P&L.
4. **Document your strategies.** Every strategy has a docstring citing its source.
5. **Risk management is not optional.** Position limits, drawdown circuit breakers, kill switches.

## License

MIT -- use it, fork it, profit from it.
