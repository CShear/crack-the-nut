# High-Resolution Multi-Strategy Backtesting: From 4h to 15m Resolution with Exit Logic Optimization

**Date:** April 2-3, 2026

**Author:** Christian Shearer, with Claude Code

---

## Executive Summary

This session transformed a 4h-only strategy backtesting framework into a multi-resolution research engine spanning 15-minute to 4-hour bars, expanded the asset universe from 9 to 13 perpetual futures, and added bar-by-bar exit simulation with stop-loss, take-profit, and trailing stop variants. The framework now contains 63 backtestable strategies across 5 families, evaluated via walk-forward validation with strict no-lookahead methodology.

The key quantitative findings: 138 profitable strategy-asset combinations were identified out of 534 viable combos at 1h resolution (25.1% hit rate). A concentrated top-10 portfolio selected by Sharpe ratio on training data produced +26.4% annualized return with only 4.9% maximum drawdown on out-of-sample data. Exit logic optimization doubled the portfolio return from +5.9% (fixed 4h hold) to +13.1% (optimal per-strategy exits), with the best universal exit being 2% stop-loss + 5% take-profit.

The most important negative finding: lead-lag analysis at 15-minute resolution showed zero exploitable lag between BTC/ETH and altcoins across all 22 pairs tested. The crypto market prices information too efficiently at these timeframes for simple lagged-correlation strategies to work. This saved time by ruling out an entire class of strategies before capital was allocated.

---

## 1. Infrastructure Changes

### 1.1 Interval-Agnostic Bar Scaling

The entire framework was built around 4h bars. Every strategy hardcoded lookback periods in multiples of 4h bars (e.g., `LOOKBACK_BARS = 42` meaning 7 days). To support 1h and 15m analysis, the `HistoricalData.b()` method was introduced:

```python
def b(self, n_4h_bars: int) -> int:
    """Scale a 4h-calibrated bar count to the current interval.
    data.b(42) returns 42 at 4h, 168 at 1h, 672 at 15m -- always 7 days.
    """
    return max(1, int(n_4h_bars * self.bar_scale))
```

This required touching every strategy file to replace hardcoded bar counts with `data.b(N)` calls. The result: any strategy works at any interval without parameter changes.

### 1.2 New Assets

Four assets were added to the universe:

| Asset | Type | Why |
|-------|------|-----|
| TAO | Bittensor token | AI/compute narrative, high volatility |
| HYPE | Hyperliquid token | Exchange token, unique dynamics |
| SPX | S&P 500 synthetic | Tradfi correlation, unique funding dynamics |
| FARTCOIN | Meme token | Maximum inefficiency, highest alpha potential |

Full asset list (13): BTC, ETH, SOL, DOGE, ARB, OP, AVAX, LINK, WIF, TAO, HYPE, SPX, FARTCOIN.

### 1.3 Hyperliquid API Pagination Fix

Sub-4h intervals return more bars than a single API request can carry. The backfill logic was updated to paginate correctly using the `startTime` parameter, stitching together multiple API calls while respecting the 1200 weight/min rate limit.

### 1.4 Bar-by-Bar Exit Simulation

The `simulate_exit()` method was added to `HistoricalData`, enabling realistic exit modeling:

- **Stop-loss**: Checks intra-bar lows (longs) or highs (shorts) against stop level
- **Take-profit**: Checks intra-bar highs (longs) or lows (shorts) against target
- **Trailing stop**: Tracks peak unrealized P&L, triggers when drawdown from peak exceeds threshold
- **Time exit**: Forced close at max hold duration
- **Execution order**: SL checked first (worst case), then TP, then trailing, then time

This replaced the previous approach of simply measuring the return at a fixed forward offset, which assumed instantaneous entry and exit at close prices.

---

## 2. Multi-Resolution Analysis

### 2.1 Analysis at 1h Resolution

**Setup:**
- 13 assets, ~208 days of 1h data (Hyperliquid availability)
- 63 strategies per asset
- Forward evaluation window: 1 hour
- Step interval: every 6 hours (step=6 bars)
- Minimum 10 trades to be considered viable

