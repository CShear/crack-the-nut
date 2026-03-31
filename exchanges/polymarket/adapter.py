"""Polymarket CLOB exchange adapter.

Wraps py-clob-client for prediction market trading.

Requires: pip install py-clob-client
"""

from __future__ import annotations

import structlog

from exchanges.base import ExchangeAdapter, OrderResult, BalanceInfo

logger = structlog.get_logger()


class PolymarketAdapter(ExchangeAdapter):
    """Adapter for Polymarket's CLOB (Central Limit Order Book).

    Key gotchas (from production experience):
    - Almost all active markets are negRisk — the client auto-detects this
    - FOK orders at midpoint DON'T fill — price at effective ask
    - Price must be rounded to 2 decimal places
    - Gamma API condition_id param is broken — use slug instead

    Usage::

        adapter = PolymarketAdapter(
            private_key="0x...",
            api_key="...",
            api_secret="...",
        )
        await adapter.connect()
    """

    def __init__(
        self,
        private_key: str,
        api_key: str = "",
        api_secret: str = "",
        api_passphrase: str = "",
        funder: str = "",
    ):
        self._private_key = private_key
        self._api_key = api_key
        self._api_secret = api_secret
        self._api_passphrase = api_passphrase
        self._funder = funder
        self._client = None

    async def connect(self) -> None:
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import ApiCreds

        host = "https://clob.polymarket.com"
        creds = ApiCreds(
            api_key=self._api_key,
            api_secret=self._api_secret,
            api_passphrase=self._api_passphrase,
        )
        self._client = ClobClient(
            host,
            key=self._private_key,
            chain_id=137,
            creds=creds,
            funder=self._funder or None,
        )
        logger.info("polymarket_connected")

    async def disconnect(self) -> None:
        self._client = None

    async def get_balance(self) -> BalanceInfo:
        """This requires checking on-chain USDC balance on Polygon."""
        # py-clob-client doesn't have a balance endpoint
        # In production, check USDC balance via web3
        return BalanceInfo(equity=0, available=0, currency="USDC")

    async def place_order(
        self,
        symbol: str,
        side: str,
        amount: float,
        price: float | None = None,
        order_type: str = "market",
        **kwargs,
    ) -> OrderResult:
        """Place a FOK order on a prediction market.

        Args:
            symbol: The token_id of the outcome to trade.
            side: "buy" or "sell".
            amount: Size in USDC.
            price: Limit price (0.01 - 0.99). Required.
            kwargs: token_id (str) if different from symbol.
        """
        from py_clob_client.clob_types import OrderArgs, OrderType
        import asyncio

        if price is None:
            raise ValueError("Polymarket orders require a price (0.01-0.99)")

        token_id = kwargs.get("token_id", symbol)
        price = round(price, 2)  # MUST round to 2 decimals

        # Trigger negRisk detection (py-clob-client uses this internally)
        await asyncio.to_thread(self._client.get_neg_risk, token_id)

        order_args = OrderArgs(
            token_id=token_id,
            price=price,
            size=amount,
            side=side.upper(),
            fee_rate_bps=0,
            nonce=0,
            expiration=0,
        )

        # Create and post FOK order
        signed = await asyncio.to_thread(self._client.create_order, order_args)
        result = await asyncio.to_thread(
            self._client.post_order,
            signed,
            OrderType.FOK,  # Fill-or-Kill
        )

        success = result.get("success", False)
        order_id = result.get("orderID", "")

        return OrderResult(
            order_id=order_id,
            filled=success,
            fill_price=price,
            fill_amount=amount if success else 0,
            raw=result,
        )

    async def cancel_order(self, symbol: str, order_id: str) -> bool:
        import asyncio

        result = await asyncio.to_thread(self._client.cancel, order_id)
        return result.get("success", False)

    async def get_positions(self) -> list[dict]:
        return []  # Positions tracked via executed_trades in DB

    async def get_orderbook(self, symbol: str, depth: int = 5) -> dict:
        """Get order book for a token_id."""
        import asyncio

        book = await asyncio.to_thread(self._client.get_order_book, symbol)
        bids = [(float(o["price"]), float(o["size"])) for o in book.get("bids", [])[:depth]]
        asks = [(float(o["price"]), float(o["size"])) for o in book.get("asks", [])[:depth]]
        return {"bids": bids, "asks": asks}

    async def get_market_price(self, token_id: str) -> float:
        """Get effective market price from CLOB order book."""
        book = await self.get_orderbook(token_id, depth=1)
        if book["asks"]:
            return book["asks"][0][0]
        if book["bids"]:
            return book["bids"][0][0]
        return 0.0
