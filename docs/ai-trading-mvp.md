# AI-Integrated Trading Bot: MVP Blueprint

> **This is a build spec, not a research document.** For the thesis, see [`ai-trading-thesis.md`](ai-trading-thesis.md). For the full research prompt, see [`ai-trading-research-prompt.md`](ai-trading-research-prompt.md).

---

## System Goal

Build an adaptive crypto trading system that:

1. Identifies multi-timeframe market state
2. Selects among a small library of strategies
3. Adjusts parameters and risk by regime
4. Keeps execution deterministic and constrained
5. Improves through walk-forward retraining and champion/challenger evaluation

---

## Architecture

```
[Historical + Live Data]
    |-- OHLCV
    |-- trades / quotes
    |-- funding / OI / liquidations
    |-- cross-asset context
    |-- optional on-chain / news
          |
          v
[Data Lake / Research Store]
    |-- raw partitioned data
    |-- cleaned normalized datasets
    |-- feature tables
          |
          v
[Feature Engine]
    |-- trend, momentum, vol, volume
    |-- market structure
    |-- derivatives context
    |-- cross-sectional context
    |-- multi-timeframe aggregates
          |
          v
[Regime Engine]
    |-- quarterly regime
    |-- weekly regime
    |-- daily regime
    |-- intraday regime
          |
          v
[Strategy Library]
    |-- trend-following profile
    |-- mean-reversion profile
    |-- breakout profile
    |-- defensive / risk-off profile
          |
          v
[Meta-Controller]
    |-- choose strategy
    |-- weight strategies
    |-- set parameter profile
    |-- set risk bucket
          |
          v
[Execution + Risk Engine]
    |-- order placement
    |-- slippage/fee controls
    |-- position caps
    |-- leverage caps
    |-- kill switches
          |
          v
[Monitoring + Learning Loop]
    |-- backtests
    |-- walk-forward
    |-- drift checks
    |-- live telemetry
    |-- champion/challenger replacement
```

---

## MVP Scope

### Instruments

Start with liquid instruments only:

- BTC perpetual
- ETH perpetual
- 1-3 additional high-liquidity majors after pipeline is stable

Do not begin with dozens of alts.

### Timeframes

Medium-speed directional trading (not ultra-HFT):

- **Execution horizon:** 15m to 4h
- **Higher context:** daily and weekly
- **Optional long context:** monthly/quarterly features

### Strategy Families

Start with only 3:

- Trend following
- Mean reversion
- Breakout / momentum continuation

Plus a crypto-native addition:

- **Funding rate harvesting** (we already proved this works in the Funding x Momentum strategy — 70% WR, 0.426 Sharpe over 44 days)

---

## Data Plan

### Version 1

- **CCXT + exchange-native APIs** for live connectivity and initial historical data
- **Exchange-native endpoints** for funding, open interest, liquidations (CCXT is thin here)
- **Parquet + DuckDB** for local research storage
- Only add Postgres/Timescale when live persistence justifies it

### Version 2

- **Tardis.dev** for normalized trades, quotes, order-book, derivatives history
- **CoinAPI** if broad normalized OHLCV is the bottleneck
- **CoinGecko** as supplementary, not primary
- **TradingView** not as primary infrastructure

---

## Feature Plan

### Core Price/Volume

- Returns over multiple horizons
- Rolling volatility, ATR, realized vol
- Range compression/expansion
- Volume z-scores
- Trend slope, distance from MAs
- Breakout distance from rolling highs/lows

### Regime Features

- ADX-like trend strength
- Realized vol percentile
- Skew/kurtosis over rolling windows
- Autocorrelation
- Cross-timeframe agreement/disagreement
- Ratio of intraday to daily volatility

### Derivatives Context (crypto-native — V1, not V2)

- Funding rate level and change
- Open interest level and change
- Liquidation bursts
- Basis where available

Our experience confirms these are first-class signals: the Funding x Momentum strategy's entire edge comes from funding rate + momentum alignment.

### Cross-Asset Context

- BTC market state
- ETH/BTC relative strength
- Broad market breadth proxies

