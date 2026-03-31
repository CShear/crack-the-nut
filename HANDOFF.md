# crack-the-nut: Implementation Handoff

This document is a complete brief for an autonomous Claude instance to execute. Read it fully before starting. All file paths are absolute. The GitHub account is `CShear`, authenticated via `gh` CLI.

## Context

Christian has a group of 6 friends called "crack-the-nut" who are collaborating on automated trading. He has 4 existing projects that share nearly identical infrastructure. The goal is:

1. **Extract shared patterns** from his 4 bots into the `crack-the-nut` shared toolkit (already scaffolded at `/home/odinsuncle/crack-the-nut/`)
2. **Create sanitized copies** of each bot as reference repos under his GitHub account, stripped of all personal financial data
3. A GitHub org will be created later — for now repos go under `CShear`

## Ground Rules

- **NEVER commit or push any of these values:**
  - Private keys (hex strings starting with `0x` that are 64+ hex chars)
  - API keys/secrets (Anthropic `sk-ant-*`, Polymarket, Taostats, etc.)
  - Telegram bot tokens (format: `digits:alphanumeric`)
  - Wallet addresses: `0x5975D902038D91B2268bDDe1fd17A4CF0A050C37`, `0x419854410D362937EAbaC3d2ebC48154De76A381`, `0xe4630...BD4`, `0x1189...ce18`
  - Substrate addresses: any starting with `5` followed by 47 alphanumeric chars (e.g., `5Fc4ad...`, `5DeeB...`, `5GTDB...`, `5EvDQ...`, `5DoNW...`)
  - Telegram chat ID: `474167080`
  - Server IPs: `37.27.212.4`, `137.184.182.54`
  - Financial amounts: bankroll values, portfolio values, TAO holdings, commission targets
- **Replace sensitive values with clearly-labeled placeholders** (see sanitization rules below)
- **Do NOT modify the original project directories** — always work from copies
- Public contract addresses (USDC, Uniswap factories, etc.) are fine — they're public on-chain constants

---

## PHASE 1: Enrich the Shared Toolkit

**Working directory:** `/home/odinsuncle/crack-the-nut/`

The toolkit is already scaffolded with a strategy base class, exchange adapter interface, and risk module. Enrich it by extracting the battle-tested patterns from Christian's bots. The goal is a toolkit that anyone in the group can `pip install` and use to build their own strategies.

### 1A. Config Module — `crack-the-nut/config/`

Create `config/base.py` — a reusable pydantic-settings base that all bots can extend.

**Source pattern:** All 4 projects use nearly identical config. Best reference:
- `/home/odinsuncle/hyperliquid-bot/hlbot/config.py` (cleanest)
- `/home/odinsuncle/prediction-market-bot/src/pmbot/config.py`

**What to extract:**
- Pydantic `BaseSettings` subclass with `.env` loading
- Common fields: `bankroll`, `paper_trade`, `telegram_bot_token`, `telegram_chat_id`, `log_level`, `json_logs`
- Risk fields: `max_position_size_pct`, `max_total_exposure_pct`, `max_positions`, `max_daily_loss_pct`, `kill_switch_pct`
- Show how to extend it for exchange-specific settings

### 1B. Data Module — `crack-the-nut/data/`

Create `data/database.py` — async SQLite helper with common table patterns.

**Source pattern:**
- `/home/odinsuncle/hyperliquid-bot/hlbot/data/db.py`
- `/home/odinsuncle/prediction-market-bot/src/pmbot/data/database.py`
- `/home/odinsuncle/tao-monitor/src/taomonitor/data/db.py`

**What to extract:**
- Async context manager for aiosqlite connections
- Common schema patterns: trades table, signals table, daily_summary table
- Helper functions: `upsert`, `get_latest`, `get_range(start, end)`
- Migration pattern: safe `ALTER TABLE ADD COLUMN` with try/except

Create `data/models.py` — shared dataclasses used across the toolkit. Pull from the existing `strategies/base.py` (Signal, Position, Candle are already there) and add:
- `Trade` (entry_price, exit_price, pnl, fees, funding_pnl, paper_trade flag)
- `PortfolioSnapshot` (equity, margin_used, unrealized_pnl, timestamp)
- `Alert` (severity, message, rule_name, cooldown)

### 1C. Scheduler Module — `crack-the-nut/scheduler/`

