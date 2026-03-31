# Exchange Notes & Gotchas

Hard-won knowledge about exchange APIs. Add to this when you discover something non-obvious.

## Hyperliquid

- **SDK is synchronous** — `hyperliquid-python-sdk` uses `requests`, not `httpx`. Wrap calls in `asyncio.to_thread()` for async.
- **Unified account** — Spot USDC serves as perps margin directly, no transfers needed.
- **Trade stream** — `users` field = `[buyer, seller]` addresses. Unique to HL.
- **WS trade side** — "B" = buy, "A" = ask/sell (not "buy"/"sell").
- **Funding** — Hourly settlement, peer-to-peer, capped at 4%/hour.
- **predictedFundings format** — `[[coin, [[venue_name, {fundingRate: ...}], ...]], ...]` — prefer "HlPerp" venue.
- **Trading is gasless.**
- **Min order** — $10 notional.
- **Rate limits** — 1200 weight/min REST, 1000 WS subscriptions.

## Polymarket

- **Gamma API `condition_id` param is BROKEN** — use `slug` param instead.
- **Almost all active markets are negRisk** (463/500 as of Feb 2026).
- **NegRisk order books show 0.001/0.999** — use CLOB APIs for effective prices.
- **FOK orders at midpoint DON'T fill** — must price at effective ask.
- **Price must be rounded to 2 decimals.**
- **py-clob-client auto-detects negRisk** via `client.get_neg_risk(token_id)`.
- **Dead market detection** — filter out markets with prices <10% or >90% to avoid phantom signals.

## Binance

_(Add notes here)_

## DEX / Concentrated Liquidity (Uniswap V3, Aerodrome, Algebra)

### Tick Math
- Uniswap V3 prices are stored as `sqrtPriceX96 = sqrt(price) * 2^96`
- To convert: `price = (sqrtPriceX96 / 2^96)^2 * 10^(decimals0 - decimals1)`
- Ticks are logarithmic: `price = 1.0001^tick`
- tick spacing varies by fee tier: 1 (0.01%), 10 (0.05%), 60 (0.30%), 200 (1.00%)

### Slippage & MEV
- Always set `amountOutMinimum` in production — 0 is fine for testing but will get sandwiched
- Use Flashbots/private mempools on Ethereum mainnet
- On L2s (Base, Arbitrum) MEV is less of a concern but still set slippage

### Nonce Management
- Web3.py default nonce can race if you send multiple txs quickly
- Pattern: fetch nonce once, increment locally per tx in a batch
- If a tx gets stuck, speed it up by resubmitting same nonce with higher gas

### Algebra Integral (Hydrex) Gotchas
- Algebra uses `globalState()` instead of Uniswap's `slot0()` — different ABI
- Single pool per pair (no fee tiers) — simpler but less flexibility
- `limitSqrtPrice` in swaps: use `MIN_SQRT_PRICE=1` when selling token0, `MAX_SQRT_PRICE=2^160-1` when selling token1, or 0 for no limit on some implementations
- Different NPM ABI: `positions()` returns different tuple order vs Uniswap V3
- Gauge staking for LP rewards is separate from the core LP position

### Position Lifecycle (Uniswap V3 style)
1. `approve()` tokens to NonfungiblePositionManager
2. `mint()` → get tokenId (NFT)
3. `collect()` earned fees periodically
4. `decreaseLiquidity()` → withdraw some or all liquidity
5. `collect()` again to get the withdrawn tokens
6. `burn()` the NFT if position is fully empty

### Gas Optimization
- Batch operations via multicall when possible
- `estimateGas()` before sending — revert reasons are in the estimate
- Gas ceiling pattern: skip rebalance cycles when gas > threshold (e.g. 50 gwei)

## Bittensor / TAO Monitoring

### Data Sources
- **Taostats API**: Best for subnet metadata, prices, flows, validator info. Rate-limited.
- **Subtensor RPC**: Direct chain queries. Good for real-time alpha prices and registration costs.
- **CoinGecko**: TAO/USD price. Free tier has aggressive rate limits.
- **Social APIs** (LunarCrush, Santiment): Optional. Spotty data for smaller subnets.

### Collection Patterns
- Collect every 4 hours minimum for meaningful time-series
- Taostats returns per-tempo data (48 tempos per day) — normalize to daily when computing yields
- Alpha prices are in TAO, not USD — always store both and the TAO/USD conversion rate

### Key Metrics
- **Net TAO flow**: Staking inflows minus outflows. Sustained positive = bullish.
- **Emission share**: % of total TAO emissions this subnet receives. Declining = concern.
- **vtrust**: Validator trust score (0-1). Below 0.6 = poor validator consensus.
- **Registration cost**: Rising = growing interest. Falling = subnet losing attention.
- **Stake concentration**: If top validator holds >35% stake, centralization risk.
