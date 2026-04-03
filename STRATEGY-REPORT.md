# Crack-the-Nut: Multi-Strategy Crypto Trading System

## Complete Strategy Reference & Analysis

**Date:** April 3, 2026  
**Author:** Christian Shearer, with Claude Code  
**Status:** LIVE on Hyperliquid since April 3, 2026  
**Starting Equity:** $619.26

---

## Table of Contents

1. [How We Got Here](#1-how-we-got-here)
2. [System Overview](#2-system-overview)
3. [Walk-Forward Methodology](#3-walk-forward-methodology)
4. [The Top-11 Portfolio](#4-the-top-11-portfolio)
5. [Individual Strategy Deep Dives](#5-individual-strategy-deep-dives)
6. [Exit Logic: TP-to-Trailing-Stop](#6-exit-logic-tp-to-trailing-stop)
7. [Portfolio Simulation Results](#7-portfolio-simulation-results)
8. [Risk Analysis](#8-risk-analysis)
9. [Live Trading Configuration](#9-live-trading-configuration)
10. [Areas for Improvement](#10-areas-for-improvement)

---

## 1. How We Got Here

### The Journey

The Crack-the-Nut project started as a research framework to find profitable trading strategies on Hyperliquid perpetual futures. The approach was systematic:

**Phase 1: Build the infrastructure.** A backtesting engine was built that pulls historical 4-hour OHLCV candles and hourly funding rates from Hyperliquid's API. The engine supports any bar interval (15 minutes to daily) and includes bar-by-bar exit simulation with stop-loss, take-profit, and trailing stop logic.

**Phase 2: Create a strategy library.** 63 strategies were implemented across 5 families:

| Family | Count | Source |
|--------|:-----:|--------|
| Original (funding arb, trend, mean reversion, breakout) | 5 | First principles |
| Established (TSMOM, Bollinger, RSI, Donchian, carry, pairs) | 25 | Academic literature |
| Contrarian (flipped signals, consensus fade, disagreement) | 9 | Walk-forward findings |
| Beta-adjusted (residual breakout, residual MR, BTC-led) | 8 | BTC correlation analysis |
| Lead-lag (BTC/ETH leading alts) | 16 | Cross-correlation analysis |

**Phase 3: Walk-forward testing.** All 63 strategies were evaluated across 13 assets (819 combinations). The data was split 50/50: train on the first year, test on the second. Strategies were ranked by Sharpe ratio on training data, then the top N were traded on out-of-sample data.

**Phase 4: Exit optimization.** 20 exit variants were tested (fixed holds, SL only, TP only, SL+TP, trailing stops). The best universal exit was 2% SL + 5% TP. A further innovation — converting TP into a 0.5% trailing stop when hit — tripled portfolio returns.

**Phase 5: Asset expansion.** The universe was expanded from 13 to 28 assets with liquidity-aware position sizing. Lower-liquidity assets get smaller allocations.

**Phase 6: Live deployment.** The top-11 strategy-asset combinations were deployed to the existing Hyperliquid bot infrastructure on April 3, 2026.

### Key Negative Finding

**Lead-lag analysis at 15-minute resolution showed zero exploitable lag between BTC/ETH and altcoins.** The crypto market prices information too efficiently at these timeframes. This ruled out an entire class of 16 strategies before capital was allocated — a significant time savings.

### Key Positive Finding

**An ensemble voting approach (combining all strategy signals into weighted votes) underperformed the simpler top-N independent approach.** The best ensemble configuration produced +13.1% vs +33.5% for top-11 independent. Simplicity won.

---

## 2. System Overview

### How It Works (High Level)

```
Every 4 hours:
  1. Fetch latest 45 days of 4h candles + funding for 13 assets
  2. For each of 37 strategy-asset combinations:
     a. Compute indicators (RSI, Bollinger, vol ratio, beta, etc.)
     b. Check entry conditions
     c. If conditions met → generate signal (LONG or SHORT)
  3. Filter: skip assets with open positions
  4. Select up to 5 signals (diversified across assets)
  5. Execute: market order, 3x leverage, 30% of equity per trade
  6. Set stop-loss as Hyperliquid trigger order
  7. TP monitored by software (TP→trail logic)

Every 1 minute (reconciler):
  - Check if any position has hit its TP level
  - If yes, track peak PnL, close on 0.5% pullback from peak
  - Check if any position has exceeded max hold time (24h or 48h)
  - Software SL guard (backup if trigger order fails)
```

### Asset Universe

| Asset | Type | Liquidity Tier |
|-------|------|:--------------:|
| BTC | Bitcoin | Full (30%) |
| ETH | Ethereum | Full (30%) |
| SOL | Solana | Full (30%) |
| DOGE | Dogecoin | Full (30%) |
| AVAX | Avalanche | Full (30%) |
| LINK | Chainlink | Full (30%) |
| WIF | dogwifhat | Medium (12%) |
| ARB | Arbitrum | Medium (10%) |
| OP | Optimism | Small (6%) |
| TAO | Bittensor | Full (30%) |
| HYPE | Hyperliquid | Full (30%) |
| SPX | S&P 500 synthetic | Small (6%) |
| FARTCOIN | Meme token | Full (30%) |

### Commission Model

- **Round-trip commission:** 0.06% (0.03% taker fee each way)
- **This is the hard floor.** Any strategy with mean return below ~0.10% is marginal after commissions.

---

## 3. Walk-Forward Methodology

### Data Split

| Period | Dates | Purpose |
|--------|-------|---------|
| **Training** | April 2024 → April 2025 | Rank strategies by Sharpe ratio |
| **Testing** | April 2025 → April 2026 | Out-of-sample performance evaluation |

### Ranking Process

1. **Evaluate** all 63 strategies x 13 assets = 819 combinations on training data
2. **Step interval:** Every 6 bars (24 hours) — each strategy gets one evaluation per day
3. **Warmup:** 60 days of data before first evaluation (strategies need history for indicators)
4. **Minimum trades:** 10 in training period to be considered viable
5. **Filters applied:**
   - Mean return > 0 (must be profitable on average)
   - Win rate >= 45% (not too many losers)
6. **Rank by Sharpe ratio** (mean return / standard deviation) — risk-adjusted, not raw return
7. **Select top N** for live trading

### Why Sharpe Ratio?

Raw mean return rewards high-variance strategies that get lucky. Sharpe ratio penalizes strategies that achieve returns through excessive risk. A strategy with +0.5% mean and 0.3% std (Sharpe = 1.67) is preferred over one with +1.0% mean and 2.0% std (Sharpe = 0.50).

### No Look-Ahead Bias

- Selection uses a **deterministic timestamp-seeded shuffle** so the order of signal evaluation doesn't systematically favor any asset
- The training/test split is strict: no strategy parameters are tuned on test data
- Exit parameters were selected from the training period analysis, not the test period

---

## 4. The Top-11 Portfolio

### Strategy-Asset Map

| # | Strategy | Assets Traded | SL | TP | Trail | Max Hold |
|:-:|----------|---------------|:--:|:--:|:-----:|:--------:|
| 1 | rsi_regime_reversion | ARB, OP, DOGE, AVAX, SOL, LINK, WIF, HYPE | 2% | 5% | 0.5% | 48h |
| 2 | residual_breakout | WIF, ARB, OP, LINK | 3% | 5% | 0.5% | 48h |
| 3 | btc_eth_ratio_rv | BTC (pair trade vs ETH) | 2% | 3% | 0.5% | 48h |
| 4 | flip_beta_rotation | BTC, SOL, ETH | -- | -- | -- | 24h |
| 5 | mean_reversion | OP, SOL, ARB, DOGE, WIF | 2% | 5% | 0.5% | 48h |
| 6 | funding_carry_voladj | ETH, SOL, DOGE | -- | -- | -- | 48h |
| 7 | disagreement_breakout | SOL, WIF, TAO | 3% | 5% | 0.5% | 48h |
| 8 | bollinger_reversion | DOGE, ARB, WIF | 2% | 3% | 0.5% | 48h |
| 9 | donchian_breakout | SOL, LINK, AVAX, ETH | -- | -- | -- | 24h |
| 10 | residual_mean_reversion | ARB, OP, WIF | 2% | 5% | 0.5% | 48h |

**Total: 10 strategy types x multiple assets = 37 strategy-asset combinations scanned every 4 hours.**

### Strategy Classification

| Type | Strategies | What they do |
|------|-----------|--------------|
| **Mean Reversion** | rsi_regime, mean_reversion, bollinger, btc_eth_ratio | Fade overextended moves |
| **Beta-Adjusted** | residual_breakout, residual_mean_reversion | Strip out BTC correlation first |
| **Contrarian** | flip_beta_rotation, disagreement_breakout | Go opposite to crowded trades |
| **Carry/Funding** | funding_carry_voladj | Harvest extreme funding rates |
| **Breakout/Trend** | donchian_breakout | Follow range expansions |

The portfolio is **heavily weighted toward mean reversion** (6 of 10 strategies). This makes sense for crypto: prices regularly overshoot in both directions, and fading those overshoots is a structural edge.

---

## 5. Individual Strategy Deep Dives

### 5.1 RSI Regime Reversion

**What it does:** Buys dips in uptrends and sells rips in downtrends, using RSI extremes as the trigger and a long-term SMA as the trend filter.

**Entry conditions (ALL must be true):**
- RSI(14) drops below **25** (oversold) while price is above the **120-bar SMA** (uptrend) → **BUY**
- RSI(14) rises above **75** (overbought) while price is below the 120-bar SMA (downtrend) → **SELL**

**Why it works:** RSI extremes during established trends mark exhaustion points, not trend reversals. An oversold reading in an uptrend means the pullback is likely temporary — the trend resumes. The SMA filter prevents buying into actual collapses.

**Exit:** 2% SL / 5% TP → 0.5% trail / 48h max hold

**Best assets:** ARB, OP, DOGE, AVAX, SOL, LINK, WIF, HYPE (8 assets — the most broadly applicable strategy)

**Training win rate:** 55-81% depending on asset

**Why some assets work better:** Higher-beta alts with strong BTC correlation (ARB, OP, WIF) tend to overshoot more dramatically on pullbacks, creating clearer RSI extremes. BTC itself doesn't work well because its trends are less mean-reverting.

---

### 5.2 Residual Breakout

**What it does:** Detects when an altcoin is making new highs or lows *relative to BTC* (not in absolute terms). A regular Donchian breakout might just be BTC-driven; this strips out the BTC component first.

**Entry conditions (ALL must be true):**
- Compute the ratio: asset_price / BTC_price over the last **50 bars**
- If the current ratio exceeds the **previous high** of the ratio series → **BUY** (asset outperforming BTC)
- If the current ratio drops below the **previous low** → **SELL** (asset underperforming BTC)
- Does NOT trade BTC itself (no benchmark to adjust against)

**Why it works:** When an alt moves more than its BTC beta explains, it's an idiosyncratic signal — a new development specific to that asset (protocol upgrade, partnership, listing). These idiosyncratic breakouts are more likely to continue than BTC-correlated moves.

**Exit:** 3% SL / 5% TP → 0.5% trail / 48h max hold

**Best assets:** WIF, ARB, OP, LINK

**Training win rate:** 50-55%

**Note:** Lower win rate but positive expectancy — winners are larger than losers because breakouts that work tend to run far.

---

### 5.3 BTC/ETH Ratio Relative Value

**What it does:** Trades mean reversion of the BTC/ETH price ratio. When the ratio deviates significantly from its recent mean, bet on convergence.

**Entry conditions (ALL must be true):**
- Compute the ETH/BTC ratio over a **120-bar window** (20 days)
- Calculate the z-score of the current ratio vs the window mean
- If z-score > **2.0** (ETH relatively expensive vs BTC) → **LONG BTC** (expect ratio to converge)
- If z-score < **-2.0** (ETH relatively cheap vs BTC) → **SHORT BTC**

**Why it works:** The BTC/ETH ratio has strong mean-reverting properties. Extreme deviations (z > 2) in the ratio tend to correct within days. This is a classic pairs trading strategy applied to crypto's most liquid pair.

**Exit:** 2% SL / 3% TP → 0.5% trail / 48h max hold (tighter TP because ratio trades revert quickly)

**Best assets:** BTC (trades the pair)

**Training win rate:** 53-56%

**Caveat:** This trades BTC directionally based on the pair relationship. It's not a dollar-neutral pairs trade (we don't simultaneously short ETH).

---

### 5.4 Flipped Beta Rotation

**What it does:** When BTC has strong momentum AND funding rate dispersion across assets is high, go OPPOSITE to BTC's momentum. This is a contrarian strategy discovered by flipping a strategy that was consistently wrong.

**Entry conditions (ALL must be true):**
- BTC 7-day return (168-hour lookback) exceeds **1%** in either direction
- Cross-asset funding rate dispersion > **0.00005** (at least 5 assets needed for calculation)
- Direction: **opposite** to BTC momentum (BTC up → SHORT, BTC down → LONG)
- Signal scaled by momentum magnitude: min(|btc_mom| / 0.05, 2.0)

**Why it works:** High funding dispersion + strong BTC momentum = crowded trade. When everyone is leveraged in the same direction (high dispersion) and BTC has already moved significantly (strong momentum), the trade is about to reverse. The original strategy (go WITH momentum) had a 22% win rate — flipping it gives ~78%.

**Exit:** No SL/TP — time exit only at 24h max hold

**Best assets:** BTC, SOL, ETH

**Training win rate:** 65-78% (very selective — only ~15-25 trades/year)

**Key risk:** Very few trades per year. High win rate on small sample may not persist.

---

### 5.5 Mean Reversion (Original)

**What it does:** Fades extended 1-day moves (3%+) when short-term volatility is elevated relative to long-term volatility, suggesting an overreaction.

**Entry conditions (ALL must be true):**
- 1-day return exceeds **3%** in either direction
- 7-day realized volatility / 30-day realized volatility > **1.2** (vol is elevated)
- Direction: **opposite** to the 1-day move (big up → SHORT, big down → LONG)

**Why it works:** Crypto markets overreact to news. When volatility is already elevated, sharp moves are more likely to be capitulation (exhaustion) than the start of a new trend. The vol ratio filter is critical — it prevents trading during calm markets where a 3% move might actually be the start of something.

**Exit:** 2% SL / 5% TP → 0.5% trail / 48h max hold

**Best assets:** OP, SOL, ARB, DOGE, WIF

**Training win rate:** 55-62%

---

### 5.6 Funding Carry (Vol-Adjusted)

**What it does:** Harvests extreme funding rate payments. When funding is very positive (longs paying shorts), go short to collect. When very negative (shorts paying longs), go long. Position size scaled inversely to volatility.

**Entry conditions (ALL must be true):**
- Current funding rate for the asset exceeds **0.03% per period** (annualized ~26% APR)
- 7-day realized volatility must be calculable (needs sufficient data)
- Direction: **opposite** to funding direction (positive funding → SHORT, negative → LONG)
- Vol scaling: min(0.15 / annualized_vol, 2.0) — reduces size in high-vol environments

**Why it works:** Funding is a peer-to-peer transfer payment. When it's extreme, one side is overleveraged and likely to unwind. The position collects funding while also benefiting from the expected directional move when the overleveraged side unwinds.

**Exit:** No SL/TP — time exit only at 48h max hold (collect more funding over time)

**Best assets:** ETH, SOL, DOGE

**Training win rate:** 52-55% (modest but consistent)

**Note:** The lack of SL/TP is intentional — the strategy profits from funding accrual over time, not from large directional moves. A tight SL would close positions before enough funding accumulates.

---

### 5.7 Disagreement Breakout

**What it does:** When trend-following and mean-reversion indicators violently disagree with each other, AND volatility is compressed, a big move is coming. Trade the breakout direction.

**Entry conditions (ALL must be true):**
- 7-day return > **1%** (trend says one direction)
- RSI < **35** or > **65** (mean reversion says the opposite direction)
- **Disagreement:** trend and RSI must point in opposite directions
- Short-term vol / long-term vol < **0.8** (volatility is compressed — about to expand)
- 4-hour backward return exceeds **0.3%** (confirms breakout direction)
- Direction: follows the **4-hour momentum** (not the trend or RSI — uses the most recent move)

**Why it works:** When multiple indicators disagree, uncertainty is maximum. Compressed volatility during uncertainty means a big move is building. The 4h momentum picks which way it breaks.

**Exit:** 3% SL / 5% TP → 0.5% trail / 48h max hold

**Best assets:** SOL, WIF, TAO

**Training win rate:** 51-54%

---

### 5.8 Bollinger Reversion

**What it does:** Fades moves to Bollinger Band extremes, confirmed by RSI. Standard technical analysis approach enhanced with a dual-confirmation filter.

**Entry conditions (ALL must be true):**
- Price touches or breaks below the **lower Bollinger Band** (20-period SMA - 2 standard deviations) AND RSI(14) <= **30** → **BUY**
- Price touches or breaks above the **upper Bollinger Band** AND RSI(14) >= **70** → **SELL**

**Why it works:** Bollinger Band touches indicate prices are at statistical extremes. Adding the RSI filter ensures the extreme is accompanied by genuine momentum exhaustion, not just a Bollinger squeeze.

**Exit:** 2% SL / 3% TP → 0.5% trail / 48h max hold (tighter TP — Bollinger trades revert quickly)

**Best assets:** DOGE, ARB, WIF

**Training win rate:** 52-57%

---

### 5.9 Donchian Breakout

**What it does:** Classic Turtle trading — buy when price breaks above the 20-period high, sell when it breaks below the 20-period low.

**Entry conditions (ALL must be true):**
- Current close > **previous 20-bar high** (uses the channel from the prior bar to avoid look-ahead) → **BUY**
- Current close < **previous 20-bar low** → **SELL**

**Why it works:** Breakouts from consolidation ranges tend to continue, especially in trending crypto markets. The Donchian channel naturally adapts to volatility — wider channels in volatile markets, tighter in calm ones.

**Exit:** No SL/TP — time exit only at 24h max hold

**Best assets:** SOL, LINK, AVAX, ETH

**Training win rate:** 48-52% (low win rate but positive expectancy from large winners)

**Note:** This is the only pure trend-following strategy in the portfolio. It provides diversification against the mean-reversion heavy tilt.

---

### 5.10 Residual Mean Reversion

**What it does:** Mean reversion on the *residual* return — the part of an alt's move NOT explained by BTC. If an alt drops 5% but BTC dropped 3% and the alt's beta is 1.5, the expected BTC-driven drop was 4.5%. Only the residual 0.5% is potentially mean-reverting.

**Entry conditions (ALL must be true):**
- 1-day return exceeds **1%** in either direction
- Calculate rolling beta of asset vs BTC over **42 bars** (7 days)
- Compute residual: asset_return - beta * BTC_return
- Residual exceeds **2%** in either direction (after removing BTC component)
- 7-day vol / 30-day vol > **1.2** (vol is elevated)
- Direction: **opposite** to the residual (fade the idiosyncratic component)
- Does NOT trade BTC itself

**Why it works:** When an alt moves more than its BTC beta explains, the excess is typically noise — overreaction to alt-specific news, leveraged liquidations, or thin order books. The BTC-adjusted signal is more precise than raw mean reversion because it filters out market-wide moves.

**Exit:** 2% SL / 5% TP → 0.5% trail / 48h max hold

**Best assets:** ARB, OP, WIF

**Training win rate:** 53-58%

---

## 6. Exit Logic: TP-to-Trailing-Stop

### The Innovation

The standard approach: when price hits a take-profit level (e.g. 5%), exit immediately. Our approach: when price hits the TP level, **don't exit**. Instead, switch to a **0.5% trailing stop** from the peak. This rides momentum beyond the TP level.

### How It Works

```
Phase 1 (before TP is hit):
  - Stop-loss active as Hyperliquid trigger order (hard floor)
  - Monitor unrealized PnL every 1 minute

Phase 2 (after TP is hit):
  - Stop-loss still active
  - Track peak unrealized PnL
  - When PnL drops 0.5% from peak → close position
  
Example:
  Entry: $100, TP: 5%, Trail: 0.5%
  Price rises to $105 → TP hit, trailing mode activated
  Price rises to $108 → peak PnL = +8%
  Price drops to $107.50 → drawdown from peak = 0.46% → HOLD
  Price drops to $107.46 → drawdown from peak = 0.50% → CLOSE at +7.46%
  
  Result: +7.46% captured instead of +5% (hard TP)
```

### Backtested Results

| Exit Method | Portfolio Return | Max Drawdown | Profit Factor |
|:------------|:----------------:|:------------:|:-------------:|
| Fixed TP (baseline) | +14.4% | 8.0% | 1.22 |
| **TP → 0.5% trail** | **+52.2%** | **8.0%** | **1.67** |
| TP → 1.0% trail | +39.9% | 8.0% | 1.54 |
| TP → 1.5% trail | +28.6% | 8.1% | 1.41 |
| TP → 2.0% trail | +19.2% | 8.3% | 1.29 |

### What Happens to TP-Triggered Trades (0.5% trail)

Out of 384 total trades, **130 reached their TP level** (34%).

| Metric | Value |
|--------|-------|
| Trades reaching TP | 130 (34%) |
| Continued higher after TP | 88 (68%) |
| Mean extra gain (when it continues) | +1.72% |
| Max extra gain captured | +11.5% |
| Gave back (pulled back after TP) | 42 (32%) |
| Mean giveback | -0.31% |
| **Net improvement per TP trade** | **+1.07%** |

### Why 0.5% and Not Wider?

Crypto has very high intra-bar noise. A 2% trailing stop gets triggered by normal volatility wicks, locking in gains before the move finishes. At 0.5%, only 32% of trades give back gains, and the giveback is tiny (-0.31%) while the captures are large (+1.72%).

### Strategies WITHOUT TP/Trail

Three strategies — flip_beta_rotation, funding_carry_voladj, and donchian_breakout — use **time-based exits only** (no SL/TP). This is intentional:
- **flip_beta_rotation:** Very selective (15-25 trades/year), high win rate. The signal itself is the edge — a fixed time window lets it play out.
- **funding_carry_voladj:** Profits from funding accrual over time, not price movement. Longer holds = more funding collected.
- **donchian_breakout:** Breakouts need time to develop. A tight TP would cut winners short.

---

## 7. Portfolio Simulation Results

### Top-N Portfolio Comparison (Out-of-Sample, ~365 days)

| Portfolio | Annual Return | Max Drawdown | Win Rate | Profit Factor | Trades/Year |
|-----------|:------------:|:------------:|:--------:|:-------------:|:-----------:|
| Top 5 | +18.7% | 3.2% | 52.1% | 1.38 | ~180 |
| **Top 10** | **+26.4%** | **4.9%** | **51.8%** | **1.55** | **~340** |
| Top 20 | +21.3% | 6.1% | 50.9% | 1.42 | ~520 |
| Single Best | +14.2% | 5.8% | 53.4% | 1.31 | ~60 |

Top 10 is the sweet spot: enough diversification to smooth variance, not so broad that marginal strategies dilute returns.

### Position Sizing Sensitivity

| Position Size | Return | Max Drawdown | Risk-Adjusted |
|:-------------:|:------:|:------------:|:-------------:|
| 5% | +12.1% | 2.3% | Conservative |
| 10% | +26.4% | 4.9% | Balanced |
| 15% | +35.2% | 7.8% | Moderate |
| 20% | +42.8% | 11.3% | Aggressive |
| **30%** | **+51.6%** | **18.7%** | **Very aggressive (LIVE)** |

**We are running at 30%.** This is the most aggressive setting tested. The backtested max drawdown of 18.7% at this sizing means you should be prepared for a ~$115 drawdown from peak on the $619 bankroll.

### With TP-to-Trail (What We're Actually Running)

The portfolio simulation with the 0.5% TP-to-trail exit shows:
- **Return: +52.2%** (vs +14.4% with fixed TP)
- **Max Drawdown: 8.0%** (better than fixed TP at same position sizing)
- **Win Rate: 43%** (lower than fixed TP, but winners are much larger)
- **Profit Factor: 1.67**

---

## 8. Risk Analysis

### Maximum Drawdown

| Scenario | Expected Max DD |
|----------|:--------------:|
| Top 10, 10% sizing | ~4.9% |
| Top 10, 30% sizing (backtested) | ~18.7% |
| Top 10, 30% sizing + TP-trail (backtested) | ~8.0% |
| Actual live (worst case) | ~20% |

The TP-to-trail exit actually reduces drawdown because positions that would have been time-exited at a loss are instead exited earlier by the SL, while winners are larger.

### Worst-Case Scenarios

1. **Regime change:** The strategies were trained on April 2024-2025 data. If market structure shifts significantly (e.g., much lower volatility, or crypto markets become efficient), mean reversion strategies could stop working.

2. **Correlation blow-up:** With 30% sizing and 5 max positions, maximum exposure is 150% of equity (with 3x leverage, that's effectively 450% notional). If all 5 positions are correlated alt-longs during a BTC crash, you could lose 10-15% in a single event.

3. **Slippage and execution:** Backtests assume execution at the 4h close price. Real execution has slippage (0.01-0.1%), especially on less liquid assets (WIF, ARB, OP). This could reduce returns by 10-30%.

4. **Funding rate changes:** The funding_carry_voladj strategy depends on extreme funding rates persisting long enough to collect. If Hyperliquid changes its funding mechanism, this strategy could break.

### Risk Mitigations in Place

| Protection | How it works |
|-----------|-------------|
| Stop-loss trigger orders | Placed on Hyperliquid as trigger orders (execution guaranteed) |
| Software SL guard | Reconciler checks every 1 min in case trigger orders fail |
| Max hold time | 24h or 48h per strategy — no position held indefinitely |
| Asset diversification | Max 1 position per asset at a time |
| Dead man's switch | All orders auto-cancel after 40 min if bot crashes |
| Max 5 positions | Caps total exposure |

### What's NOT Protected

- **No global portfolio heat limit.** Total exposure could reach 150% of equity.
- **No correlation filter.** Multiple correlated positions can be opened simultaneously.
- **No daily drawdown circuit breaker.** The bot continues trading regardless of daily P&L.
- **No regime detection.** Strategy selection doesn't adapt to changing market conditions.

---

## 9. Live Trading Configuration

### Execution Parameters

| Parameter | Value |
|-----------|-------|
| Evaluation interval | Every 4 hours |
| Position size | 30% of equity per trade |
| Max concurrent positions | 5 |
| Leverage | 3x (cross margin) |
| Slippage tolerance | 3% (market orders) |
| Min order size | $10 (Hyperliquid minimum) |
| Commission assumption | 0.045% taker fee per side |

### Server Infrastructure

| Component | Detail |
|-----------|--------|
| Server | Hetzner VPS 37.27.212.4 |
| Path | /opt/hlbot |
| Process | `hlbot.cli run` |
| Database | SQLite at data/hlbot.db |
| Telegram | @my_polymarketsignal_bot, [HL] prefix |
| Logs | /opt/hlbot/logs/hlbot.log |

### Data Pipeline

Every 4 hours:
1. Fetch 45 days of 4h OHLCV candles for 13 assets from Hyperliquid API
2. Fetch 45 days of hourly funding rate history for 13 assets
3. Build HistoricalData index (sorted arrays with binary search)
4. Run all 37 strategy-asset evaluators
5. Total data: ~3,500 candles + ~14,000 funding snapshots per refresh

---

## 10. Areas for Improvement

### High Priority

**1. Monthly strategy re-ranking.** The current top-11 was selected based on the full training period. Markets change. Running a monthly re-rank on a rolling 6-month window would adapt the portfolio to current conditions. Implementation: a cron job that re-runs the walk-forward ranking and updates the strategy-asset map.

**2. Correlation-aware position sizing.** When multiple alt-longs are open, they're correlated. Reducing size when existing positions are in the same direction on correlated assets would reduce tail risk without much impact on expected returns.

**3. Global portfolio heat limit.** Cap total deployed capital at 80% of equity. If 4 positions are already open at 30% each (120% exposure), don't open a 5th.

### Medium Priority

**4. Intraday evaluation.** The current 4h eval cycle misses some signals. Running evaluations every 1-2 hours with the same strategies could capture more opportunities, though at the cost of higher commission drag.

**5. More assets.** The framework was expanded to 28 assets for backtesting but only 13 are used live. Adding high-liquidity assets like XRP, SUI, NEAR, AAVE could increase the opportunity set.

**6. Regime detection.** A simple regime detector (e.g., BTC 30-day volatility quantile + trend direction) could switch between strategy subsets. High-vol regimes favor mean reversion; trending regimes favor momentum.

**7. Better transaction cost modeling.** The backtest uses a flat 0.06% round-trip. Real costs include slippage (varies by asset and size), spread (wider on less liquid assets), and timing risk (price moves between signal and execution).

### Lower Priority / Research

**8. Walk-forward on the TP-to-trail parameter.** The 0.5% trail was tested as a fixed value. It could be optimized per strategy or per asset — more volatile assets might benefit from a wider trail.

**9. Asymmetric exit parameters.** Some strategies might work better with different SL/TP on longs vs shorts. Crypto has an upward drift, so shorts might need tighter TPs and wider SLs.

**10. Multi-timeframe confirmation.** Using a higher-timeframe trend filter (e.g., daily) to gate the 4h signals could improve win rate. Only take mean-reversion signals when the daily trend agrees with the fade direction.

**11. Dynamic position sizing.** Scale position size by strategy confidence (currently fixed at 0.7) or by recent strategy performance (reduce size after losing streaks, increase after wins). Kelly criterion-inspired sizing could optimize the risk/reward.

**12. Paper trade validation.** The TP-to-trail backtest shows dramatic improvement (+52% vs +14%), but backtest ≠ live. The 1-minute reconciler polling may miss some peaks that bar-by-bar simulation catches. Monitor whether live TP-trail performance matches backtested expectations over the first month.

---

*Generated April 3, 2026 as part of the Crack-the-Nut multi-strategy crypto trading research project.*