Create `scheduler/runner.py` — APScheduler wiring template.

**Source pattern:**
- `/home/odinsuncle/hyperliquid-bot/hlbot/scheduler/scheduler.py`
- `/home/odinsuncle/prediction-market-bot/src/pmbot/scheduler/runner.py`

**What to extract:**
- AsyncIOScheduler setup with both IntervalTrigger and CronTrigger
- Job registration pattern (add_job with misfire_grace_time)
- Graceful shutdown (scheduler.shutdown on signal)
- Example job wiring showing how strategies hook into the scheduler

### 1D. Telegram Module — `crack-the-nut/notify/`

Create `notify/telegram.py` — notification bot.

**Source pattern:**
- `/home/odinsuncle/hyperliquid-bot/hlbot/telegram/bot.py`
- `/home/odinsuncle/prediction-market-bot/src/pmbot/telegram/bot.py`

**What to extract:**
- Async Telegram bot wrapper (python-telegram-bot)
- `send_alert(message, parse_mode="Markdown")` method
- Rate limiting (Telegram has 30 msg/sec limit)
- Message formatting helpers for trades, signals, daily reports
- Error handling (bot token invalid, chat not found, etc.)

### 1E. Enrich Risk Module — `crack-the-nut/execution/risk.py`

The existing `risk.py` has the basics. Enrich it with patterns from:
- `/home/odinsuncle/hyperliquid-bot/hlbot/strategies/combiner.py` — correlation group exposure caps
- `/home/odinsuncle/lp-bot/src/lpbot/risk/__init__.py` — gas ceiling, pool staleness checks
- `/home/odinsuncle/prediction-market-bot/src/pmbot/signals/kelly.py` — half-Kelly position sizing

**Add:**
- `KellySizer` class — half-Kelly formula from the PM bot
- Correlation group tracking from the HL bot (max 15% per group)
- `GasGuard` — pause if gas exceeds threshold (for on-chain bots)

### 1F. Scoring Module — `crack-the-nut/scoring/`

Create `scoring/confidence.py` — multi-factor confidence scoring.

**Source pattern:**
- `/home/odinsuncle/prediction-market-bot/src/pmbot/signals/scorer.py` — 0-100 composite scoring
- `/home/odinsuncle/tao-monitor/src/taomonitor/scoring/health.py` — weighted sub-score pattern
- `/home/odinsuncle/tao-monitor/src/taomonitor/scoring/signals.py` — -100 to +100 signal scoring

**What to extract:**
- `CompositeScorer` class — register sub-scores with weights, compute weighted average
- Sub-score interface: name, weight, `score(data) -> float`
- Useful for any multi-factor signal generation

### 1G. Exchange Adapters — `crack-the-nut/exchanges/`

Create minimal but functional adapters. These are the most exchange-specific code but the interface is already defined in `exchanges/base.py`.

**Hyperliquid adapter** — `exchanges/hyperliquid/adapter.py`:
- Source: `/home/odinsuncle/hyperliquid-bot/hlbot/execution/executor.py`
- Key pattern: wrapping sync SDK in `asyncio.to_thread()`
- Include funding rate query, position query, market order placement
- Strip all hardcoded addresses

**DEX/Web3 adapter** — `exchanges/dex/adapter.py`:
- Source: `/home/odinsuncle/lp-bot/src/lpbot/pool/__init__.py` and `swap/__init__.py`
- Key pattern: web3.py transaction building (estimate gas → sign → broadcast → receipt)
- Generic swap and position management
- Strip all hardcoded addresses, use config for contract addresses

**Polymarket adapter** — `exchanges/polymarket/adapter.py`:
- Source: `/home/odinsuncle/prediction-market-bot/src/pmbot/execution/executor.py` and `data/polymarket.py`
- Key pattern: py-clob-client wrapper, FOK order placement, negRisk detection
- Strip all hardcoded addresses

### 1H. Backtest Engine — `crack-the-nut/backtest/engine/`

Create `backtest/engine/runner.py` — a backtesting framework.

**Source pattern:**
- `/home/odinsuncle/tao-monitor/src/taomonitor/scoring/outcomes.py` — horizon-based accuracy tracking
- The PM bot's 1700-signal backtest methodology (documented in memory, implement the pattern)

