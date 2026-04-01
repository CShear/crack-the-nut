# AI Trading Thesis: State Estimation, Not Trade Generation

**Version:** 1.0 | **Date:** March 31, 2026 | **Audience:** crack-the-nut contributors

This is our north star document. Read it before writing code, designing a strategy, or evaluating a result. If something we build contradicts what's written here, either this document is wrong and we update it, or the code is wrong and we fix it.

---

## 1. The Problem We're Solving

We built three trading bots over seven weeks. Each one taught us the same lesson from a different angle.

**Polymarket bot** -- whale tracking plus an AI probability ensemble. Paper traded to +$34,148 on 450 signals. Live traded to -$13.40 on 58 trades. The AI ensemble pattern (3 Claude calls, median probability, variance-derived confidence) genuinely works for probabilistic assessment. But paper trading assumed midpoint fills on thin order books where real FOK orders fill at the ask. A 2-5% spread per trade annihilated the edge. The signal was real; the execution model was fiction.

**Hyperliquid perps bot** -- three parallel strategies (whale tracker, funding sniper, liquidation rider) feeding a signal combiner. Only the funding sniper ever traded live. Of 2,328 signals, 2,327 came from funding. Whale tracker generated zero signals because the discovery threshold was set for mainnet-sized flow on altcoin-sized markets. Liquidation rider generated one signal but never acted on it. The funding sniper earned +$7.06 on $599 bankroll in 15 hours, validating that harvesting extreme funding rates is a real edge even at small scale. Everything else was dead weight.

**LP bot** -- concentrated liquidity market making on Base. Paper showed $57 in fees on $2,500 capital in two hours. Live lost $49 in 45 hours. The 0.05% fee tier on cbBTC/USDC generates micro-fees per swap, and 10-minute rebalancing costs (gas plus slippage) exceeded fee income. The real insight: at small scale, pure trading fees cannot cover rebalancing. You need emission rewards (gauge incentives, Merkl farming) to make the math work.

The full numbers are in [`docs/bot-performance-report.md`](bot-performance-report.md). Total capital deployed: ~$1,845. Total live PnL: ~-$55.

Three different markets, three different strategies, three consistent findings:

1. **Paper trading always lies.** Every bot showed dramatically better paper results than live. The gap comes from execution costs that paper trading ignores. Any system that doesn't model slippage, fees, gas, and latency is producing entertainment, not information.

2. **Simple strategies beat complex ones at small scale.** The funding sniper -- a conceptually trivial strategy (extreme rate, take opposite side) -- was the only consistently profitable approach across all three bots. Whale tracking, liquidation riding, and multi-factor AI signals all showed promise in backtesting and failed in live execution at $100-600 bankroll.

3. **The missing piece is not smarter entries.** All three bots apply the same logic regardless of market conditions. They don't know if BTC is in a 3-month downtrend or a 1-week rip. They run the funding sniper with identical parameters in a low-vol ranging market and a liquidation cascade. The first step we took toward fixing this -- using BTC+ETH+SOL 12-hour return as a crude single-number regime signal for a Funding x Momentum strategy -- tripled the Sharpe ratio in backtest. That's not a coincidence. That's the signal telling us where the alpha lives.

The bots don't need smarter entries. They need to know WHEN to run WHICH strategy and WITH what parameters.

---

## 2. The Core Architecture

A layered system where AI operates in the middle and hard rules stay at the edges:

```
[Data Layer] --> [Feature Layer] --> [Regime Layer] --> [Strategy Layer] --> [Meta-Controller] --> [Execution/Risk] --> [Learning Loop]
     |                |                   |                   |                    |                     |                  |
  WS streams     Indicators,         AI classifies       Fixed library       Selects strategy,     RiskManager,       Walk-forward
  REST polls     rolling stats,      multi-timeframe     of strategies       picks parameter       KellySizer,        evaluation,
  on-chain       funding rates,      market state        (funding_arb,       profile, sets         kill switch,       Brier tracking,
  data feeds     order flow          into discrete       whale_copy,         risk bucket           GasGuard,          parameter
                 metrics             regime vector       multi_factor,       (0x/0.25x/0.5x/1x)   hard limits        re-evaluation
                                                         LP rebalance)
```