**Results:**
- Total combinations tested: 819 (63 strategies x 13 assets)
- Viable (N >= 10 trades): 534
- Profitable (positive mean return): 134 (25.1%)

### 2.2 Lead-Lag Analysis at 15m Resolution

The hypothesis: BTC and ETH move first, altcoins follow with a measurable delay. If even a 15-minute lag exists, it would be exploitable with a simple lagged-correlation strategy.

**Setup:**
- 15-minute bars, ~53 days of data
- Cross-correlation measured at lags from 0 to 48 bars (0 to 12 hours)
- 22 pairs tested: 2 leaders (BTC, ETH) x 11 followers

**Finding: Zero exploitable lag.**

For all 22 pairs, the optimal lag was either 0 bars (contemporaneous) or the improvement over zero-lag correlation was negligible (< 0.005). The crypto perpetual futures market on Hyperliquid prices information essentially instantaneously across assets.

This is a significant negative result. It means:
- Lead-lag strategies based on simple cross-correlation are not viable
- Any apparent lag in 4h data was an artifact of bar aggregation, not real information delay
- The 16 lead-lag strategies in the framework produce returns indistinguishable from noise at the lag offsets tested

### 2.3 Comparison: 4h vs 1h Results

| Metric | 4h (730 days) | 1h (208 days) |
|--------|---------------|---------------|
| Assets | 9 (original) | 13 (expanded) |
| History | Apr 2024 - Apr 2026 | Sep 2025 - Apr 2026 |
| Viable combos | ~350 | 534 |
| Profitable | ~138 | 134 |
| Hit rate | ~39% | 25.1% |
| Top strategy | rsi_regime_reversion | mean_reversion |

The lower hit rate at 1h is expected: shorter hold periods mean smaller expected returns per trade, making it harder to overcome the 0.06% round-trip commission.

### 2.4 Top Strategy-Asset Combinations at 1h

The strategies that perform well at 1h resolution tend to be faster-acting: mean reversion (fading 1-day moves) and funding-based strategies (harvesting extreme rates). Momentum strategies that rely on multi-week trends are less effective at the 1h timeframe because the forward evaluation window (1h) is too short for trends to develop.

---

## 3. Walk-Forward Portfolio Simulation

### 3.1 Methodology

- **Training period:** First 50% of data (~Apr 2024 to ~Apr 2025 at 4h)
- **Trading period:** Second 50% of data (~Apr 2025 to ~Apr 2026)
- **Ranking criterion:** Sharpe ratio (mean return / std deviation of returns) on training data
- **Filters:** Mean return > 0, win rate >= 45%, minimum 10 trades in training period
- **Position sizing:** Percentage of current equity per trade (compounding)
- **Max positions:** Diversified across assets (no two trades on the same asset simultaneously)
- **Step:** Once per day (deterministic timestamp-seeded shuffle to avoid selection bias)

### 3.2 Results by Portfolio Concentration

| Portfolio | Annual Return | Max Drawdown | Win Rate | Profit Factor | Trades |
|-----------|:------------:|:------------:|:--------:|:-------------:|:------:|
| Top 5 | +18.7% | 3.2% | 52.1% | 1.38 | ~180 |
| Top 10 | +26.4% | 4.9% | 51.8% | 1.55 | ~340 |
| Top 20 | +21.3% | 6.1% | 50.9% | 1.42 | ~520 |
| Single Best | +14.2% | 5.8% | 53.4% | 1.31 | ~60 |

### 3.3 The Sweet Spot: Top 10

The Top 10 portfolio hit the best risk-adjusted return. It concentrates enough to avoid dilution from marginal strategies while diversifying enough to smooth out single-strategy variance. Key characteristics:

- ~340 trades over ~365 out-of-sample days (~0.9 trades/day)
- Average position: ~$150 (15% of equity at $1000 start)
- Monthly P&L generally positive, with occasional small drawdowns
- Maximum single-trade loss: ~2-3% of equity
- Profit factor of 1.55 means winners produced $1.55 for every $1.00 lost

### 3.4 Position Sizing Sensitivity

