# crack-the-nut

A shared Python toolkit for building automated trading bots. Extract patterns once, reuse everywhere.

**Goal:** Help us all toward financial independence so we have more resources to do everything else that matters.

## What Is This?

This is a **library of composable modules** — not a framework, not a monolith. You pick the pieces you need and wire them into your own bot. Every module is async Python 3.12, battle-tested from 4 live trading bots.

The toolkit handles the boring infrastructure (config, database, scheduling, notifications, risk management) so you can focus on **strategy**.

## Modules

| Module | Import | What it does |
|--------|--------|-------------|
| **config** | `from config import BotSettings` | Pydantic-settings base class. Loads from `.env`. Subclass to add your own fields. |
| **data** | `from data import Database, Trade` | Async SQLite wrapper with `upsert`, `get_latest`, `get_range`, `insert_batch`. Common trade/signal/summary schemas included. |
| **strategies** | `from strategies.base import Strategy, Signal` | Abstract base class — implement `on_data`, `should_enter`, `should_exit`. Same interface for backtesting and live. |
| **exchanges** | `from exchanges.hyperliquid import HyperliquidAdapter` | Exchange adapters: Hyperliquid (perps), Polymarket (prediction markets), DEX/Web3 (Uniswap V3 style). All implement `ExchangeAdapter` interface. |
| **execution** | `from execution import RiskManager, KellySizer` | Risk gates (position limits, daily loss, kill switch), half-Kelly position sizing, correlation group tracking, gas guards for on-chain bots. |
| **scoring** | `from scoring import CompositeScorer, SubScore` | Register weighted sub-scores, get a 0-100 composite. Used for multi-factor signal generation. |
| **backtest** | `from backtest import BacktestRunner` | Feed candles to a Strategy, get back win rate, PnL, Sharpe ratio, max drawdown, profit factor. |
| **agents** | `from agents import LLMAnalyst` | Ensemble LLM predictions (3 temperatures, take median). Confidence from variance. 2-hour cache. Anthropic or OpenAI. |
| **scheduler** | `from scheduler import SchedulerRunner` | APScheduler wrapper — `add_interval()`, `add_cron()`, graceful shutdown on SIGINT/SIGTERM. |
| **notify** | `from notify import TelegramNotifier` | Telegram alerts with rate limiting (20 msg/min). Formatting helpers for trades, signals, daily reports. |
| **analog** | `from analog import AnalogFinder, MetaController` | Similarity-based strategy selection: fingerprint the market, find historical analogs via KNN, score strategies, pick the best one. |
| **analog.walkforward** | `from analog import WalkForward` | Walk-forward evaluation: step through history, fit on past-only data, measure actual outcomes. No lookahead. |
| **analog.champion** | `from analog import Arena, ChallengerConfig` | Champion/challenger arena: compare configurations via walk-forward, replace only when a challenger beats the champion with margin. |

## Quick Start

```bash
git clone https://github.com/CShear/crack-the-nut.git
cd crack-the-nut
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

## Building a Bot

### 1. Create your config

```python
# my_bot/config.py
from config import BotSettings

class MySettings(BotSettings):
    exchange_api_key: str = ""
    my_custom_threshold: float = 0.05
```

### 2. Write a strategy

```python
# my_bot/strategy.py
from strategies.base import Strategy, Signal, Candle, Direction

class MyStrategy(Strategy):
    async def on_data(self, candle: Candle) -> None:
        pass  # analyze new data

    async def should_enter(self) -> Signal | None:
        return Signal(asset="BTC", direction=Direction.LONG, confidence=0.8, entry_price=65000)

    async def should_exit(self, position) -> bool:
        return False  # your exit logic
```

### 3. Backtest it

```python
from backtest import BacktestRunner
from my_bot.strategy import MyStrategy

runner = BacktestRunner(MyStrategy(), initial_capital=10_000)
result = await runner.run(candles)
print(result.summary)
# {'total_trades': 47, 'win_rate': '57.4%', 'total_pnl': 1230.50,
#  'max_drawdown': '8.3%', 'sharpe_ratio': 1.82, 'profit_factor': 1.65}
```

### 4. Wire up live execution

```python
from exchanges.hyperliquid import HyperliquidAdapter
from execution import RiskManager, KellySizer
from scheduler import SchedulerRunner
from notify import TelegramNotifier

adapter = HyperliquidAdapter(private_key="...", account_address="...")
risk = RiskManager(bankroll=1000.0)
sizer = KellySizer(max_pct=0.05)
notifier = TelegramNotifier(token="...", chat_id="...", prefix="[MY BOT]")