**What to build:**
- `BacktestRunner` class that takes a Strategy and historical data (CSV or DataFrame)
- Iterates candles, calls `on_data()`, `should_enter()`, `should_exit()`
- Tracks paper positions with simulated fills
- Computes: win rate, total PnL, max drawdown, Sharpe ratio, profit factor
- Output: summary dict + trade-by-trade DataFrame

### 1I. Agents Module — `crack-the-nut/agents/`

Create `agents/llm_analyst.py` — LLM-powered analysis wrapper.

**Source pattern:**
- `/home/odinsuncle/prediction-market-bot/src/pmbot/ai/engine.py` — Claude ensemble (3 calls at different temperatures, take median)
- `/home/odinsuncle/prediction-market-bot/src/pmbot/ai/prompts.py` — superforecaster system prompts

**What to extract:**
- `LLMAnalyst` class — wraps anthropic SDK, runs ensemble predictions
- Temperature diversity pattern (0.3, 0.5, 0.7 → median)
- Confidence from variance (low variance = high confidence)
- Cache layer (2-hour TTL) to control API costs
- Make it model-agnostic (anthropic or openai SDK)

### 1J. Docs — `crack-the-nut/docs/`

`docs/exchange-notes.md` already has HL and PM gotchas. Add:
- DEX/LP gotchas from the LP bot (Uniswap V3 tick math, slippage, nonce management)
- Research/monitoring patterns from TAO monitor
- A `docs/architecture.md` explaining the overall toolkit design

### 1K. Example Strategies

Add 2 more example strategies beyond the existing `funding_arb.py`:

**`strategies/examples/whale_copy.py`** — whale copy-trading pattern
- Source: `/home/odinsuncle/hyperliquid-bot/hlbot/strategies/whale_tracker.py`
- Generalize: track large wallets on any exchange, copy their trades with delay and smaller size

**`strategies/examples/multi_factor_signal.py`** — multi-factor signal generation
- Source: PM bot's combiner pattern
- Shows how to combine multiple data sources (whale consensus, AI prediction, price momentum) into a single trading signal

### After Phase 1

- Run `ruff check . && ruff format .` on the entire toolkit
- `pip install -e ".[dev]"` should work
- Commit and push to `CShear/crack-the-nut`
- Keep commits granular (one per module, not one giant commit)

---

## PHASE 2: Create Sanitized Reference Repos

For each of the 4 projects, create a sanitized copy as a new GitHub repo.

### Sanitization Rules (apply to ALL repos)

1. **Copy to a temp directory first** — never modify originals
   ```bash
   cp -r /home/odinsuncle/<project> /tmp/<ref-name>
   cd /tmp/<ref-name>
   ```

2. **Delete sensitive files:**
   - `.env` (keep `.env.example`)
   - `data/*.db`, `data/*.sqlite`
   - `logs/*`
   - Any `*.log` files
   - `.git/` (start fresh)

3. **Create/update `.env.example`** with placeholder values:
   ```
   # Private keys
   PRIVATE_KEY=0x_YOUR_PRIVATE_KEY_HERE
   HL_PRIVATE_KEY=0x_YOUR_PRIVATE_KEY_HERE
   
   # Wallet addresses
   HL_ACCOUNT_ADDRESS=0x_YOUR_WALLET_ADDRESS
   HL_API_WALLET_ADDRESS=0x_YOUR_API_WALLET_ADDRESS
   
   # API keys
   ANTHROPIC_API_KEY=sk-ant-YOUR_KEY_HERE
   POLYMARKET_API_KEY=YOUR_KEY_HERE
   TAOSTATS_API_KEY=YOUR_KEY_HERE
   
   # Telegram
   TELEGRAM_BOT_TOKEN=YOUR_BOT_TOKEN
   TELEGRAM_CHAT_ID=YOUR_CHAT_ID
   
   # Financial
   BANKROLL=1000.00
   ```

4. **Scrub `config.py` in every project** — replace hardcoded defaults:
   - Any `0x5975D902...` → `"0x_YOUR_WALLET_ADDRESS"`
   - Any `0x419854...` → `"0x_YOUR_API_WALLET_ADDRESS"`
   - Any `474167080` → `0` (chat ID)
   - Any bankroll default → `1000.0`
   - Substrate addresses (5Fc4a..., 5DeeB..., 5GTDB..., 5EvDQ..., 5DoNW...) → `"5_YOUR_SUBSTRATE_ADDRESS"`

