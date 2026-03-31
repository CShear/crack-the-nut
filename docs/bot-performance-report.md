# Trading Bot Performance Report
**Author:** Christian Shearer | **Date:** March 31, 2026

Three bots built, deployed, and iterated on over ~7 weeks. Here's what happened with each one, what we learned, and where they could go next.

---

## 1. Polymarket Bot (ref-prediction-bot)

**What it does:** Tracks the top ~225 Polymarket traders (whales) by PnL, monitors their trades in real time, and when 7+ whales converge on the same market with 60%+ agreement, runs a Claude AI ensemble to estimate a fair probability. If the AI disagrees with the market price by more than 5%, the bot generates a signal and executes a trade.

**Timeline:**
- Feb 5: Research phase complete. Found that pure arbitrage (<1% of markets) isn't viable after fees (~2.5% round-trip). Pivoted to whale+AI hybrid.
- Feb 9: Paper trading started
- Feb 13: Live with $100 bankroll
- Feb 26: Major threshold tightening after a 1,700-signal backtest

### Results

| Metric | Paper Trading | Live Trading |
|--------|:------------:|:------------:|
| Trades | 450 | 58 |
| Win Rate | 56.2% | 37.9% |
| Total PnL | +$34,148 | -$13.40 |
| Avg PnL/Trade | +$75.88 | -$0.23 |

**The gap between paper and live is the story.** Paper trading was wildly profitable — 56% win rate, +$34K across 450 resolved trades. But live trading lost money on 58 trades.

### Why the gap?

1. **Execution reality:** Paper trades fill at midpoint; live FOK orders fill at the ask. On thin Polymarket books, that spread eats 2-5% of edge immediately.
2. **Position sizing:** Paper used $5K reference bankroll (positions of $50-250). Live used $100 bankroll (positions of $1-5). At $2-5 per trade, a single loss wipes multiple wins.
3. **Fee drag:** Polymarket charges ~1% taker + 2% winner fee. On small positions, this is devastating.
4. **Signal quality:** 1,774 signals generated but only 13 formally approved (0.7%). The filter was too loose early on — many signals had marginal edge that fees consumed.

### What we learned

- **The AI ensemble pattern works.** 3 Claude calls at temperatures 0.3/0.5/0.7, take the median, derive confidence from variance. Low variance = high conviction. This is a genuinely useful pattern for any probabilistic assessment.
- **Whale tracking has real alpha.** The 56% paper win rate on 450 trades is statistically significant. The signal is there.
- **Execution is everything.** A strategy that's +54% on paper can be -37% live if execution costs aren't modeled accurately.
- **The Feb 26 backtest was the most valuable output.** We discovered 3 bugs: outcome prices weren't binary (0.9995 instead of 1.0), phantom longshot wins at dead-market prices, and a config bug where `MIN_CONFIDENCE_SCORE` env var never worked (field name mismatch). Corrected win rate: 54% (was showing 34% before fixes). Best strategy: Edge>=5% + Confidence>=50 + Whales>=7 + Price 10-90% = 347 trades, 51.9% WR, +27.7% ROI.
- **Entry price sweet spot is 20-35%.** That range had +53% ROI. Above 65% loses money.

### Ideas for next iteration

- **Model execution costs in signal scoring.** Don't just check edge vs market price — check edge vs effective fill price (ask + fees). Kill any signal where post-cost edge < 2%.
- **Scale up bankroll.** At $100, position sizes are too small to overcome fixed costs. Need $500-1K minimum for positions to be meaningful.
- **Add the understand-and-improve loop.** The backtest infrastructure exists — have the bot periodically re-evaluate its own thresholds against recent resolved trades and auto-tune.
- **Focus on non-sports markets.** Many live trades were sports events with fast resolution. Longer-duration political/economic markets may have more stable edges.

---

## 2. Hyperliquid Perps Bot (ref-perp-bot)

**What it does:** Runs 3 parallel strategies on Hyperliquid perpetual futures:
1. **Whale Tracker** — watches the trade stream for $50K+ trades, signals when multiple whales converge
2. **Funding Sniper** — shorts when funding rates are extreme positive (longs overpaying), longs when extreme negative
3. **Liquidation Rider** — detects cascading liquidations and rides the momentum

A signal combiner resolves conflicts, applies Kelly-inspired sizing, and caps correlated exposure at 15% per group.

**Timeline:**
- Feb 14: Launched with $599 bankroll (3 strategies enabled)
- Feb 14-25: 10 commits fixing critical bugs (position accumulation, equity double-counting, SL/TP triggers)
- Feb 25: Data-driven tuning of funding sniper, added momentum paper trading
- Mar 12: SL/TP execution fix (use fill prices, not signal prices)

### Results