# Schedule your strategy loop, daily reports, etc.
runner = SchedulerRunner(timezone="America/New_York")
runner.add_interval("check_signals", my_strategy_loop, minutes=5)
runner.add_cron("daily_report", send_daily_report, hour=23, minute=59)
await runner.run_forever()
```

### 5. Deploy

```bash
# On a VPS (we use Hetzner, $3-5/mo):
pip install -e .
cp .env.example .env  # fill in your keys
# Create a systemd service, start with paper_trade=True
```

See [docs/architecture.md](docs/architecture.md) for the full design walkthrough and directory conventions.

## Strategy Library (55 strategies, 5 families)

The `analog/` module contains 55 backtestable strategies organized into families:

| Family | File | Count | Description |
|--------|------|:-----:|-------------|
| **Established** | `analog/strategies.py` | 25 | Academic/practitioner strategies: TSMOM, cross-sectional momentum, funding carry, mean reversion, volatility, trend following, pairs, crypto-native |
| **Contrarian** | `analog/contrarian.py` | 9 | Flipped signals (strategies that predict backwards), consensus fade, novel-state trend, disagreement breakout, funding-filtered |
| **Beta-adjusted** | `analog/beta_strategies.py` | 8 | Residual strategies (strip out BTC beta), BTC-led alt trading, beta-relative |
| **Lead-lag** | `analog/lead_lag.py` | 8 | BTC/ETH as leading indicators for alt catch-up, impulse catch-up, reversal fade |
| **Original** | `analog/evaluators.py` | 5 | Funding arb, multi-asset funding, trend follow, mean reversion, breakout |

### Key Findings (2yr walk-forward, 9 assets, Apr 2024–Apr 2026)

**Most robust strategies (positive on the most assets):**
- `rsi_regime_reversion` — 8/8 alts, avg +0.43% per trade
- `residual_breakout` — 7/8 alts, +0.17% avg (trades breakouts relative to BTC, not raw price)
- `mean_reversion` — 7/9 assets, +0.14% avg
- `donchian_breakout` — 7/9 assets

**Best individual combos:**
- ARB:rsi_regime_reversion — 81.2% WR, +0.71% mean, 16 trades
- OP:mean_reversion — 62.3% WR, +0.50% mean, 69 trades
- WIF:residual_breakout — 50.9% WR, +0.68% mean, 110 trades (highest total PnL)
- LINK:btc_impulse_catch_up — 58.3% WR, +1.09% mean

**Key insight:** Alts are where the edge is. Strategies that break even on BTC are profitable on less efficient altcoin markets. Beta-adjusted strategies (trading the residual after removing BTC influence) are more robust than raw price strategies.

### Analysis Commands

```bash
# Run analog analysis on current market
python3 -m analog.run_analysis --days 730

# Walk-forward evaluation (all strategies)
python3 -m analog.run_walkforward --days 730

# Multi-asset strategy comparison (39 strategies x 9 assets)
python3 -m analog.run_multi_asset --days 730 --top 40

# Lead-lag analysis (BTC/ETH → alts)
python3 -m analog.lead_lag --days 730

# Champion/challenger arena
python3 -m analog.run_walkforward --days 730 --arena
```

## Example Strategies (reference implementations)

Three reference strategies in `strategies/examples/`:

- **`funding_arb.py`** — Short when funding rates are extreme positive, long when extreme negative. Collects funding payments.
- **`whale_copy.py`** — Track large wallets, copy their trades when multiple whales converge on the same direction within a time window.
- **`multi_factor_signal.py`** — Combine whale consensus, AI predictions, and price momentum into a single scored signal using `CompositeScorer`.

## Exchange Notes

Hard-won gotchas from production in [docs/exchange-notes.md](docs/exchange-notes.md):

- **Hyperliquid** — sync SDK wrapping, unified accounts, WS trade format, funding mechanics
- **Polymarket** — negRisk detection, FOK pricing, Gamma API bugs, CLOB order placement
- **DEX/LP** — tick math, slippage, nonce management, Algebra vs Uniswap V3 differences
- **Bittensor** — Taostats API, alpha price normalization, emission yield calculation

## Reference Implementations

Sanitized versions of real trading bots built with these patterns:

- [ref-perp-bot](https://github.com/CShear/ref-perp-bot) — Perpetual futures (Hyperliquid). 3 strategies, signal combiner, Telegram alerts.
- [ref-prediction-bot](https://github.com/CShear/ref-prediction-bot) — Prediction markets (Polymarket). Whale tracking + AI ensemble.
- [ref-lp-bot](https://github.com/CShear/ref-lp-bot) — Concentrated liquidity market making. Uniswap V3, Aerodrome, Algebra.
- [ref-subnet-monitor](https://github.com/CShear/ref-subnet-monitor) — Bittensor subnet health dashboard. Scoring, signals, alerts.

## Ground Rules

1. **No secrets in the repo.** API keys, private keys, wallet addresses go in `.env` (gitignored).
2. **Backtest before you deploy.** The backtest engine exists for a reason.
3. **Document your strategies.** A strategy without docs is a strategy nobody else can use.
4. **Share losses too.** We learn more from what didn't work.
5. **Risk management is not optional.** Every strategy needs stop-losses and position limits.

## Stack

- Python 3.12+, async throughout
- `pydantic-settings` — config from `.env`
- `aiosqlite` — async SQLite
- `httpx` + `websockets` — REST/WS
- `structlog` — structured logging
- `APScheduler` — job scheduling
- `python-telegram-bot` — notifications
- `pandas` — data analysis
- Exchange SDKs: `hyperliquid-python-sdk`, `py-clob-client`, `web3` (all optional)
- AI: `anthropic`, `openai` (optional)

## License

MIT — use it, fork it, profit from it.