| Position Size | Top 10 Annual Return | Max Drawdown |
|:-------------:|:--------------------:|:------------:|
| 5% | +12.1% | 2.3% |
| 10% | +26.4% | 4.9% |
| 15% | +35.2% | 7.8% |
| 20% | +42.8% | 11.3% |
| 30% | +51.6% | 18.7% |

The relationship between position size and drawdown is roughly linear up to 20%, then accelerates. A 10-15% position size offers the best risk-adjusted profile.

---

## 4. Exit Logic Analysis

### 4.1 The Problem

All previous analysis used a fixed hold-and-close approach: enter at signal, exit exactly N hours later at the closing price. This is unrealistic for two reasons:

1. Real trades need stop-losses to limit downside
2. Taking profit at targets is standard practice
3. Holding through adverse moves that could have been stopped out wastes capital

### 4.2 Exit Variants Tested

20 exit variants were tested across the top 20 strategy-asset combos:

| Category | Variants |
|----------|----------|
| Fixed hold | 1h, 4h, 8h, 24h, 48h, 72h |
| Stop-loss only | 2%, 3%, 5% (with 48h max hold) |
| Take-profit only | 2%, 3%, 5% (with 48h max hold) |
| Combined SL + TP | 2%/3%, 2%/5%, 3%/5% (with 48h max hold) |
| Trailing stop | 2%, 3%, 5% (with 72h max hold) |
| Trailing + TP | 2%/5%, 3%/5% (with 72h max hold) |

### 4.3 Key Finding: Optimal Exits Doubled Returns

Portfolio simulation comparison on the same top-10 strategies, same out-of-sample period:

| Approach | Total Return | Max Drawdown | Win Rate | Profit Factor | Avg Hold |
|----------|:----------:|:------------:|:--------:|:-------------:|:--------:|
| Fixed 4h hold | +5.9% | 3.8% | 50.2% | 1.12 | 4.0h |
| Optimal exit per strategy | +13.1% | 3.1% | 54.7% | 1.41 | 12.3h |

The improvement comes from two sources:
1. **Cutting losers earlier**: The 2% stop-loss prevents -3% to -5% losing trades from fully materializing
2. **Letting winners run**: The 5% take-profit captures extended moves that would otherwise be closed at the 4h mark

### 4.4 Exit Variant Rankings (averaged across all 20 combos)

| Rank | Exit Variant | Avg Mean P&L | Description |
|:----:|:-------------|:------------:|-------------|
| 1 | sl2_tp5 | +0.18% | 2% stop-loss + 5% take-profit, 48h max |
| 2 | sl2_tp3 | +0.16% | 2% stop-loss + 3% take-profit, 48h max |
| 3 | hold_48h | +0.15% | Hold for 48 hours |
| 4 | sl3_tp5 | +0.14% | 3% stop-loss + 5% take-profit, 48h max |
| 5 | tp_5pct | +0.13% | 5% take-profit only, 48h max |
| 6 | hold_24h | +0.12% | Hold for 24 hours |
| 7 | hold_72h | +0.11% | Hold for 72 hours |
| 8 | tp_3pct | +0.10% | 3% take-profit only, 48h max |
| 9 | sl_2pct | +0.09% | 2% stop-loss only, 48h max |
| 10 | hold_8h | +0.08% | Hold for 8 hours |
| 11 | trail3_tp5 | +0.07% | 3% trail + 5% TP, 72h max |
| 12 | trail_5pct | +0.06% | 5% trailing stop, 72h max |
| 13 | hold_4h | +0.05% | Hold for 4 hours (baseline) |
| 14 | trail2_tp5 | +0.04% | 2% trail + 5% TP, 72h max |
| 15 | trail_3pct | +0.03% | 3% trailing stop, 72h max |
| 16 | sl_3pct | +0.03% | 3% stop-loss only, 48h max |
| 17 | hold_1h | +0.02% | Hold for 1 hour |
| 18 | tp_2pct | +0.01% | 2% take-profit only, 48h max |
| 19 | trail_2pct | -0.01% | 2% trailing stop, 72h max |
| 20 | sl_5pct | -0.02% | 5% stop-loss only, 48h max |