### Later (not V1)

- On-chain metrics
- News/sentiment
- DEX flow
- Order-book imbalance

---

## Regime Design

### Regime Vector

```
R_t = [
  quarterly_trend_regime,
  weekly_structure_regime,
  daily_volatility_regime,
  intraday_execution_regime
]
```

Example: `R_t = [bear_trend, weekly_rebound, high_vol, intraday_pullback]`

### Taxonomy (keep simple)

| Horizon   | States                                |
|-----------|---------------------------------------|
| Quarterly | bull / bear / transition              |
| Weekly    | trend / range / reversal attempt      |
| Daily vol | low / normal / high / shock           |
| Intraday  | continuation / pullback / chop        |

### Model Choices for V1

1. Hand-built regime labels as baseline (rolling-feature thresholds)
2. Clustering on rolling feature windows as comparison
3. HMM as second-stage validation
4. Change-point detection for regime boundaries

Start with hand labels. Only use ML if it beats hand labels in walk-forward testing.

---

## Meta-Controller Design

Given current features and regime vector, decide:

- Which strategy family is active
- Which parameter profile to use
- What risk bucket to assign (0x, 0.25x, 0.5x, 1.0x)
- When to stand down entirely

### Good First Models

- Gradient boosted trees (XGBoost/LightGBM)
- Regularized logistic/multinomial models
- Simple contextual bandit
- Ranking model over strategy candidates

Avoid deep nets first.

### Output Shape

The meta-controller does NOT output buy/sell. It outputs:

```
strategy = breakout
params = profile_3
risk_bucket = 0.5x
confidence = medium
trade_filter = enabled
```

This keeps the AI inside a structured control box.

Reference: the `adversarial_ensemble.py` module in our toolkit uses a similar pattern — structured output (probability + tension + confidence) rather than a naked trade decision. The `binary_calibration.py` module shows how to bound and recalibrate AI outputs over time.

---

## Execution and Risk

Keep outside AI control.

### Hard Rules AI Cannot Override

- Max position size
- Max leverage
- Max daily drawdown
- Exchange outage handling
- Stale data handling
- Circuit breakers
- Order throttles
- Exposure concentration limits

### Risk Buckets

AI influences bounded risk only:

| Bucket | Meaning      |
|--------|--------------|
| 0x     | Sit out      |
| 0.25x  | Minimal      |
| 0.5x   | Reduced      |
| 1.0x   | Full size    |

---

## Learning Loop

### Training Cadence

- Daily or weekly feature refresh
- Weekly or biweekly retraining
- Monthly challenger evaluation
- Emergency recalibration only when drift is obvious

### Replacement Criteria

Only replace champion when challenger is better on:

- Risk-adjusted returns (not just PnL)
- Drawdown profile
- Regime robustness
- Turnover-adjusted edge
- Stability across rolling windows

### Evaluation

Every new model must pass:

1. In-sample fit sanity check
2. Purged walk-forward validation
3. Out-of-sample paper evaluation
4. Shadow live comparison vs champion
5. Small-capital deployment

---

## Stack Recommendations

### Solo Builder / Low Budget

| Layer         | Choice                              |
|---------------|-------------------------------------|
| Data          | CCXT + exchange-native APIs         |
| Storage       | Parquet + DuckDB                    |
| Research      | pandas/polars + vectorbt            |
| Modeling      | scikit-learn, XGBoost/LightGBM      |
| Live          | CCXT + custom execution             |
| Tracking      | MLflow + structured logs            |
| Orchestration | cron / Prefect                      |

### Serious Retail Quant

| Layer         | Choice                                              |
|---------------|-----------------------------------------------------|
| Data          | CCXT + exchange-native + selective paid historical   |
| Storage       | Parquet + DuckDB, maybe Postgres/Timescale for live  |
| Research      | vectorbt + custom evaluation                        |
| Modeling      | sklearn + gradient boosting + HMM/change-point       |
| Live          | exchange-native websockets, CCXT fallback            |
| Tracking      | MLflow/W&B, Grafana/Prometheus                      |