| Metric | Live | Paper |
|--------|:----:|:-----:|
| Trades | 11 | 22 |
| Win Rate | 54.5% | 9.1% |
| Total PnL | +$7.06 | -$2.80 |
| Strategy | Funding Sniper only | Mixed |

**Only the Funding Sniper ever traded live.** 2,327 of 2,328 signals came from funding sniper. Whale Tracker generated 0 signals. Liquidation Rider generated 1 signal but never acted on it.

### What happened

- The bot filled its 10-position cap almost immediately and never closed any. All 11 live trades are still open — the SL/TP logic had bugs that prevented exits.
- Funding PnL was positive (+$1.39), validating the core thesis that harvesting extreme funding rates works.
- Portfolio equity dropped from $652 to $643 (-1.4%) over 15 hours, partly from a BTC/ETH/SOL liquidation cascade on Feb 14 ($5M+ in liquidations detected).
- Paper trading was terrible (9.1% WR) — confirming that the non-funding strategies weren't ready for live.

### What we learned

- **Funding rate harvesting is a real edge**, even at small scale. The bot earned $1.39 in funding on $599 bankroll in 15 hours. That extrapolates to ~$80/month, though real-world compounding is less clean.
- **Position exits are harder than entries.** The bot was good at finding entry signals but couldn't close trades. Multiple bugs in SL/TP trigger logic, fill price usage, and order types needed fixing.
- **The sync SDK wrapping pattern works.** Hyperliquid's Python SDK is synchronous, but `asyncio.to_thread()` wrapping performed well in production with no blocking issues.
- **Whale tracking on Hyperliquid is different from Polymarket.** On Polymarket, whale wallets are public via leaderboard. On Hyperliquid, you only see real-time trade flow — you need to discover whales by volume heuristics (>$500K volume + 5 trades). The bot never built a whale list because the discovery threshold was too high for the assets being tracked.
- **Bug density was high.** 10 commits in 11 days fixing critical issues (double-counted equity, uncontrolled position accumulation, wrong trigger prices, HTML escaping in Telegram messages). This is normal for a new bot but meant the first 2 weeks of data are partially corrupted.

### Ideas for next iteration

- **Fix position management first.** Before adding any new features, the bot needs to reliably close positions via SL/TP. This is table stakes.
- **Lower whale discovery threshold.** $500K is too high for altcoins — try $100K or even $50K. Alternatively, seed the whale list manually from known profitable Hyperliquid traders.
- **Pure funding farm mode.** Strip out whale tracking and liquidation riding for now. Run a focused funding-only strategy that opens/closes positions based purely on rate extremes. Simpler = fewer bugs.
- **Add dead man's switch monitoring.** The 2400s timeout exists but isn't monitored externally. Add a Telegram heartbeat so you know if the bot is down.

---

## 3. LP Bot (ref-lp-bot)

**What it does:** Scans DEXs for high-volume pools, deploys concentrated liquidity positions in tight price ranges, and rebalances every 10 minutes to stay in range and capture trading fees. Supports Uniswap V3, Aerodrome, and Algebra Integral (Hydrex).

**Timeline:**
- Mar 7-8: Scanner + paper trading validation (5 pools x $500)
- Mar 12: Go-live on Base cbBTC/USDC (Uniswap V3, 0.05% fee tier) with $1,146 portfolio
- Mar 12-14: Lost $49 in 45 hours. Root cause: micro-fees at 0.05% tier + rebalancing overhead
- Mar 14: Pivoted to Hydrex kVCM/USDC strategy (Algebra Integral, 164% stated APR from HYDX emissions)
- Mar 16: Algebra Integral support complete, gauge staking added
- Mar 24: kVCM price support blitz — $16.9K spent, moved price $0.058 to $0.076 (+30%) in under 3 minutes

### Results

| Phase | Duration | Fees Earned | Gas Spent | IL | Net PnL |
|-------|----------|:-----------:|:---------:|:--:|:-------:|
| Paper (5 pools) | ~2 hours | $57.25 | $3.65 | $5.37 | +$54.13 |
| Live cbBTC/USDC | 45 hours | minimal | significant | yes | -$49.00 |
| kVCM Blitz | 2 min 50s | n/a | ~$50 | n/a | 258K kVCM acquired |

**Time 0 baseline (Mar 12):** $1,146.23 total ($967.92 in LP position, $77.35 wallet USDC, $100.97 ETH for gas). BTC at $70,290.

### What happened

**Paper trading looked incredible.** $57 in fees on $2,500 deployed capital in ~2 hours with only $3.65 gas and $5.37 IL. Extrapolated annualized return was absurd (117%+ APR).

**Live was a different story.** The 0.05% fee tier on cbBTC/USDC generates micro-fees per swap. With 10-minute rebalancing, swap fees and gas costs from rebalancing exceeded fee income. Lost $49 in 45 hours.

