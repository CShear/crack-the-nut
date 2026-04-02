"""Backfill historical data and compute fingerprints.

Pulls 4h OHLCV candles and hourly funding rates from Hyperliquid, then
computes fingerprints and stores them in Parquet.

Usage::

    python3 -m analog.backfill

    # Or from code:
    import asyncio
    from analog.backfill import run_backfill
    asyncio.run(run_backfill())
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx
import structlog

from analog.fingerprint import FingerprintEngine, Fingerprint
from analog.surface import FundingSurfaceEngine
from analog.store import FingerprintStore

logger = structlog.get_logger()

# --- Constants ---
HYPERLIQUID_BASE = "https://api.hyperliquid.xyz"

CANDLE_ASSETS = ["BTC", "ETH"]
FUNDING_ASSETS = ["BTC", "ETH", "SOL", "DOGE", "ARB", "OP", "AVAX", "LINK", "WIF", "PEPE"]
INTERVAL = "4h"
LOOKBACK_DAYS = 730  # ~2 years

# HL rate limit: 1200 weight/min. Be conservative.
HL_REQUEST_DELAY = 1.0  # seconds between requests
HL_RETRY_DELAY = 5.0  # seconds on 429
HL_MAX_RETRIES = 3


@dataclass
class CandleData:
    """Raw OHLCV candle."""
    timestamp_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class FundingSnapshot:
    """Funding rate for one asset at one time."""
    asset: str
    rate: float
    timestamp_ms: int


@dataclass
class BackfillResult:
    """Summary of backfill operation."""
    candles: dict[str, list[CandleData]] = field(default_factory=dict)
    funding: dict[str, list[FundingSnapshot]] = field(default_factory=dict)
    fingerprints: list[Fingerprint] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


# --- Hyperliquid API helpers ---

async def _hl_post(
    client: httpx.AsyncClient,
    payload: dict,
    retries: int = HL_MAX_RETRIES,
) -> list | dict:
    """POST to Hyperliquid info endpoint with retry on 429."""
    for attempt in range(retries):
        try:
            resp = await client.post(
                f"{HYPERLIQUID_BASE}/info",
                json=payload,
                timeout=30,
            )
            if resp.status_code == 429:
                wait = HL_RETRY_DELAY * (attempt + 1)
                logger.warning("hl_rate_limited", wait=wait, attempt=attempt + 1)
                await asyncio.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError:
            raise
        except Exception:
            if attempt < retries - 1:
                await asyncio.sleep(HL_RETRY_DELAY)
                continue
            raise
    return []


# --- Hyperliquid Candles ---

async def fetch_hl_candles(
    client: httpx.AsyncClient,
    coin: str,
    interval: str,
    start_ms: int,
    end_ms: int,
) -> list[CandleData]:
    """Fetch historical candles from Hyperliquid.

    HL candleSnapshot returns up to ~5000 candles per request.
    We paginate by advancing startTime.
    """
    all_candles: list[CandleData] = []
    cursor = start_ms
    chunk_ms = 5000 * 4 * 3600 * 1000  # ~5000 bars × 4h in ms

    while cursor < end_ms:
        chunk_end = min(cursor + chunk_ms, end_ms)
        raw = await _hl_post(client, {
            "type": "candleSnapshot",
            "req": {
                "coin": coin,
                "interval": interval,
                "startTime": cursor,
                "endTime": chunk_end,
            },
        })

        if not raw:
            break

        for c in raw:
            all_candles.append(CandleData(
                timestamp_ms=int(c["t"]),
                open=float(c["o"]),
                high=float(c["h"]),
                low=float(c["l"]),
                close=float(c["c"]),
                volume=float(c["v"]),
            ))

        last_ts = int(raw[-1]["t"])
        if last_ts <= cursor:
            break
        cursor = last_ts + 1

        logger.info("hl_candles_chunk", coin=coin, count=len(all_candles), last_date=datetime.fromtimestamp(last_ts / 1000, tz=timezone.utc).date())
        await asyncio.sleep(HL_REQUEST_DELAY)

    return all_candles


# --- Hyperliquid Funding Rates ---

async def fetch_hl_funding(
    client: httpx.AsyncClient,
    coin: str,
    start_ms: int,
    end_ms: int,
) -> list[FundingSnapshot]:
    """Fetch historical funding from Hyperliquid info API."""
    all_funding: list[FundingSnapshot] = []
    cursor = start_ms

    while cursor < end_ms:
        raw = await _hl_post(client, {
            "type": "fundingHistory",
            "coin": coin,
            "startTime": cursor,
        })

        if not raw:
            break

        for r in raw:
            ts = int(r.get("time", 0))
            rate = float(r.get("fundingRate", 0))
            all_funding.append(FundingSnapshot(asset=coin, rate=rate, timestamp_ms=ts))

        last_ts = int(raw[-1].get("time", 0))
        if last_ts <= cursor:
            break
        cursor = last_ts + 1

        if len(raw) < 500:
            break

        logger.info("hl_funding_chunk", coin=coin, count=len(all_funding))
        await asyncio.sleep(HL_REQUEST_DELAY)

    return all_funding


# --- Fingerprint Computation ---

def compute_fingerprints(
    candles: dict[str, list[CandleData]],
    funding: dict[str, list[FundingSnapshot]],
    primary: str = "BTC",
    secondary: str = "ETH",
) -> list[Fingerprint]:
    """Compute fingerprints from backfilled data.

    Aligns candle data by timestamp, feeds incrementally to the engines,
    and produces one fingerprint per 4h bar (after warmup).
    """
    engine = FingerprintEngine(primary_asset=primary, secondary_asset=secondary)
    surface_engine = FundingSurfaceEngine(top_n=10)

    # Sort candles by timestamp
    primary_candles = sorted(candles.get(primary, []), key=lambda c: c.timestamp_ms)
    secondary_candles = sorted(candles.get(secondary, []), key=lambda c: c.timestamp_ms)

    if not primary_candles:
        logger.error("no_primary_candles", asset=primary)
        return []

    # Index secondary candles by timestamp for alignment
    secondary_by_ts: dict[int, CandleData] = {c.timestamp_ms: c for c in secondary_candles}

    # Build funding timeline: group by closest 4h bar
    funding_by_bar: dict[int, dict[str, list[float]]] = {}
    for asset_name, snapshots in funding.items():
        for snap in snapshots:
            bar_ms = (snap.timestamp_ms // (4 * 3600 * 1000)) * (4 * 3600 * 1000)
            if bar_ms not in funding_by_bar:
                funding_by_bar[bar_ms] = {}
            if asset_name not in funding_by_bar[bar_ms]:
                funding_by_bar[bar_ms][asset_name] = []
            funding_by_bar[bar_ms][asset_name].append(snap.rate)

    # Warmup: need 180 bars (~30 days) before we start fingerprinting
    warmup_bars = 180
    fingerprints: list[Fingerprint] = []

    for i, candle in enumerate(primary_candles):
        ts_sec = candle.timestamp_ms / 1000.0

        engine.update_candles(
            primary,
            closes=[candle.close],
            highs=[candle.high],
            lows=[candle.low],
            volumes=[candle.volume],
            timestamps=[ts_sec],
        )

        sec = secondary_by_ts.get(candle.timestamp_ms)
        if sec:
            engine.update_candles(
                secondary,
                closes=[sec.close],
                highs=[sec.high],
                lows=[sec.low],
                volumes=[sec.volume],
                timestamps=[ts_sec],
            )

        bar_ms = (candle.timestamp_ms // (4 * 3600 * 1000)) * (4 * 3600 * 1000)
        if bar_ms in funding_by_bar:
            rates = {}
            for asset_name, rate_list in funding_by_bar[bar_ms].items():
                rates[asset_name] = sum(rate_list) / len(rate_list)
            if rates:
                surface_engine.record(rates, timestamp=ts_sec)
                engine.update_funding(surface_engine.features())

        if i >= warmup_bars:
            fp = engine.compute(timestamp=ts_sec)
            fingerprints.append(fp)

    return fingerprints


# --- Main Backfill ---

async def run_backfill(
    lookback_days: int = LOOKBACK_DAYS,
    data_dir: str = "data/fingerprints",
) -> BackfillResult:
    """Run the full backfill pipeline.

    1. Fetch candles from Hyperliquid (BTC, ETH)
    2. Fetch funding rates from Hyperliquid (10 assets, sequentially with delays)
    3. Compute fingerprints
    4. Store in Parquet
    """
    result = BackfillResult()

    now_ms = int(time.time() * 1000)
    start_ms = now_ms - lookback_days * 24 * 3600 * 1000

    start_dt = datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc)
    end_dt = datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc)
    logger.info("backfill_start", start=str(start_dt)[:10], end=str(end_dt)[:10], days=lookback_days)

    async with httpx.AsyncClient(timeout=30) as client:
        # --- Fetch candles from Hyperliquid ---
        for asset in CANDLE_ASSETS:
            logger.info("fetching_candles", asset=asset, source="hyperliquid")
            try:
                candles = await fetch_hl_candles(client, asset, INTERVAL, start_ms, now_ms)
                result.candles[asset] = candles
                logger.info("candles_fetched", asset=asset, count=len(candles))
            except Exception as e:
                msg = f"Failed to fetch {asset} candles: {e}"
                logger.error(msg)
                result.errors.append(msg)
            await asyncio.sleep(HL_REQUEST_DELAY)

        # --- Fetch funding rates from Hyperliquid (sequentially) ---
        for asset in FUNDING_ASSETS:
            logger.info("fetching_funding", asset=asset, source="hyperliquid")
            try:
                funding = await fetch_hl_funding(client, asset, start_ms, now_ms)
                result.funding[asset] = funding
                logger.info("funding_fetched", asset=asset, count=len(funding))
            except Exception as e:
                msg = f"Failed to fetch {asset} funding: {e}"
                logger.warning(msg)
                result.errors.append(msg)
            # Longer delay between assets to respect rate limits
            await asyncio.sleep(HL_REQUEST_DELAY * 2)

    # --- Compute fingerprints ---
    logger.info("computing_fingerprints")
    result.fingerprints = compute_fingerprints(result.candles, result.funding)
    logger.info("fingerprints_computed", count=len(result.fingerprints))

    # --- Store ---
    store = FingerprintStore(data_dir)
    store.append_batch(result.fingerprints)
    stored = store.flush()
    logger.info("fingerprints_stored", count=stored, path=data_dir)

    # --- Summary ---
    if result.fingerprints:
        first_fp = result.fingerprints[0]
        last_fp = result.fingerprints[-1]
        first_dt = datetime.fromtimestamp(first_fp.timestamp, tz=timezone.utc)
        last_dt = datetime.fromtimestamp(last_fp.timestamp, tz=timezone.utc)
        logger.info(
            "backfill_complete",
            fingerprints=len(result.fingerprints),
            features=len(first_fp.vector),
            span=f"{first_dt.date()} to {last_dt.date()}",
            errors=len(result.errors),
        )

    return result


def print_summary(result: BackfillResult) -> None:
    """Print human-readable backfill summary."""
    print("\n=== Backfill Summary ===\n")

    print("Candles:")
    for asset, candles in result.candles.items():
        if candles:
            first = datetime.fromtimestamp(candles[0].timestamp_ms / 1000, tz=timezone.utc)
            last = datetime.fromtimestamp(candles[-1].timestamp_ms / 1000, tz=timezone.utc)
            print(f"  {asset}: {len(candles)} bars, {first.date()} -> {last.date()}")
            print(f"       price range: ${candles[0].close:,.0f} -> ${candles[-1].close:,.0f}")

    print("\nFunding rates:")
    for asset, funding in sorted(result.funding.items()):
        if funding:
            first = datetime.fromtimestamp(funding[0].timestamp_ms / 1000, tz=timezone.utc)
            last = datetime.fromtimestamp(funding[-1].timestamp_ms / 1000, tz=timezone.utc)
            print(f"  {asset}: {len(funding)} snapshots, {first.date()} -> {last.date()}")

    print(f"\nFingerprints: {len(result.fingerprints)}")
    if result.fingerprints:
        fp = result.fingerprints[-1]
        print(f"  Features: {len(fp.vector)}")
        print("  Latest vector sample:")
        for k in sorted(fp.vector.keys())[:10]:
            print(f"    {k}: {fp.vector[k]}")
        if len(fp.vector) > 10:
            print(f"    ... and {len(fp.vector) - 10} more")

    if result.errors:
        print(f"\nErrors ({len(result.errors)}):")
        for e in result.errors:
            print(f"  - {e}")


if __name__ == "__main__":
    result = asyncio.run(run_backfill())
    print_summary(result)