The key principle: **AI controls the middle; humans and hard code control the edges.**

The Data Layer and Execution/Risk layer are deterministic. They do exactly what they're told. The `RiskManager` in `execution/risk.py` checks every entry against position limits, exposure caps, daily loss limits, and kill switches. The `GasGuard` pauses on-chain bots when gas is too expensive. These are not suggestions. They are gates that cannot be overridden by any AI layer.

The Feature Layer and Regime Layer are where AI earns its keep. Converting raw price data into a regime classification, then selecting which strategy to run and with what risk budget -- that's the problem where pattern recognition at scale actually helps.

---

## 3. The Regime Vector

This is the central concept. Instead of asking "should I buy or sell right now?" we ask "what kind of market are we in right now?" and let that answer determine everything downstream.

Represent the market as a multi-timeframe state vector:

```
R_t = [quarterly_trend, weekly_structure, daily_volatility, intraday_execution]
```

Each dimension is a discrete category, not a continuous value. Example:

```
R_t = [bear_trend, weekly_rebound, high_vol, intraday_pullback]
```

This is a specific, actionable state. It tells us: we're in a macro bear market, but this week is a counter-trend bounce, volatility is elevated, and intraday we're seeing a pullback within that bounce. Different strategies perform differently under this state than they would under `[bull_trend, weekly_continuation, low_vol, intraday_trend]`.

**Practical taxonomy (start here, expand only when data justifies it):**

| Timeframe | Categories |
|-----------|-----------|
| Quarterly trend | bull / bear / transition |
| Weekly structure | trend / range / reversal attempt |
| Daily volatility | low / normal / high / shock |
| Intraday | continuation / pullback / chop |

That's 3 x 3 x 4 x 3 = 108 possible states. In practice, many combinations don't occur or collapse into each other. But even a rough classification enables the question that matters: **"What worked historically under these conditions?"**

The `MomentumScorer` in `scoring/momentum.py` is a primitive version of one dimension of this vector -- it tracks rolling price changes and flags trending vs mean-reverting states. The regime vector generalizes this across timeframes.

**What this enables:**

- Strategy selection: "In `[bear, range, high_vol, chop]`, only run funding sniper at 0.25x risk. Disable whale copy entirely."
- Parameter adaptation: "In `[bull, trend, low_vol, continuation]`, widen LP range to capture fees with less rebalancing."
- Risk budgeting: "In `[transition, reversal_attempt, shock, *]`, go to 0x (sit out) until the regime stabilizes."

---

## 4. What AI Should (and Shouldn't) Control

This is where most people get it wrong. They give AI full autonomy over trade decisions because it sounds impressive. Then they lose money because AI is optimizing the wrong objective, or overfitting, or hallucinating conviction.

### AI controls (bounded):

**Regime classification.** Given features, classify the current multi-timeframe state. This is a categorization problem -- exactly the kind of thing language models and classifiers are good at.

**Strategy selection from a fixed library.** The strategies in `strategies/examples/` -- `funding_arb.py`, `whale_copy.py`, `multi_factor_signal.py` -- are the playbook. AI picks which ones to activate. It does not invent new strategies at runtime.

**Parameter profile selection.** Each strategy has predefined parameter profiles (conservative, moderate, aggressive). AI selects a profile. It does not set arbitrary parameter values. The difference matters: choosing between `{threshold: 0.01, size: 0.03}` and `{threshold: 0.005, size: 0.05}` is bounded. Letting AI set `threshold=0.000001` is not.

**Risk bucket assignment.** Discrete levels: 0x (sit out), 0.25x (minimum), 0.5x (reduced), 1.0x (full). Not continuous. This prevents the AI from gradually creeping risk up in a way that looks fine until it doesn't.

**Confidence scoring.** The `BinaryCalibrator` in `agents/binary_calibration.py` is the template for how AI should contribute to decisions. It takes a raw LLM probability estimate and shrinks it toward 0.5 with a measurable `shrink_factor`. It tracks its own Brier score over time via `CalibrationStats`. When overconfidence drifts, `suggested_shrink_factor` tells you exactly how much to adjust. That's AI helping with a measurable, bounded contribution -- not AI making unconstrained decisions.

