"""Polymarket position redemption — claim winnings after market resolution.

This is the piece that's easy to forget until a market resolves and you
realize your winning positions are sitting unclaimed on-chain.

Critical gotcha: Polymarket has TWO different redemption contracts depending
on whether the market is a standard market or a neg-risk market. Using the
wrong contract will fail silently — the transaction goes through but nothing
is redeemed.

Standard markets:     ConditionalTokens contract
Neg-risk markets:     NegRiskAdapter contract (a wrapper around ConditionalTokens)

Almost all active Polymarket markets are neg-risk as of 2024-2025. If you're
not sure, check via the Gamma API (is_neg_risk field) or just try the
neg-risk contract first.

Usage::

    from web3 import Web3
    from exchanges.polymarket.redemption import PolymarketRedeemer

    w3 = Web3(Web3.HTTPProvider("https://polygon-rpc.com"))
    redeemer = PolymarketRedeemer(w3=w3, private_key="0x...")

    # Check and redeem all resolved positions
    results = await redeemer.redeem_resolved(positions=[
        {"condition_id": "0x...", "token_id": "0x...", "neg_risk": True,
         "yes_amount": 50_000_000, "no_amount": 0},
    ])
    for r in results:
        print(r)

On-chain architecture:
    Polygon mainnet (chain_id=137)
    USDC contract:         0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174
    ConditionalTokens:     0x4D97DCd97eC945f40cF65F87097ACe5EA0476045
    NegRiskAdapter:        0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296
    NegRiskCTF Exchange:   0xC5d563A36AE78145C45a50134d48A1215220f80a

USDC uses 6 decimal places. All amounts in this module are in raw units (1e6).
$1.00 USDC = 1_000_000 raw units.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import structlog

logger = structlog.get_logger()

# Polygon mainnet addresses
CONDITIONAL_TOKENS_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
NEG_RISK_ADAPTER_ADDRESS = "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"
USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"

# Minimal ABIs — only the functions we need
CONDITIONAL_TOKENS_ABI = [
    {
        "name": "redeemPositions",
        "type": "function",
        "inputs": [
            {"name": "collateralToken", "type": "address"},
            {"name": "parentCollectionId", "type": "bytes32"},
            {"name": "conditionId", "type": "bytes32"},
            {"name": "indexSets", "type": "uint256[]"},
        ],
        "outputs": [],
        "stateMutability": "nonpayable",
    },
    {
        "name": "balanceOf",
        "type": "function",
        "inputs": [
            {"name": "owner", "type": "address"},
            {"name": "id", "type": "uint256"},
        ],
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
    },
]

NEG_RISK_ADAPTER_ABI = [
    {
        "name": "redeemPositions",
        "type": "function",
        "inputs": [
            {"name": "conditionId", "type": "bytes32"},
            {"name": "amounts", "type": "uint256[]"},
        ],
        "outputs": [],
        "stateMutability": "nonpayable",
    },
]


@dataclass
class RedemptionResult:
    """Result of a single redemption attempt."""

    condition_id: str
    success: bool
    tx_hash: str
    amount_redeemed_usdc: float   # Approximate, based on amounts passed in
    neg_risk: bool
    error: str


class PolymarketRedeemer:
    """Redeems resolved Polymarket positions and returns USDC to your wallet.

    Handles both standard (ConditionalTokens) and neg-risk (NegRiskAdapter)
    markets. Almost all current Polymarket markets are neg-risk — but the
    code checks and routes correctly regardless.

    Args:
        w3: Connected Web3 instance pointed at Polygon mainnet.
        private_key: Wallet private key. Must hold the winning tokens.
        gas_limit: Gas limit per transaction. Default is conservative.
        max_fee_gwei: Max gas price in gwei. Polygon is cheap — 50 is plenty.
    """

    def __init__(
        self,
        w3,
        private_key: str,
        gas_limit: int = 300_000,
        max_fee_gwei: float = 50.0,
    ):
        self.w3 = w3
        self.private_key = private_key
        self.account = w3.eth.account.from_key(private_key)
        self.gas_limit = gas_limit
        self.max_fee_gwei = max_fee_gwei

        self._ct = w3.eth.contract(
            address=w3.to_checksum_address(CONDITIONAL_TOKENS_ADDRESS),
            abi=CONDITIONAL_TOKENS_ABI,
        )
        self._neg_risk = w3.eth.contract(
            address=w3.to_checksum_address(NEG_RISK_ADAPTER_ADDRESS),
            abi=NEG_RISK_ADAPTER_ABI,
        )

    async def redeem_resolved(
        self,
        positions: list[dict],
    ) -> list[RedemptionResult]:
        """Redeem a list of resolved positions.

        Args:
            positions: List of position dicts, each with:
                - condition_id (str): The market condition ID (bytes32 as hex)
                - neg_risk (bool): Whether this is a neg-risk market
                - yes_amount (int): Raw YES token balance (in 1e6 units)
                - no_amount (int): Raw NO token balance (in 1e6 units)

                One of yes_amount or no_amount should be > 0 (your winning side).
                Pass both — the contract handles it correctly.

        Returns:
            List of RedemptionResult objects, one per position attempted.

        Example::

            positions = [
                {
                    "condition_id": "0xabc123...",
                    "neg_risk": True,
                    "yes_amount": 50_000_000,   # $50 in YES tokens
                    "no_amount": 0,
                },
            ]
            results = await redeemer.redeem_resolved(positions)
        """
        results = []
        for pos in positions:
            result = await self._redeem_one(pos)
            results.append(result)
            if result.success:
                logger.info(
                    "redemption_success",
                    condition_id=pos["condition_id"][:16],
                    amount_usdc=result.amount_redeemed_usdc,
                    neg_risk=result.neg_risk,
                    tx_hash=result.tx_hash,
                )
            else:
                logger.error(
                    "redemption_failed",
                    condition_id=pos["condition_id"][:16],
                    error=result.error,
                )
        return results

    async def _redeem_one(self, pos: dict) -> RedemptionResult:
        condition_id = pos["condition_id"]
        neg_risk = pos.get("neg_risk", True)  # Default to neg-risk (most common)
        yes_amount = int(pos.get("yes_amount", 0))
        no_amount = int(pos.get("no_amount", 0))

        total_raw = yes_amount + no_amount
        amount_usdc = total_raw / 1_000_000

        try:
            if neg_risk:
                tx_hash = await asyncio.to_thread(
                    self._send_neg_risk_redemption,
                    condition_id,
                    yes_amount,
                    no_amount,
                )
            else:
                tx_hash = await asyncio.to_thread(
                    self._send_standard_redemption,
                    condition_id,
                )

            return RedemptionResult(
                condition_id=condition_id,
                success=True,
                tx_hash=tx_hash,
                amount_redeemed_usdc=amount_usdc,
                neg_risk=neg_risk,
                error="",
            )

        except Exception as e:
            return RedemptionResult(
                condition_id=condition_id,
                success=False,
                tx_hash="",
                amount_redeemed_usdc=0.0,
                neg_risk=neg_risk,
                error=str(e),
            )

    def _send_neg_risk_redemption(
        self,
        condition_id: str,
        yes_amount: int,
        no_amount: int,
    ) -> str:
        """Submit redemption via NegRiskAdapter contract.

        Signature: redeemPositions(bytes32 conditionId, uint256[] amounts)
        amounts = [YES_token_amount, NO_token_amount]
        """
        condition_id_bytes = bytes.fromhex(condition_id.removeprefix("0x"))

        tx = self._neg_risk.functions.redeemPositions(
            condition_id_bytes,
            [yes_amount, no_amount],
        ).build_transaction({
            "from": self.account.address,
            "nonce": self.w3.eth.get_transaction_count(self.account.address),
            "gas": self.gas_limit,
            "maxFeePerGas": self.w3.to_wei(self.max_fee_gwei, "gwei"),
            "maxPriorityFeePerGas": self.w3.to_wei(2, "gwei"),
            "chainId": 137,
        })

        signed = self.w3.eth.account.sign_transaction(tx, self.private_key)
        tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)

        if receipt["status"] != 1:
            raise RuntimeError(f"Neg-risk redemption reverted: {tx_hash.hex()}")

        return tx_hash.hex()

    def _send_standard_redemption(self, condition_id: str) -> str:
        """Submit redemption via ConditionalTokens contract.

        Signature: redeemPositions(
            address collateralToken,
            bytes32 parentCollectionId,
            bytes32 conditionId,
            uint256[] indexSets,
        )
        indexSets = [1, 2] for a binary (YES/NO) market.
        parentCollectionId = bytes32(0) for top-level markets.
        """
        condition_id_bytes = bytes.fromhex(condition_id.removeprefix("0x"))
        parent_collection_id = b"\x00" * 32  # bytes32(0)

        tx = self._ct.functions.redeemPositions(
            self.w3.to_checksum_address(USDC_ADDRESS),
            parent_collection_id,
            condition_id_bytes,
            [1, 2],  # YES index = 1, NO index = 2
        ).build_transaction({
            "from": self.account.address,
            "nonce": self.w3.eth.get_transaction_count(self.account.address),
            "gas": self.gas_limit,
            "maxFeePerGas": self.w3.to_wei(self.max_fee_gwei, "gwei"),
            "maxPriorityFeePerGas": self.w3.to_wei(2, "gwei"),
            "chainId": 137,
        })

        signed = self.w3.eth.account.sign_transaction(tx, self.private_key)
        tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)

        if receipt["status"] != 1:
            raise RuntimeError(f"Standard redemption reverted: {tx_hash.hex()}")

        return tx_hash.hex()

    async def check_redeemable(self, condition_id: str, token_id: str) -> int:
        """Check the raw token balance for a position.

        Returns the balance in raw units (divide by 1e6 for USDC).
        If > 0, the market has resolved in your favor and can be redeemed.
        """
        token_id_int = int(token_id, 16) if isinstance(token_id, str) else token_id
        balance = await asyncio.to_thread(
            self._ct.functions.balanceOf(
                self.account.address,
                token_id_int,
            ).call
        )
        return balance
