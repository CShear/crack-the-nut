"""Generic DEX adapter for on-chain trading (Uniswap V3 style).

Wraps web3.py for transaction building, signing, and broadcasting.

Requires: pip install web3
"""

from __future__ import annotations


import structlog

from exchanges.base import ExchangeAdapter, OrderResult, BalanceInfo

logger = structlog.get_logger()

DEADLINE_SECONDS = 300


class DexAdapter(ExchangeAdapter):
    """On-chain DEX adapter (Uniswap V3, Aerodrome, etc.).

    Usage::

        adapter = DexAdapter(
            rpc_url="https://mainnet.base.org",
            private_key="0x...",
            router_address="0x...",
            router_abi=[...],
        )
        await adapter.connect()
    """

    def __init__(
        self,
        rpc_url: str,
        private_key: str,
        router_address: str,
        router_abi: list[dict],
        chain_id: int = 8453,  # Base mainnet
    ):
        self._rpc_url = rpc_url
        self._private_key = private_key
        self._router_address = router_address
        self._router_abi = router_abi
        self._chain_id = chain_id
        self._w3 = None
        self._router = None
        self._account = None

    async def connect(self) -> None:
        from web3 import Web3

        self._w3 = Web3(Web3.HTTPProvider(self._rpc_url))
        if not self._w3.is_connected():
            raise ConnectionError(f"Cannot connect to {self._rpc_url}")

        self._account = self._w3.eth.account.from_key(self._private_key)
        self._router = self._w3.eth.contract(
            address=Web3.to_checksum_address(self._router_address),
            abi=self._router_abi,
        )
        logger.info("dex_connected", chain_id=self._chain_id, account=self._account.address)

    async def disconnect(self) -> None:
        self._w3 = None

    async def get_balance(self) -> BalanceInfo:
        """Get ETH balance."""
        from web3 import Web3

        wei = self._w3.eth.get_balance(self._account.address)
        eth = float(Web3.from_wei(wei, "ether"))
        return BalanceInfo(equity=eth, available=eth, currency="ETH")

    def get_token_balance(self, token_address: str, decimals: int = 18) -> float:
        """Get ERC20 token balance."""
        from web3 import Web3

        ERC20_BALANCE_ABI = [
            {
                "inputs": [{"name": "account", "type": "address"}],
                "name": "balanceOf",
                "outputs": [{"name": "", "type": "uint256"}],
                "stateMutability": "view",
                "type": "function",
            }
        ]
        token = self._w3.eth.contract(
            address=Web3.to_checksum_address(token_address),
            abi=ERC20_BALANCE_ABI,
        )
        raw = token.functions.balanceOf(self._account.address).call()
        return raw / (10**decimals)

    async def place_order(
        self,
        symbol: str,
        side: str,
        amount: float,
        price: float | None = None,
        order_type: str = "market",
        **kwargs,
    ) -> OrderResult:
        """Execute a swap via the router. symbol is ignored; use token addresses in kwargs."""
        token_in = kwargs.get("token_in")
        token_out = kwargs.get("token_out")
        fee = kwargs.get("fee", 500)
        amount_in_raw = kwargs.get("amount_in_raw", 0)

        if not token_in or not token_out or not amount_in_raw:
            raise ValueError("DEX orders require token_in, token_out, amount_in_raw in kwargs")

        tx_hash = self._swap_exact_input(token_in, token_out, fee, amount_in_raw)
        return OrderResult(
            order_id=tx_hash,
            filled=True,
            fill_price=0,  # Actual price computed from events
            fill_amount=amount,
            raw={"tx_hash": tx_hash},
        )

    async def cancel_order(self, symbol: str, order_id: str) -> bool:
        return False  # On-chain swaps can't be cancelled

    async def get_positions(self) -> list[dict]:
        return []  # LP positions tracked separately

    async def get_orderbook(self, symbol: str, depth: int = 5) -> dict:
        return {"bids": [], "asks": []}  # AMMs don't have order books

    def _swap_exact_input(
        self,
        token_in: str,
        token_out: str,
        fee: int,
        amount_in: int,
    ) -> str:
        """Build, sign, and broadcast a swap transaction."""
        from web3 import Web3

        params = (
            Web3.to_checksum_address(token_in),
            Web3.to_checksum_address(token_out),
            fee,
            Web3.to_checksum_address(self._account.address),
            amount_in,
            0,  # amountOutMinimum
            0,  # sqrtPriceLimitX96
        )

        func = self._router.functions.exactInputSingle(params)
        tx = func.build_transaction(
            {
                "from": self._account.address,
                "nonce": self._w3.eth.get_transaction_count(self._account.address),
                "gas": 300_000,
                "maxFeePerGas": self._w3.eth.gas_price * 2,
                "maxPriorityFeePerGas": self._w3.to_wei(0.1, "gwei"),
                "chainId": self._chain_id,
            }
        )

        signed = self._w3.eth.account.sign_transaction(tx, self._private_key)
        tx_hash = self._w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = self._w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)

        if receipt["status"] != 1:
            raise RuntimeError(f"Swap reverted: {tx_hash.hex()}")

        logger.info("swap_complete", tx=tx_hash.hex())
        return tx_hash.hex()

    def get_gas_price_gwei(self) -> float:
        """Get current gas price in gwei."""
        from web3 import Web3

        return float(Web3.from_wei(self._w3.eth.gas_price, "gwei"))

    def get_eth_balance(self) -> float:
        from web3 import Web3

        wei = self._w3.eth.get_balance(self._account.address)
        return float(Web3.from_wei(wei, "ether"))
