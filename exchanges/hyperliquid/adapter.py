"""Hyperliquid exchange adapter.

Wraps the synchronous hyperliquid-python-sdk in async calls via
asyncio.to_thread(). This is the battle-tested pattern from production.

Requires: pip install hyperliquid-python-sdk
"""

from __future__ import annotations

import asyncio

import structlog

from exchanges.base import ExchangeAdapter, OrderResult, BalanceInfo

logger = structlog.get_logger()


class HyperliquidAdapter(ExchangeAdapter):
    """Async adapter for Hyperliquid perpetual futures.

    Usage::

        adapter = HyperliquidAdapter(
            private_key="0x...",
            account_address="0x...",
        )
        await adapter.connect()
        balance = await adapter.get_balance()
        result = await adapter.place_order("BTC", "buy", 0.001, order_type="market")
    """

    def __init__(
        self,
        private_key: str,
        account_address: str,
        api_wallet_address: str | None = None,
        use_testnet: bool = False,
    ):
        self._private_key = private_key
        self._account_address = account_address
        self._api_wallet_address = api_wallet_address
        self._use_testnet = use_testnet
        self._info = None  # hyperliquid Info client
        self._exchange = None  # hyperliquid Exchange client
        self._base_url = "https://api.hyperliquid-testnet.xyz" if use_testnet else "https://api.hyperliquid.xyz"

    async def connect(self) -> None:
        """Initialize SDK clients (sync SDK, called once)."""
        from hyperliquid.info import Info
        from hyperliquid.exchange import Exchange
        from hyperliquid.utils import constants

        base_url = constants.TESTNET_API_URL if self._use_testnet else constants.MAINNET_API_URL
        self._info = Info(base_url, skip_ws=True)

        vault_address = None
        self._exchange = Exchange(
            wallet=None,
            base_url=base_url,
            vault_address=vault_address,
            account_address=self._account_address,
        )
        # Set up API wallet signing if provided
        if self._api_wallet_address:
            self._exchange.account_address = self._account_address
            self._exchange.wallet = None  # Will be set by SDK from private key

        logger.info("hyperliquid_connected", testnet=self._use_testnet)

    async def disconnect(self) -> None:
        self._info = None
        self._exchange = None

    async def get_balance(self) -> BalanceInfo:
        """Query account balance (unified account: spot + perps)."""
        state = await asyncio.to_thread(self._info.user_state, self._account_address)
        # Unified account: crossMarginSummary has accountValue
        margin = state.get("crossMarginSummary", {})
        equity = float(margin.get("accountValue", 0))
        used = float(margin.get("totalMarginUsed", 0))
        return BalanceInfo(
            equity=equity,
            available=equity - used,
            currency="USD",
        )

    async def place_order(
        self,
        symbol: str,
        side: str,
        amount: float,
        price: float | None = None,
        order_type: str = "market",
        leverage: int = 3,
        reduce_only: bool = False,
    ) -> OrderResult:
        """Place a market or limit order.

        The HL SDK is synchronous, so we wrap in asyncio.to_thread().
        """
        is_buy = side.lower() in ("buy", "long")

        if order_type == "market":
            # Market order: use slippage price
            mid = await self._get_mid_price(symbol)
            slippage = 0.003  # 0.3%
            px = mid * (1 + slippage) if is_buy else mid * (1 - slippage)
            order_spec = {
                "coin": symbol,
                "is_buy": is_buy,
                "sz": amount,
                "limit_px": round(px, _sig_figs(px)),
                "order_type": {"limit": {"tif": "Ioc"}},
                "reduce_only": reduce_only,
            }
        else:
            order_spec = {
                "coin": symbol,
                "is_buy": is_buy,
                "sz": amount,
                "limit_px": price,
                "order_type": {"limit": {"tif": "Gtc"}},
                "reduce_only": reduce_only,
            }

        result = await asyncio.to_thread(self._exchange.order, **order_spec)

        status = result.get("status", "unknown")
        filled = status == "ok"
        order_id = ""
        if filled and "response" in result:
            statuses = result["response"].get("data", {}).get("statuses", [])
            if statuses and "resting" in statuses[0]:
                order_id = str(statuses[0]["resting"]["oid"])
            elif statuses and "filled" in statuses[0]:
                order_id = str(statuses[0]["filled"]["oid"])

        return OrderResult(
            order_id=order_id,
            filled=filled,
            fill_price=price or px,
            fill_amount=amount if filled else 0,
            raw=result,
        )

    async def cancel_order(self, symbol: str, order_id: str) -> bool:
        result = await asyncio.to_thread(self._exchange.cancel, symbol, int(order_id))
        return result.get("status") == "ok"

    async def get_positions(self) -> list[dict]:
        state = await asyncio.to_thread(self._info.user_state, self._account_address)
        positions = []
        for p in state.get("assetPositions", []):
            pos = p.get("position", {})
            size = float(pos.get("szi", 0))
            if size == 0:
                continue
            positions.append(
                {
                    "symbol": pos.get("coin"),
                    "side": "long" if size > 0 else "short",
                    "size": abs(size),
                    "entry_price": float(pos.get("entryPx", 0)),
                    "unrealized_pnl": float(pos.get("unrealizedPnl", 0)),
                    "leverage": float(pos.get("leverage", {}).get("value", 1)),
                }
            )
        return positions

    async def get_orderbook(self, symbol: str, depth: int = 5) -> dict:
        book = await asyncio.to_thread(self._info.l2_snapshot, symbol)
        return {
            "bids": [(float(p["px"]), float(p["sz"])) for p in book["levels"][0][:depth]],
            "asks": [(float(p["px"]), float(p["sz"])) for p in book["levels"][1][:depth]],
        }

    async def get_funding_rate(self, symbol: str) -> float | None:
        """Get predicted funding rate for a symbol."""
        meta = await asyncio.to_thread(self._info.meta_and_asset_ctxs)
        for ctx in meta[1]:
            if ctx.get("coin") == symbol or ctx.get("name") == symbol:
                return float(ctx.get("funding", 0))
        return None

    async def get_all_mids(self) -> dict[str, float]:
        """Get mid prices for all assets."""
        mids = await asyncio.to_thread(self._info.all_mids)
        return {k: float(v) for k, v in mids.items()}

    async def _get_mid_price(self, symbol: str) -> float:
        mids = await self.get_all_mids()
        if symbol not in mids:
            raise ValueError(f"No mid price for {symbol}")
        return mids[symbol]


def _sig_figs(x: float, figs: int = 5) -> int:
    """Compute decimal places for significant figures (HL requirement)."""
    if x == 0:
        return figs
    import math

    magnitude = math.floor(math.log10(abs(x)))
    return max(0, figs - 1 - magnitude)
