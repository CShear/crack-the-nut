# crack-the-nut

A shared trading toolkit for building, testing, and running automated strategies across multiple exchanges and asset types.

**Goal:** Help us all toward financial independence so we have more resources to do everything else that matters.

## How This Works

This is a **mono-repo toolkit** — a shared framework where we all contribute strategies, exchange adapters, and backtesting tools. Everyone runs their own instance with their own keys and risk settings.

### What lives here vs. individual repos

| Here (crack-the-nut) | Individual repos (in the org) |
|---|---|
| Shared strategies anyone can use | Your personal bot, shared as-is |
| Exchange adapters | Experimental/WIP projects |
| Backtesting engine | Specialized tools |
| Execution & risk management | Forks you're hacking on |

The natural flow: share your bot as its own repo → others learn from it → best patterns get extracted into this toolkit → new strategies get built on proven components.

## Repo Structure

```
crack-the-nut/
├── strategies/        # Pluggable strategy modules
│   └── examples/      # Reference implementations (funding arb, whale copy, multi-factor)
├── exchanges/         # Exchange adapters (Hyperliquid, Polymarket, DEX/Web3)
├── execution/         # Risk management — KellySizer, CorrelationTracker, GasGuard
├── data/              # Async SQLite helper, Trade/Portfolio/Alert models
├── config/            # Pydantic-settings base class for .env loading
├── scoring/           # Multi-factor composite confidence scoring
├── scheduler/         # APScheduler async runner with interval/cron jobs
├── notify/            # Telegram bot with rate limiting and formatting
├── backtest/          # Backtesting engine (Sharpe, drawdown, profit factor)
├── agents/            # LLM ensemble analyst (temperature diversity, caching)
└── docs/              # Architecture docs, exchange gotchas
```

## Quick Start

```bash
# Clone
git clone git@github.com:crack-the-nut/crack-the-nut.git
cd crack-the-nut

# Set up Python environment
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

# Copy config template, fill in your keys
cp config/example.env .env

# Run a backtest
python -m backtest.engine --strategy strategies/examples/funding_arb.py --data backtest/datasets/sample.csv

# Run a strategy live (paper mode by default)
python -m execution.runner --strategy strategies/examples/funding_arb.py --paper
```

## Writing a Strategy

Every strategy implements a simple interface:

```python
from strategies.base import Strategy, Signal

class MyStrategy(Strategy):
    """One-line description of what this does."""

    async def on_data(self, candle):
        """Called on every new data point."""
        # Your analysis here
        pass

    async def should_enter(self) -> Signal | None:
        """Return a Signal to enter, or None to skip."""
        pass

    async def should_exit(self, position) -> bool:
        """Return True to close the position."""
        pass
```

See `strategies/examples/` for working references.

## How We Work Together

- **Telegram group** — Real-time discussion, quick questions, sharing wins/losses
- **GitHub Issues** — Track bugs, feature requests, strategy ideas
- **GitHub Discussions** — Longer-form strategy analysis, architecture proposals
- **Weekly roundup** — Each person posts in Telegram: what they ran, PnL, one lesson learned
- **PRs welcome** — Add strategies, fix exchange adapters, improve the backtest engine

## Ground Rules

1. **No API keys or wallet keys in the repo.** Use `.env` files (gitignored).
2. **Backtest before you deploy.** The backtest engine is the arbiter of "does this work?"
3. **Document your strategies.** A strategy without a README is a strategy nobody else can use.
4. **Share losses too.** We learn more from what didn't work.
5. **Risk management is not optional.** Every strategy must have stop-losses and position limits.

## Stack

- **Language:** Python 3.12+
- **Async:** asyncio throughout
- **Exchanges:** httpx + websockets for REST/WS, exchange-specific SDKs where helpful
- **Data:** aiosqlite for local storage, pandas for analysis
- **Logging:** structlog
- **Config:** pydantic-settings, dotenv
- **AI/Agents:** anthropic SDK, langchain, or whatever works — the `agents/` directory is agnostic

## Reference Implementations

These are sanitized versions of real trading bots built with this toolkit's patterns:

- [ref-perp-bot](https://github.com/CShear/ref-perp-bot) — Perpetual futures (Hyperliquid)
- [ref-prediction-bot](https://github.com/CShear/ref-prediction-bot) — Prediction markets (Polymarket)
- [ref-lp-bot](https://github.com/CShear/ref-lp-bot) — Concentrated liquidity market making
- [ref-subnet-monitor](https://github.com/CShear/ref-subnet-monitor) — Bittensor subnet research dashboard

## License

MIT — use it, fork it, profit from it.