The `AdversarialAnalyst` in `agents/adversarial_ensemble.py` is another example. Instead of three temperature-diverse calls that all share the same framing bias, it forces YES/NO/neutral perspectives with different system prompts. The `tension` metric -- the gap between the YES-advocate's estimate and the NO-advocate's estimate -- is a natural confidence signal. High tension means genuine uncertainty. Low tension means the evidence is one-sided. That structural disagreement is more informative than any single model's confidence score.

### AI does NOT control:

- **Max position size** (`RiskConfig.max_position_size_pct`)
- **Max leverage**
- **Kill switches** (`RiskConfig.kill_switch_pct` -- currently 20%)
- **Max positions** (`RiskConfig.max_positions`)
- **Daily loss limits** (`RiskConfig.max_daily_loss_pct`)
- **Exchange connection handling** (the adapter layer)
- **Order execution mechanics** (FOK, limit, market -- decided by strategy, not AI)
- **Minimum order size** (`RiskConfig.min_order_usd`)

These are hard limits set by humans and enforced in code. The `RiskManager.check_entry()` method returns a rejection reason string or `None`. There is no override mechanism. There is no "AI is really confident so let's bypass the kill switch" path. If you're tempted to add one, re-read the performance report.

---

## 5. Why Not End-to-End AI?

This is the question everyone asks. If AI is so capable, why not let it make all the decisions? Here's why, grounded in what we've actually seen:

**RL agents need millions of episodes to converge.** Crypto market regimes shift faster than convergence. By the time the agent learns that its policy works in a ranging market, the market has transitioned to a trending one. The policy that was converging is now diverging. With a $1-5K bankroll, you cannot survive the exploration phase. Every "exploration" trade is real money lost.

**Non-stationarity kills learned policies.** The distribution of returns, volatility, correlation structure, and liquidity all shift over time. A model trained on 2024 data faces a different market in 2026. This isn't a solvable problem -- it's a fundamental property of financial markets. The best you can do is detect when the regime has shifted and adapt. Which is exactly what the regime vector approach does, without pretending the underlying distribution is stable.

**LLMs don't have a loss function aligned with trading.** An LLM trained on internet text will generate plausible-sounding trade rationales. Plausibility is not profitability. The Polymarket bot's AI ensemble had a genuine edge in probability estimation (54% win rate on 450 paper trades is statistically significant), but that's a narrow, well-defined task (estimate P(event) given evidence) where the LLM's training distribution overlaps with the problem. Asking an LLM "should I go long ETH right now?" is asking it to solve a problem its training did not optimize for.

**Small bankrolls amplify every mistake.** At $600 bankroll, a single bad trade sized at 10% costs $60. That's 10% of capital gone from one decision. An end-to-end AI system that's right 60% of the time and wrong 40% will still blow through a small bankroll during an unlucky streak. Bounded risk (discrete risk buckets, hard position limits, kill switches) is the only way to survive long enough to find out if the system works.

**Selection bias in "AI trading" content is extreme.** For every person sharing their AI trading bot's +300% return, there are hundreds who lost money and said nothing. The ones who share profitable results usually cherry-picked the time period, didn't include fees, or ran the strategy on one asset during one favorable regime. Ask them to show the walk-forward test across multiple regimes. Most can't.

---

## 6. The Anti-Hype Checklist

Before believing any result -- yours, ours, or anyone else's -- run it through these ten questions:

1. **Did it survive purged walk-forward testing?** Not just in-sample/out-of-sample splitting, but walk-forward with a purge gap to prevent lookahead from overlapping windows. If you only tested on one contiguous block of data, you don't have a result.

2. **Did I include realistic fees, slippage, and latency?** The Polymarket bot's paper-to-live gap came almost entirely from execution costs. If your backtest fills at the midpoint, it's lying to you.