5. **Scrub CLAUDE.md** — rewrite as a generic deployment guide:
   - Replace `37.27.212.4` → `YOUR_SERVER_IP`
   - Replace `137.184.182.54` → `YOUR_SERVER_IP`
   - Replace `/opt/<service>` → keep as suggested deploy path but note it's configurable
   - Remove SSH commands with real IPs
   - Remove wallet addresses
   - Remove GitHub URLs to private repos (or replace with the new public ref repo URL)
   - Keep the architecture description, stack info, and operational patterns

6. **Scrub any hardcoded financial data:**
   - `seed_portfolio.py` (TAO monitor): Replace all TAO amounts and USD values with synthetic data. Keep the structure and date range but use fake numbers (e.g., 100 TAO @ $300).
   - `kvcm_strategy.py` (LP bot): Replace commission targets (`TOTAL_CFC`, `TOTAL_KVCM_FROM_CFCS`, `TARGET_AVG_PER_CFC`) with placeholder values and a comment saying "set your own targets"
   - Any hardcoded bankroll or position size dollar amounts in strategy files

7. **Verify with grep** before committing:
   ```bash
   # Run these checks — ALL must return empty:
   grep -ri "0x5975D902" . --include="*.py" --include="*.md" --include="*.env*"
   grep -ri "0x419854" . --include="*.py" --include="*.md" --include="*.env*"
   grep -ri "0xc03150d45e" . --include="*.py" --include="*.md" --include="*.env*"
   grep -ri "0x82c71d953a" . --include="*.py" --include="*.md" --include="*.env*"
   grep -ri "8509870479:" . --include="*.py" --include="*.md" --include="*.env*"
   grep -ri "474167080" . --include="*.py" --include="*.md" --include="*.env*"
   grep -ri "37\.27\.212\.4" . --include="*.py" --include="*.md" --include="*.env*"
   grep -ri "137\.184\.182\.54" . --include="*.py" --include="*.md" --include="*.env*"
   grep -ri "5Fc4adwLNR" . --include="*.py" --include="*.md" --include="*.env*"
   grep -ri "5DeeBKvdR7" . --include="*.py" --include="*.md" --include="*.env*"
   grep -ri "5GTDB7htKY" . --include="*.py" --include="*.md" --include="*.env*"
   grep -ri "5EvDQkwgFa" . --include="*.py" --include="*.md" --include="*.env*"
   grep -ri "5DoNWyc2aU" . --include="*.py" --include="*.md" --include="*.env*"
   grep -ri "sk-ant-" . --include="*.py" --include="*.md" --include="*.env*"
   grep -ri "0xe4630" . --include="*.py" --include="*.md" --include="*.env*"
   grep -ri "tao-08997902" . --include="*.py" --include="*.md" --include="*.env*"
   # Also check for any private key patterns (64-char hex after 0x):
   grep -rP "0x[0-9a-fA-F]{64}" . --include="*.py" --include="*.md" --include="*.env*" | grep -v "node_modules"
   ```

### 2A. ref-perp-bot (from Hyperliquid bot)

**Source:** `/home/odinsuncle/hyperliquid-bot/`
**New repo:** `CShear/ref-perp-bot`
**Description:** "Reference implementation — perpetual futures trading bot (Hyperliquid). Multi-strategy: whale tracking, funding rate arbitrage, liquidation cascade riding."

**Extra sanitization:**
- `hlbot/config.py` lines 15-16: hardcoded wallet addresses → placeholders
- `hlbot/config.py` line 34: hardcoded chat ID → 0

**README should explain:**
- Architecture: WS streams → 3 strategies → SignalCombiner → Executor → Telegram
- Each strategy's thesis (whale copy, funding harvest, liquidation cascade)
- How the signal combiner resolves conflicts and sizes positions
- The async-wrapping pattern for the sync SDK
- Key lessons from `docs/exchange-notes.md` (HL section)

### 2B. ref-prediction-bot (from Polymarket bot)

**Source:** `/home/odinsuncle/prediction-market-bot/`
**New repo:** `CShear/ref-prediction-bot`
**Description:** "Reference implementation — prediction market trading bot (Polymarket). Hybrid whale tracking + AI probability estimation."

**Extra sanitization:**
- Check `src/pmbot/data/polymarket.py` for any hardcoded proxy wallet addresses
- Check `src/pmbot/execution/executor.py` for signature/wallet references

