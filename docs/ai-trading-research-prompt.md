# AI-Integrated Crypto Trading: Research Prompt

> **This is a prompt, not a report.** Feed this to a deep-research tool (Claude, Gemini Deep Research, Perplexity Pro, etc.) to produce a detailed implementation-aware analysis. The companion docs are:
> - [`ai-trading-thesis.md`](ai-trading-thesis.md) — the core thesis and architecture
> - [`ai-trading-mvp.md`](ai-trading-mvp.md) — the implementation blueprint

---

## Instructions

Produce a detailed, current, implementation-aware report on how to build and improve AI-integrated crypto trading bots for a builder who already has working rule-based bots and wants to move toward an adaptive AI-assisted architecture.

This is NOT a request for a generic overview of algorithmic trading. Focus on crypto specifically, and focus on practical system design, model integration, evaluation, and deployment.

## Audience and context

Assume the reader already has functioning rule-based crypto bots with explicit entry/exit logic, parameter sets, and execution plumbing. The missing piece is the AI layer.

The goal is not "build an AI that predicts price from scratch and decides every trade."
The goal is to design a system that can:

1. Learn from large historical crypto datasets, not just its own limited live history.
2. Detect and label market conditions/regimes.
3. Understand that markets have multiple simultaneous time horizons.
4. Adapt strategy choice, parameterization, and risk based on those conditions.
5. Improve over time through retraining, walk-forward evaluation, champion/challenger testing, and disciplined deployment.

## Core thesis to evaluate

Stress-test this thesis:

A layered architecture is more robust than an end-to-end autonomous AI trader.

Target architecture:

- Data layer
- Feature layer
- Regime layer
- Strategy layer
- Decision/meta-controller layer
- Execution/risk layer
- Learning/evaluation layer

Assess whether this is currently the strongest practical architecture for serious builders, and where it breaks down.

## Central design concept

The system should reason across stacked time horizons, for example:

- 3-month downtrend
- 1-week uptrend
- 8-hour downtrend

It should be able to represent the current market state as something like:

`[quarterly regime, weekly regime, daily regime, intraday regime]`

Then use that regime vector to answer:

- What worked historically?
- Under what market regime did it work?
- What regime are we in now?
- What regime are we likely entering?
- Which strategy or strategy mix should run now?
- How should parameters and risk shift as the regime changes?

## Research questions

### 1. Data sources

Compare the best historical and live crypto market data sources for AI-integrated bots.

At minimum compare: CoinGecko, CoinAPI, Tardis.dev, exchange-native APIs, CCXT as an access layer.

Include: asset coverage, exchange coverage, raw vs normalized data, spot vs perpetuals/futures/options, OHLCV vs trades vs quotes vs order-book depth, funding/open interest/liquidations, on-chain/news/sentiment/macro context, historical depth, data quality, Python usability, rate limits, pricing, backtesting suitability, live-trading suitability.

Important: explicitly assess TradingView as a data source. Its charting docs center on connecting your own backend datafeed, not providing a general historical market-data download API. Treat unofficial wrappers as unofficial and assess reliability, maintenance, and legal risk.

### 2. Research / backtesting / live stack

Compare: CCXT, vectorbt, backtrader, Freqtrade/FreqAI, other serious frameworks.

Distinguish: research stack vs simulation/backtesting stack vs live trading stack.

Recommend best combinations for: solo builder/low budget, serious retail quant, small team moving toward production.

### 3. Market regime detection

Cover: trend/range/non-trend classification, volatility regime detection, bull/bear/transition states, rolling-feature labeling, clustering, hidden Markov models, change-point detection, state-space models, multi-timeframe regime stacking, cross-asset context, derivatives context (funding, open interest, liquidations), whether LLMs are useful here or mostly irrelevant.

Give special attention to hierarchical regimes across long/medium/short/intraday horizons.

### 4. AI integration patterns

Compare and rank by robustness vs hype: predictive model only, regime classifier + rule-based execution, parameter optimizer, strategy selector/meta-controller, ensemble system, reinforcement learning, LLM-assisted system.

Be opinionated about which are mature, fragile, or overhyped.

### 5. Historical learning and ongoing adaptation

Design a practical learning loop: collect data → engineer features → label regimes/outcomes → train → backtest → walk-forward test → deploy small → collect live outcomes → retrain → champion/challenger comparison.

Cover: retraining frequency, avoiding leakage/overfitting, drift monitoring, model versioning, feature stability, realistic fees/slippage/latency, how to know the system is actually improving.

### 6. Strategy families

Survey: trend following, mean reversion, breakout, momentum, market making, basis/funding, pairs/stat arb, volatility/regime-conditioned, microstructure-aware, DEX-specific.

For each: needed data, relevant timeframes, regimes where it works, where AI helps most, biggest traps.

### 7. Practical architecture recommendation

Recommend V1 (MVP), V2 (first upgrades), V3 (professional). For each: data sources, research framework, backtesting framework, live stack, AI layer, storage, logging, monitoring.

### 8. Safety and realism

Be explicit about: why most bots fail, where AI makes systems worse, overfitting, non-stationarity, regime shifts, exchange/counterparty risk, liquidity illusions, survivorship bias, lookahead bias, hyperparameter over-optimization, why no model guarantees profitability.

## Output requirements

Produce sections: Executive summary, Key conclusions, Tooling landscape, Data-source comparison table, AI architecture patterns, Regime detection methods, Strategy families, Recommended build path, Stack recommendations, Evaluation methodology, Failure modes, Concrete 30-day next steps, MVP architecture, Suggested follow-up prompts.

Include where useful: tables, text architecture diagrams, decision trees, example feature sets, example pipelines.

## Research standards

- Prioritize practical systems a serious solo builder could implement
- Focus on crypto specifically
- Distinguish mature from experimental
- Prefer official docs and credible practitioner sources
- Cite clearly, be honest about uncertainty, avoid hype