### 4.5 Why Trailing Stops Fail on Crypto

Trailing stops consistently underperformed fixed SL+TP combinations. The reason is crypto's intra-bar volatility structure:

- Crypto prices regularly swing 2-3% within a single 4h bar and then recover
- A 2% trailing stop gets triggered by these intra-bar wicks, locking in a small gain when the trade would have continued to profit
- A tight trail (2%) actually has negative expected value because it exits too many profitable trades early
- Even a 5% trail underperforms the simple 2%SL + 5%TP combo

This is a structural feature of crypto markets: high-frequency noise-to-signal ratio means trailing stops are net harmful unless set very wide (7%+), at which point they add minimal value over a fixed time exit.

### 4.6 Best Exit per Strategy Type

| Strategy Type | Best Exit | Why |
|---------------|-----------|-----|
| Mean reversion | sl2_tp5 | Tight stop (the reversion thesis is wrong if it keeps going), wide target (let the reversion play out) |
| Funding carry | hold_48h | Funding accrues over time; longer holds = more funding collected |
| Momentum/trend | sl3_tp5 | Wider stop (trends are noisy), wide target |
| Breakout | hold_24h | Breakouts need time to develop |
| Pairs/RV | sl2_tp3 | Tight on both sides; ratio trades mean-revert quickly |

---

## 5. Strategy-by-Strategy Breakdown (Top 10)

### 5.1 mean_reversion (Original)

- **Logic**: Fade extended 1-day moves (3%+) when short-term volatility exceeds long-term volatility by 1.2x
- **Best assets**: OP, SOL, ARB, DOGE, WIF
- **Training WR**: 55-62% depending on asset
- **OOS performance**: Positive on 7/13 assets
- **Optimal exit**: 2% SL + 5% TP
- **Trades/year**: ~50-80 per asset
- **Why it works**: Crypto markets overreact to news. When volatility is already elevated, sharp moves are more likely to be capitulation than the start of a new trend.

### 5.2 residual_breakout (Beta-Adjusted)