3. **Is the edge concentrated in one lucky period?** Run the same strategy on rolling 30-day windows. If the PnL comes from one week and the other 29 days are flat or negative, you found a coincidence, not a strategy.

4. **Does it still work after reducing complexity?** Remove the least important feature. Remove the second least important. Keep going until it breaks. If a 3-feature model performs 95% as well as a 12-feature model, use the 3-feature model. The simpler model will generalize better.

5. **Does it still work when I perturb parameters?** Shift your threshold by +/-20%. If the strategy goes from profitable to catastrophic, you're overfit to specific parameter values. Robust strategies degrade gracefully.

6. **Is the model choosing structure, or memorizing noise?** If your model has more parameters than your dataset has independent observations, it's memorizing. This is especially common with neural networks on daily candle data (252 trading days per year is not a lot of data).

7. **Did live paper behavior match backtest assumptions?** Our LP bot paper traded +$54 in 2 hours. Live lost $49 in 45 hours. The paper assumptions (instant fills, no slippage, no gas estimation failure) were wrong. Paper trading is only useful if it models the same frictions as live.

8. **Is the improvement real after turnover and drawdown?** A strategy that returns 50% but has 80% max drawdown is not better than one that returns 20% with 10% max drawdown. Account for the path, not just the endpoint.

9. **Would I still trust this without the prettiest chart?** Equity curves can be made to look good with selective time windows, log scale, and cherry-picked starting points. Look at the distribution of individual trade returns, not the cumulative curve.

10. **Did the AI add value, or just add narrative?** AI is very good at generating convincing explanations for random data. If you remove the AI component and use a simple heuristic, does the result change meaningfully? The Funding x Momentum backtest showed that a crude single-number regime signal (BTC+ETH+SOL 12h return) tripled the Sharpe ratio. That's a simple heuristic outperforming no regime awareness at all. AI should beat that heuristic to justify its complexity. If it doesn't, use the heuristic.

---

## 7. What This Means for Us

We are not building an "AI trader." That framing leads to end-to-end systems, unconstrained optimization, and the failure modes described above.

We are building an **adaptive framework** that makes our existing strategies smarter about when and how they run. The strategies themselves -- funding arbitrage, whale tracking, concentrated liquidity -- are the library. The AI layer reads the market, picks from the library, and dials the risk.

**The sequence matters. Each step earns the right to the next:**

**Step 1: Regime detection across timeframes.** Build the regime vector. Classify the market state using features we already have (price momentum, volatility, funding rates, order flow). Validate that the classification is stable (same regime should persist for hours, not flip every candle). This is the foundation. Nothing else works without it.

**Step 2: Strategy selection and parameter adaptation.** Map regime states to strategy configurations. In `[bear, range, high_vol, chop]`, activate funding sniper at conservative parameters. In `[bull, trend, low_vol, continuation]`, activate whale copy at moderate parameters. Start with manual mapping based on backtest analysis, then let AI learn the mapping from walk-forward evaluation.

**Step 3: Learning loop with walk-forward evaluation.** The system evaluates its own regime classifications and strategy selections against actual outcomes. When it detects drift (the regime it classified as "range" actually behaved like "trend"), it logs the miss and adjusts. This is where `CalibrationStats.suggested_shrink_factor` scales up -- from calibrating individual probability estimates to calibrating the entire regime-to-strategy pipeline.

**Autonomy increases only as earlier layers prove value.** If regime detection doesn't improve strategy selection in walk-forward tests, we don't move to Step 2. If strategy selection doesn't improve risk-adjusted returns, we don't move to Step 3. Every layer has to earn its place with out-of-sample evidence.

The crack-the-nut toolkit already has the building blocks: `Strategy` base class for the strategy library, `RiskManager` and `KellySizer` for bounded execution, `BinaryCalibrator` and `AdversarialAnalyst` for bounded AI, `MomentumScorer` as a primitive regime feature, and the backtest engine for validation. What we're building next is the connective tissue -- the regime layer and meta-controller that tie these pieces together.

That's the thesis. AI for state estimation and adaptation, not for unconstrained trade generation. Simple strategies, smart selection, hard limits. Prove each layer before building the next one.