### Small Team

| Layer         | Choice                                                  |
|---------------|---------------------------------------------------------|
| Data          | Tardis/CoinAPI + exchange-native live                   |
| Storage       | object storage + Parquet + relational metadata          |
| Research      | custom pipeline, event-driven simulator                 |
| Modeling      | structured offline training, champion/challenger service |
| Live          | exchange-specific adapters, risk gateway, replayable logs|
| Monitoring    | centralized metrics, alerting, experiment registry      |

---

## What NOT to Build First

- Full end-to-end RL trader
- LLM deciding entries/exits
- Many-strategy zoo with hundreds of knobs
- Tick-level market making without exchange-grade infra
- Dozens of altcoins
- Hyperparameter sweeps before trusting the simulator

---

## Phased Roadmap

### Phase 1: Days 1-30

- Define universe (BTC/ETH perps + 1-3 majors)
- Build historical dataset (CCXT + exchange APIs)
- Normalize and store features (Parquet + DuckDB)
- Create baseline regime labels (hand-built thresholds)
- Backtest 3 strategy families across regimes
- Create regime-conditioned performance tables

### Phase 2: Days 30-60

- Build meta-controller (gradient boosted trees)
- Add walk-forward evaluation pipeline
- Add live paper trading with regime logging
- Add feature drift and model drift monitoring
- Test challenger vs static baseline

### Phase 3: Days 60-90

- Deploy small capital
- Compare AI-selected strategy vs fixed-strategy baseline
- Tune only after live telemetry is trustworthy
- Add richer data/context only where bottleneck is proven

---

## Top 10 Decisions to Make

1. Which exchanges and instruments for V1
2. Bar-based only or bar + derivatives features for V1
3. Whether first paid data upgrade is Tardis or CoinAPI
4. Whether regime layer is hand-labeled or model-labeled first
5. Initial regime taxonomy
6. Which three strategy families make the first library
7. Whether meta-controller chooses one strategy or weights several
8. What hard risk rules are permanently outside AI
9. Champion/challenger replacement criteria
10. What live telemetry is mandatory before capital scales

---

## Connection to Existing Toolkit

This blueprint builds on the crack-the-nut toolkit modules:

| MVP Component          | Existing Module                      | Status                                        |
|------------------------|--------------------------------------|-----------------------------------------------|
| Strategy interface     | `strategies/base.py`                 | Ready                                         |
| Backtest runner        | `backtest/engine/runner.py`          | Ready                                         |
| Risk management        | `execution/risk.py`                  | Ready (RiskManager, KellySizer, CorrelationTracker) |
| Confidence scoring     | `scoring/confidence.py`              | Ready (CompositeScorer — usable for regime scoring) |
| LLM analysis           | `agents/llm_analyst.py`              | Ready (ensemble pattern)                      |
| LLM calibration        | `agents/binary_calibration.py`       | Ready (TronBankman PR)                        |
| Adversarial ensemble   | `agents/adversarial_ensemble.py`     | Ready (TronBankman PR)                        |
| Momentum scoring       | `scoring/momentum.py`               | Ready (TronBankman PR)                        |
| Exchange adapters      | `exchanges/`                         | Ready (HL, PM, DEX)                           |
| Scheduling             | `scheduler/runner.py`                | Ready                                         |
| Notifications          | `notify/telegram.py`                 | Ready                                         |
| Regime engine          | `analog/fingerprint.py`, `analog/finder.py` | Ready (analog memory approach — KNN similarity, not discrete labels) |
| Meta-controller        | `analog/meta.py`                     | Ready (strategy selection + parameter profiles + risk allocation) |
| Feature engine         | `analog/fingerprint.py`, `analog/surface.py` | Ready (25 features: price, vol, momentum, funding surface) |
| Walk-forward evaluator | `analog/walkforward.py`              | Ready (purged walk-forward, no lookahead, sit-out accuracy) |
| Champion/challenger    | `analog/champion.py`                 | Ready (arena comparison, composite scoring, replacement threshold) |
