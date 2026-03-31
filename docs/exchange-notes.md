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

## DEXs (Uniswap, etc.)

_(Add notes here)_
