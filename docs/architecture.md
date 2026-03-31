# crack-the-nut Architecture

## Overview

This toolkit provides reusable building blocks for automated trading bots.
Rather than a monolithic framework, it's a collection of composable modules
that share conventions but don't force a specific architecture.

## Module Map

```
┌─────────────────────────────────────────────────────────┐
│                    Your Bot (CLI)                        │
├──────────┬──────────┬───────────┬───────────┬──────────┤
│ Strategy │ Scoring  │  Agents   │  Backtest │  Config  │
│          │          │           │           │          │
│ base.py  │ compos-  │ llm_      │ runner.py │ base.py  │
│ examples/│ ite.py   │ analyst   │           │          │
├──────────┴──────────┴───────────┴───────────┼──────────┤
│              Execution Layer                 │  Notify  │
│                                              │          │
│  risk.py (RiskManager, KellySizer,          │ telegram │
│           CorrelationTracker, GasGuard)      │          │
├──────────────────────────────────────────────┼──────────┤
│           Exchange Adapters                  │Scheduler │
│                                              │          │
│  hyperliquid/ │ polymarket/ │ dex/          │ runner   │
├──────────────────────────────────────────────┼──────────┤
│              Data Layer                      │          │
│                                              │          │
│  database.py (async SQLite)                  │          │
│  models.py (Trade, Portfolio, Alert)         │          │
└──────────────────────────────────────────────┴──────────┘
```

## Data Flow

A typical bot follows this pipeline:

```
Data Sources (WS, REST, on-chain)
    │
    ▼
Strategy.on_data(candle)          ← process new market data
    │
    ▼
Strategy.should_enter() → Signal  ← generate trading signals
    │
    ▼
CompositeScorer.score() → 0-100   ← multi-factor confidence scoring
    │
    ▼
RiskManager.check_entry()         ← position sizing + risk gates
    │
    ▼
ExchangeAdapter.place_order()     ← execute on exchange
    │
    ▼
TelegramNotifier.send_alert()     ← notify
    │
    ▼
Database.upsert()                 ← persist
```

## Key Design Decisions

### Async Everywhere
All I/O is async (aiosqlite, httpx, websockets). The sync Hyperliquid SDK
is wrapped in `asyncio.to_thread()`. This lets a single event loop handle
WebSocket streams, REST polling, and order execution without blocking.

### Strategy as Interface
`Strategy` is an abstract base with 4 methods: `on_data`, `should_enter`,
`should_exit`, `on_fill`/`on_close`. This makes strategies testable in
isolation — the backtest engine calls the same methods as the live runner.

### Risk as a Gate, Not a Layer
`RiskManager` is called explicitly before execution, not wired into the
adapter. This keeps the exchange adapter pure (it just executes orders)
and makes risk checks visible in the calling code.

### Exchange Adapter Pattern
Each exchange adapter implements the same `ExchangeAdapter` interface
(connect, get_balance, place_order, etc.). Strategies don't know which
exchange they're running on — the adapter abstracts the differences.

### Configuration Hierarchy
`BotSettings` (pydantic-settings) loads from `.env` files. Subclass it
to add exchange-specific fields. Risk parameters live in config so they
can be tuned without code changes.

## How to Build a Bot

1. **Subclass `BotSettings`** — add your exchange credentials and strategy params
2. **Implement `Strategy`** — your trading logic in `should_enter` / `should_exit`
3. **Pick an adapter** — HyperliquidAdapter, PolymarketAdapter, DexAdapter, or write your own
4. **Wire it up** — use `SchedulerRunner` for periodic jobs, `TelegramNotifier` for alerts
5. **Backtest first** — `BacktestRunner` uses the same Strategy interface
6. **Deploy** — systemd service on a VPS, paper_trade=True until you trust it

## Directory Convention

```
my-bot/
├── .env                  # Secrets (never committed)
├── .env.example          # Template for others
├── data/
│   └── bot.db            # SQLite (gitignored)
├── logs/                 # (gitignored)
├── src/my_bot/
│   ├── config.py         # Extends BotSettings
│   ├── strategy.py       # Extends Strategy
│   └── cli.py            # Typer CLI: run, status, stats
├── pyproject.toml
└── CLAUDE.md             # Deployment & architecture notes
```