**README should explain:**
- Architecture: Whale consensus + AI ensemble → Signal combiner → Executor
- The AI ensemble pattern (3 temperatures, median, confidence from variance)
- Whale tracking methodology (leaderboard → wallet polling → consensus detection)
- Backtesting results methodology (not actual numbers — describe the approach)
- Key CLOB/negRisk gotchas

### 2C. ref-lp-bot (from LP bot)

**Source:** `/home/odinsuncle/lp-bot/`
**New repo:** `CShear/ref-lp-bot`
**Description:** "Reference implementation — concentrated liquidity market making bot. Uniswap V3, Aerodrome, Algebra DEXs on Base/Arbitrum/Polygon."

**Extra sanitization:**
- `src/lpbot/kvcm_strategy.py` lines 57-99: commission constants → generic placeholders
- `src/lpbot/chains/__init__.py`: wallet addresses if any (contract addresses are fine)
- The entire `kvcm_buyer/` directory is very specific to Christian's kVCM strategy — include it but strip financial targets

**README should explain:**
- Architecture: Scanner → Scorer → Position Manager → Rebalance Loop
- Concentrated liquidity concepts (tick math, range selection, impermanent loss)
- The rebalance loop (detect out-of-range → remove → swap → re-mint)
- Multi-DEX support (Uniswap V3, Aerodrome SlipStream, Algebra)
- Gas management and risk controls

### 2D. ref-subnet-monitor (from TAO monitor)

**Source:** `/home/odinsuncle/tao-monitor/`
**New repo:** `CShear/ref-subnet-monitor`
**Description:** "Reference implementation — Bittensor subnet health monitoring dashboard. Scoring, signals, alerts, weekly reports."

**Extra sanitization:**
- `src/taomonitor/data/seed_portfolio.py`: Replace ALL data with synthetic values (use 100 TAO @ $300 as baseline, generate reasonable fake history)
- `src/taomonitor/config.py`: Remove any hardcoded wallet addresses or chat IDs
- `.env.example`: Include `TRACKED_WALLETS` field but with placeholder addresses
- `src/taomonitor/collectors/wallets.py`: Keep the collector pattern but ensure no hardcoded addresses

**README should explain:**
- Architecture: Collectors → Time-series DB → Scoring → Signals → Alerts → Dashboard
- The health scoring model (6 weighted sub-scores)
- Investment signal generation (-100 to +100)
- Outcome backtesting (7d/14d/30d horizon tracking)
- Alert rule engine with cooldowns
- FastAPI + Jinja2 + HTMX dashboard pattern
- How to add new data sources (extend BaseCollector)

### After Phase 2

For each repo:
1. `git init && git checkout -b main`
2. Add all files, commit with message: `"Initial release — sanitized reference implementation"`
3. `gh repo create CShear/<name> --public --source=. --push --description "<description>"`
4. Run the full grep verification suite (from sanitization rules step 7) one final time on each repo AFTER pushing — if anything leaks, force-push a fix immediately

### Final Commit to crack-the-nut

After both phases, update `crack-the-nut/README.md` to link to the reference repos:

```markdown
## Reference Implementations

These are sanitized versions of real trading bots built with this toolkit's patterns:

- [ref-perp-bot](https://github.com/CShear/ref-perp-bot) — Perpetual futures (Hyperliquid)
- [ref-prediction-bot](https://github.com/CShear/ref-prediction-bot) — Prediction markets (Polymarket)
- [ref-lp-bot](https://github.com/CShear/ref-lp-bot) — Concentrated liquidity market making
- [ref-subnet-monitor](https://github.com/CShear/ref-subnet-monitor) — Bittensor subnet research dashboard
```

---

## Order of Operations

1. Phase 1 first (toolkit enrichment) — this is independent work
2. Phase 2 second (sanitized repos) — depends on understanding the code from Phase 1
3. Update crack-the-nut README with links last
4. Commit everything, push everything
5. Run verification greps on ALL repos one final time

## How to verify success

- `cd /home/odinsuncle/crack-the-nut && pip install -e ".[dev]" && ruff check .` passes
- All 4 ref repos exist on GitHub under CShear
- The full grep suite (sanitization step 7) returns empty on every repo
- Each ref repo has a clear README explaining architecture and how to run it
- No `.env` files committed (only `.env.example`)
- No database files committed
- No log files committed