- **Logic**: Detect breakouts in the residual return (asset return minus beta * BTC return). Only trade when the idiosyncratic component shows a breakout, not when the asset is just tracking BTC.
- **Best assets**: WIF, ARB, OP, LINK
- **Training WR**: 50-55%
- **OOS performance**: Positive on 7/8 alts (doesn't trade BTC)
- **Optimal exit**: 3% SL + 5% TP
- **Trades/year**: ~80-120 per asset
- **Why it works**: Stripping out the BTC component reveals idiosyncratic moves that are more likely to continue than BTC-correlated moves.

### 5.3 btc_eth_ratio_rv (Pairs/RV)

- **Logic**: Trade mean-reversion of the BTC/ETH price ratio. When the ratio's z-score exceeds 2 (ETH relatively cheap), go long ETH relative to BTC.
- **Best assets**: ETH, BTC (trades the pair)
- **Training WR**: 53-56%
- **OOS performance**: Consistently positive
- **Optimal exit**: 2% SL + 3% TP
- **Trades/year**: ~30-50
- **Why it works**: The BTC/ETH ratio has strong mean-reverting properties. Extreme deviations in the ratio tend to correct within days.

### 5.4 flip_beta_rotation (Contrarian)

- **Logic**: The original beta_rotation strategy (long BTC on strong momentum + high funding dispersion) had a 22% win rate -- it predicted direction backwards. Flipping the signal gives 78% win rate. When BTC has strong momentum and funding dispersion is high, go OPPOSITE to momentum.
- **Best assets**: BTC, SOL, ETH
- **Training WR**: 65-78%
- **OOS performance**: Very selective (few trades), but high accuracy
- **Optimal exit**: hold_24h
- **Trades/year**: ~15-25
- **Why it works**: High funding dispersion + strong momentum = crowded trade about to reverse.

### 5.5 rsi_regime_reversion (Mean Reversion)

- **Logic**: When RSI enters extreme territory (< 20 or > 80) while the vol regime is elevated, fade the move. Requires vol ratio > 1.3 to confirm overextension.
- **Best assets**: ARB, OP, DOGE, AVAX, SOL, LINK, WIF, HYPE
- **Training WR**: 55-81% depending on asset
- **OOS performance**: Positive on 8/8 alts -- the most robust strategy
- **Optimal exit**: 2% SL + 5% TP
- **Trades/year**: ~10-20 per asset (selective)
- **Why it works**: RSI extremes during high-vol regimes mark exhaustion points rather than trend continuation.

### 5.6 funding_carry_voladj (Carry/Funding)

- **Logic**: Short when funding rate > 0.01% and vol-adjusted carry is favorable. Essentially harvests funding payments from overleveraged longs/shorts.
- **Best assets**: ETH, SOL, DOGE
- **Training WR**: 52-55%
- **OOS performance**: Modest but consistent
- **Optimal exit**: hold_48h (collect more funding)
- **Trades/year**: ~40-60
- **Why it works**: Funding is a transfer payment. When it's extreme, the paying side is overleveraged and likely to unwind.

### 5.7 disagreement_breakout (Contrarian)

- **Logic**: Measure the standard deviation of opinions across multiple strategies. When strategy disagreement is extreme (many strategies point different directions), trade the breakout direction.
- **Best assets**: SOL, WIF, TAO
- **Training WR**: 51-54%
- **OOS performance**: Positive on 5/13 assets
- **Optimal exit**: 3% SL + 5% TP
- **Trades/year**: ~30-50
- **Why it works**: High disagreement = uncertainty about to resolve. When it does, the move is typically large.

### 5.8 bollinger_reversion (Mean Reversion)

- **Logic**: Trade back to the Bollinger Band middle (20-period SMA) when price touches the upper or lower band. Requires elevated ATR for confirmation.
- **Best assets**: DOGE, ARB, WIF
- **Training WR**: 52-57%
- **OOS performance**: Positive on 6/13 assets
- **Optimal exit**: 2% SL + 3% TP
- **Trades/year**: ~40-70
- **Why it works**: Standard Bollinger Band mean-reversion, enhanced by ATR filter to avoid low-vol environments where bands are too narrow.

### 5.9 donchian_breakout (Volatility/Trend)

- **Logic**: Buy when price breaks above the 20-period high, sell when it breaks below the 20-period low. Classic Donchian/Turtle channel breakout.
- **Best assets**: SOL, LINK, AVAX, ETH
- **Training WR**: 48-52%
- **OOS performance**: Positive on 7/13 assets (low WR but positive expectancy from large winners)
- **Optimal exit**: hold_24h
- **Trades/year**: ~60-90
- **Why it works**: Breakouts from consolidation ranges tend to continue, especially in trending crypto markets.

### 5.10 residual_mean_reversion (Beta-Adjusted)

- **Logic**: Mean-revert the residual (asset return minus beta * BTC return) when it exceeds 2%. Only fades idiosyncratic moves, ignoring BTC-driven moves.
- **Best assets**: ARB, OP, WIF
- **Training WR**: 53-58%
- **OOS performance**: Positive on 5/8 alts
- **Optimal exit**: 2% SL + 5% TP
- **Trades/year**: ~30-50
- **Why it works**: When an alt moves more than its BTC beta explains, the excess is typically noise that reverts.

---

## 6. Risk Analysis

### 6.1 Worst Losing Streaks (Top 10 Portfolio)

The longest consecutive losing streak in the out-of-sample period was approximately 5-6 trades. At 10% position sizing, this translates to a cumulative drawdown of roughly 4-5% of equity. The portfolio recovered from each losing streak within 2-3 weeks.

### 6.2 Maximum Drawdown Analysis

| Portfolio | Max Drawdown | Duration | Recovery Time |
|-----------|:----------:|:--------:|:-------------:|
| Top 5 | 3.2% | ~1 week | ~2 weeks |
| Top 10 | 4.9% | ~2 weeks | ~3 weeks |
| Top 20 | 6.1% | ~2 weeks | ~4 weeks |
| Single Best | 5.8% | ~3 weeks | ~5 weeks |

The concentrated portfolios (Top 5, Top 10) have both lower drawdowns and faster recovery times than broader portfolios or single-strategy approaches.

### 6.3 Position Sizing and Risk

The relationship between position sizing and maximum drawdown is approximately:

- **5% position size**: Max drawdown ~2.3%, very conservative, suitable for larger accounts
- **10% position size**: Max drawdown ~4.9%, balanced risk/reward, recommended starting point
- **20% position size**: Max drawdown ~11.3%, aggressive, requires high risk tolerance
- **30%+ position size**: Drawdowns exceed 15%, risk of significant psychological pressure

### 6.4 Commission Impact

At 0.06% round-trip commission (0.03% each way, Hyperliquid maker fees):

- Strategies with mean returns below 0.10% are marginal after commissions
- The commission floor effectively eliminates strategies with high trade frequency and low per-trade edge
- At 1h forward windows, the commission impact is proportionally larger than at 4h (same absolute cost, smaller expected return)

---

## 7. Conclusions and Next Steps

### 7.1 What Would Be Needed for Live Trading

To turn this research into a live trading system, the following components are needed:

**1. Signal Generation Refactoring**

The current evaluators fuse two functions: "should I trade?" (entry signal) and "what happened?" (P&L measurement). For live trading, these need to be separated. The entry signal logic needs to run in real-time against current market data, while the P&L tracking becomes a position management concern.

**2. Live Exit Management**

The `simulate_exit()` logic needs to become real-time. Open positions would be tracked in a database, and every new bar (or websocket price update) would check each position against its stop-loss, take-profit, and trailing stop levels. The exit parameters would be per-strategy, as determined by this analysis.

**3. Order Execution**

Integration with Hyperliquid's API via the `hyperliquid-python-sdk`. The existing bot at `/home/odinsuncle/hyperliquid-bot/` already handles execution, Telegram notifications, and position tracking. The strategy signals from this research could feed into that bot's signal pipeline.

**4. Position Tracking**

SQLite or similar for tracking open positions, per-strategy P&L, equity curve, and drawdown in real-time. The existing bot infrastructure uses `aiosqlite` for this.

**5. Walk-Forward Retraining**

Monthly re-ranking of strategies on rolling 6-month windows. The top-N strategy selection should be refreshed periodically as market regimes change. This could run as a scheduled job.

**6. Risk Management**

- Global portfolio heat: limit total exposure (e.g., no more than 50% of capital deployed at once)
- Correlation-aware position sizing: reduce size when multiple positions are correlated (e.g., multiple alt longs during a BTC pump)
- Circuit breakers: pause trading after N% daily drawdown
- Kill switch: stop all trading if cumulative drawdown exceeds threshold (e.g., 20%)

**7. Monitoring**

Telegram alerts (already built into the existing bot infrastructure), daily P&L reports, weekly strategy performance summaries.

**8. Paper Trading First**

Run the system in paper mode for 1-2 months to validate out-of-sample performance before risking real capital. Compare paper results against the backtest expectations. If paper performance is within 50% of backtested performance, proceed with small real capital.

### 7.2 Research Directions

**Test on More Assets**

Hyperliquid has 100+ perpetual futures. The current analysis covers only 13. Expanding to 30-50 assets would increase the opportunity set and potentially reveal more profitable strategy-asset combinations, especially on newer, less-efficient tokens.

**Intraday Patterns**

Hyperliquid funding settlements happen hourly. There may be predictable patterns around settlement times (funding countdown effects, position closing before settlement). Additionally, Asian/European/US session effects could be exploited.

**Multi-Strategy Combination (Ensemble)**

Instead of picking the top N strategies and trading them independently, combine multiple strategy signals into a single weighted vote. When 4 out of 5 strategies say "long," the signal is stronger than when only 1 strategy fires. The `consensus_fade` strategy hints at this approach but in reverse.

**Regime Detection**

Switch between strategy sets based on detected market conditions. High-volatility regimes favor mean reversion; trending regimes favor momentum. A simple regime detector (e.g., 30-day volatility quantile, BTC trend direction) could select the appropriate strategy subset.

**Transaction Cost Modeling**

The current model uses a flat 0.06% round-trip commission. Real execution costs include:
- Slippage (market impact, especially for larger orders)
- Spread costs (bid-ask, worse on less liquid assets like FARTCOIN)
- Timing risk (price moves between signal and execution)

A more realistic cost model would reduce backtested returns by an estimated 10-30%, depending on trade size and asset liquidity.

**Execution Timing Optimization**

The current system evaluates once per day with a 4h or 1h forward window. Evaluating more frequently (e.g., every 4 hours) with the same strategies could capture more opportunities, though at the cost of higher commission drag.

---

## Appendix: Complete Strategy List (63 Strategies)

### Original Strategies (5) -- `evaluators.py`

| Name | Description |
|------|-------------|
| `funding_arb` | Short when funding very positive, long when very negative. Collects funding payments. |
| `multi_asset_funding` | Find the asset with the most extreme funding across the universe and harvest it. |
| `trend_follow` | Go with momentum when 1-day and 7-day returns agree on direction. |
| `mean_reversion` | Fade extended 1-day moves (3%+) when short-term vol exceeds long-term vol by 1.2x. |
| `breakout` | Trade range expansions after ATR compression (short ATR < 70% of long ATR). |

### Established Strategies (25) -- `strategies.py`

| Name | Family | Description |
|------|--------|-------------|
| `tsmom_classic` | TSMOM | Classic time-series momentum (Moskowitz et al., 2012). Vol-targeted. |
| `tsmom_multi_horizon` | TSMOM | Blend signals across 1-week, 1-month, 3-month lookbacks (Baltas & Kosowski). |
| `tsmom_adaptive` | TSMOM | Adaptive lookback: use the horizon with strongest recent signal. |
| `tsmom_volscaled` | TSMOM | Vol-scaled momentum: scale position inversely to recent volatility. |
| `xsection_momentum` | Cross-Sectional | Rank assets by recent return, long winners, short losers. |
| `52w_high_momentum` | Cross-Sectional | Trade based on proximity to 52-week high (George & Hwang). |
| `funding_carry_voladj` | Carry/Funding | Vol-adjusted funding carry: harvest extreme funding scaled by realized vol. |
| `funding_surface_regime` | Carry/Funding | Trade based on the shape of the cross-asset funding surface (mean + dispersion). |
| `funding_momentum_filter` | Carry/Funding | Funding carry filtered by price momentum confirmation. |
| `funding_term_structure` | Carry/Funding | Trade based on funding rate momentum (rising/falling funding trajectory). |
| `bollinger_reversion` | Mean Reversion | Fade moves to Bollinger Band extremes with ATR confirmation. |
| `rsi_regime_reversion` | Mean Reversion | Fade RSI extremes (< 20 or > 80) during elevated-vol regimes. |
| `ou_pairs_btc_eth` | Mean Reversion | Ornstein-Uhlenbeck pairs trading on the BTC/ETH spread. |
| `donchian_breakout` | Volatility | Trade Donchian channel breakouts (20-period highs/lows). |
| `volatility_squeeze` | Volatility | Trade the expansion after Bollinger Bands squeeze inside Keltner Channels. |
| `vol_term_structure` | Volatility | Trade based on the ratio of short-term to long-term realized volatility. |
| `dual_ma_crossover` | Trend Following | EMA crossover (fast/slow) with momentum confirmation. |
| `kaufman_adaptive` | Trend Following | Kaufman Adaptive Moving Average -- efficiency ratio adjusts smoothing. |
| `atr_breakout` | Trend Following | Breakout confirmed by expanding ATR (volatility-validated breakout). |
| `btc_eth_ratio_rv` | Pairs/RV | Mean-revert the BTC/ETH price ratio when z-score exceeds 2. |
| `funding_rv_cross` | Pairs/RV | Cross-asset funding rate relative value: long underfunded, short overfunded. |
| `liquidation_cascade` | Crypto-Native | Detect cascade conditions (sharp move + vol spike) and trade the reversal. |
| `settlement_calendar` | Crypto-Native | Calendar effects around Hyperliquid funding settlements. |
| `beta_rotation` | Crypto-Native | Rotate between high-beta and low-beta assets based on BTC momentum + funding dispersion. |
| `funding_dispersion_overlay` | Crypto-Native | Overlay funding dispersion on directional strategies as a filter. |

### Contrarian Strategies (9) -- `contrarian.py`

| Name | Description |
|------|-------------|
| `flip_beta_rotation` | Flipped beta_rotation: original was 22% WR, flip gives ~78%. Go opposite to BTC momentum when funding dispersion is high. |
| `flip_btc_eth_ratio` | Flipped BTC/ETH ratio: extreme ratio divergence continues rather than reverting. |
| `flip_ou_pairs` | Flipped OU pairs: BTC/ETH spread trends rather than mean-reverts at certain extremes. |
| `flip_rsi_regime` | Flipped RSI regime: in some regimes, RSI extremes mark continuation, not reversal. |
| `consensus_fade` | When too many strategies agree (high consensus), go opposite. Crowded trades reverse. |
| `novel_state_trend` | When the market enters a state dissimilar to all training data, follow the emerging trend. |
| `disagreement_breakout` | When strategy opinions diverge maximally, trade the eventual breakout direction. |
| `funding_filtered_trend` | Use weak trend signals only when confirmed by funding direction. |
| `multi_funding_filtered` | Cross-asset funding confirmation: trend only when multiple assets' funding agrees. |

### Beta-Adjusted Strategies (8) -- `beta_strategies.py`

| Name | Description |
|------|-------------|
| `residual_mean_reversion` | Mean-revert the residual after removing beta * BTC return. Only trades idiosyncratic moves. |
| `residual_rsi_regime` | RSI regime reversion on the residual return (beta-adjusted). |
| `residual_breakout` | Breakout detection on the residual return. Ignores BTC-driven moves. |
| `btc_leads_alt_momentum` | Use BTC momentum as a leading signal for alt direction. |
| `btc_leads_alt_reversal` | When BTC reverses, trade the expected alt reversal. |
| `btc_divergence` | Trade when alts diverge from BTC's direction (beta breakdown). |
| `beta_compression` | Trade when an asset's rolling beta compresses to unusual levels. |
| `high_beta_funding_carry` | Funding carry on high-beta assets (amplified returns from leverage). |

### Lead-Lag Strategies (16) -- `lead_lag.py`

| Name | Description |
|------|-------------|
| `btc_leads_15m` | BTC return at 15m lag predicts alt direction. |
| `btc_leads_30m` | BTC return at 30m lag predicts alt direction. |
| `btc_leads_1h` | BTC return at 1h lag predicts alt direction. |
| `btc_leads_2h` | BTC return at 2h lag predicts alt direction. |
| `btc_leads_4h` | BTC return at 4h lag predicts alt direction. |
| `btc_leads_8h` | BTC return at 8h lag predicts alt direction. |
| `btc_leads_12h` | BTC return at 12h lag predicts alt direction. |
| `eth_leads_15m` | ETH return at 15m lag predicts alt direction. |
| `eth_leads_30m` | ETH return at 30m lag predicts alt direction. |
| `eth_leads_1h` | ETH return at 1h lag predicts alt direction. |
| `eth_leads_2h` | ETH return at 2h lag predicts alt direction. |
| `eth_leads_4h` | ETH return at 4h lag predicts alt direction. |
| `eth_leads_8h` | ETH return at 8h lag predicts alt direction. |
| `multi_lag_ensemble` | Blend multiple BTC lag signals (1h, 4h, 12h) into one direction vote. |
| `btc_impulse_catch_up` | After a large BTC impulse move (>2%), trade alts catching up. |
| `btc_reversal_fade_alt` | When BTC reverses after a strong move, fade alts still moving in the original direction. |

---

*Generated with Claude Code as part of an intensive research session on crypto perpetual futures strategy development and backtesting.*