**The pivot to Hydrex/kVCM was the key insight.** Pure trading fees at $500-1K scale on major pairs are insufficient. You need emission rewards on top. Hydrex offered 164% stated APR on kVCM/USDC from HYDX token emissions, which completely changes the economics.

**The blitz tool became the most impactful output.** Built a rapid-fire multi-pool buy executor that alternates between Hydrex CLMM and Aerodrome V2 to drain both order books simultaneously. Moved kVCM price 30% in under 3 minutes with $16.9K, outrunning 5 active arb bots. Price held post-execution.

### What we learned

- **Concentrated liquidity at small scale needs emission rewards.** On $500-1K positions, trading fees alone (especially on 0.05% tier pools) don't cover rebalancing costs. You need pools with farming rewards (HYDX, AERO, etc.) to be profitable.
- **Paper trading overstated returns by ~10x.** Paper assumed instant fills at market price with no slippage. Real swaps on thin books had 0.5-2% slippage per rebalance, which compounds quickly at 10-minute intervals.
- **Algebra Integral (Hydrex) requires different ABIs everywhere.** `globalState()` vs `slot0()`, 8-param swap router vs 7-param, deployer field in mint, different position tuple ordering. Every integration point was slightly different from Uniswap V3.
- **On-chain NAV is the only source of truth.** Early versions tracked fees cumulatively, which overstated returns by including wallet buffer USDC. The fix was reading on-chain balances directly (position value + wallet tokens + ETH).
- **9 critical bugs in the first 4 days of live trading.** Mint amount math (475 BTC instead of $500 worth), nonce collisions on 2-second blocks, gas estimation failures, orphaned positions from failed deploys. $406 was stuck in orphaned NFTs until we built a recovery script.

### Ideas for next iteration

- **Target emission-rich pools only.** Don't bother with pure fee-tier pools at small scale. Filter for pools with active farming programs (gauges, Merkl, etc.) and factor emission APR into the pool scorer.
- **Model rebalancing costs in the scoring.** Current scanner scores pools by volume and spread but doesn't factor in gas cost per rebalance. On Base ($0.01-0.10 per tx), this is fine, but on mainnet it would be fatal.
- **Build a proper IL model.** Current IL tracking is retrospective. A forward-looking model that predicts IL from volatility and adjusts range width accordingly would reduce unnecessary rebalances.
- **Auto-compound rewards.** The Hydrex strategy includes oHYDX harvesting code but it's not yet integrated into the rebalance loop. Harvesting rewards and compounding them into the LP position would meaningfully increase returns.

---

## Cross-Cutting Themes

### 1. Paper trading always lies
All three bots showed dramatically better paper results than live. The gap comes from execution costs (slippage, fees, gas), which paper trading ignores. **Any backtesting or paper trading system must model execution costs to be useful.**

### 2. Bug density in the first 2 weeks is extreme
Every bot had 5-10 critical bugs discovered in the first week of live trading. This is normal — you can't unit test every exchange API edge case. Budget for a "burn-in" period where the bot runs live with minimum capital and you fix bugs daily.

### 3. Simple strategies beat complex ones at small scale
The funding sniper (simple: extreme rate → take opposite side) was the only consistently profitable strategy across all three bots. Whale tracking, liquidation riding, and multi-factor AI signals all showed promise in backtesting but failed in live execution at $100-600 bankroll scale. Complexity has a cost.

### 4. The toolkit extracts real value
Despite mixed P&L results, the infrastructure built across these bots is battle-tested and reusable: async database patterns, Telegram notifications, risk management, config management, scheduler wiring, exchange adapters. That's what's now in the crack-the-nut toolkit.

### 5. The missing piece: AI understand-and-improve loops
All three bots are purely programmatic — they execute fixed rules. None of them learn from their own performance. The next evolution is building in an automated feedback loop where the bot:
1. Evaluates its recent trade outcomes
2. Identifies which signals/thresholds are working vs not
3. Adjusts its own parameters (within guardrails)
4. Logs what it changed and why

The backtest infrastructure and outcome tracking already exist (especially in the Polymarket bot's signal outcome evaluator). The gap is closing the loop — having the bot act on its own analysis rather than waiting for a human to tune thresholds.

---

## Summary Table

| Bot | Bankroll | Live Trades | Win Rate | Net PnL | Status |
|-----|:--------:|:-----------:|:--------:|:-------:|--------|
| Polymarket | $100 | 58 | 37.9% | -$13.40 | Paused — needs execution cost modeling |
| Hyperliquid | $599 | 11 | 54.5% | +$7.06 | Running — needs exit logic fixes |
| LP Bot | $1,146 | ~45h live | n/a | -$49.00 | Pivoted to kVCM/Hydrex strategy |

**Total capital deployed:** ~$1,845
**Total live PnL:** ~-$55
**Total lessons learned:** Priceless (and now shared in the toolkit)
