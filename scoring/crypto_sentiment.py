"""Crypto-specific sentiment SubScore using xAI/Grok.

Scores a token 0–100 across three risk dimensions — legislation,
exploit/hack exposure, and social sentiment — using LLM analysis.
Designed to plug directly into ``CompositeScorer`` as a ``SubScore``.

Setup::

    # .env
    XAI_API_KEY=xai-...

Usage as a standalone scorer::

    from agents.llm_analyst_xai import XAIAnalyst
    from scoring.crypto_sentiment import CryptoSentimentScorer

    analyst = XAIAnalyst()
    scorer  = CryptoSentimentScorer(analyst=analyst)
    score   = await scorer.score("ETH")          # 0-100 (100 = bullish/safe)

Usage inside MultiFactorStrategy::

    from scoring.confidence import CompositeScorer, SubScore
    from scoring.crypto_sentiment import CryptoSentimentScorer

    sentiment = CryptoSentimentScorer(analyst=analyst)

    composite = CompositeScorer()
    composite.register(SubScore("sentiment",  0.40, sentiment.score_sync))
    composite.register(SubScore("momentum",   0.35, momentum_fn))
    composite.register(SubScore("whale",      0.25, whale_fn))

Scoring dimensions (each 0–100, averaged with equal weight):

- **Legislation risk** — adverse regulatory headlines (0 = ban/crackdown, 100 = favourable)
- **Exploit/hack exposure** — recent protocol vulnerabilities (0 = active exploit, 100 = clean)
- **Social sentiment** — community mood and trending narrative (0 = FUD, 100 = euphoric)

Results are cached for ``cache_ttl_seconds`` (default 2 hours) per token
to avoid burning API quota on repeated calls within the same run.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import dataclass

import structlog

logger = structlog.get_logger()

CACHE_TTL_SECONDS = 7200  # 2 hours — matches LLMAnalyst default


@dataclass
class SentimentResult:
    """Breakdown of the three scoring dimensions."""

    token: str
    legislation_score: float   # 0-100
    exploit_score: float       # 0-100
    sentiment_score: float     # 0-100
    composite: float           # 0-100 (equal-weight average)
    confidence: float          # 0-100 (from LLM variance)
    reasoning: str


class CryptoSentimentScorer:
    """Score a crypto token's risk/sentiment environment using an LLM analyst.

    Designed to be used as a ``SubScore`` function inside ``CompositeScorer``,
    or called directly for a full breakdown via ``score_full()``.

    Args:
        analyst: Any ``LLMAnalyst``-compatible instance. Recommended: ``XAIAnalyst``.
        cache_ttl_seconds: How long to cache scores per token (default 2 hours).
        context_fn: Optional async callable ``(token: str) -> str`` that returns
                    additional context (e.g. recent price, TVL, recent news headlines).
                    When provided, its output is appended to each LLM prompt.
    """

    def __init__(
        self,
        analyst,
        cache_ttl_seconds: int = CACHE_TTL_SECONDS,
        context_fn=None,
    ):
        self.analyst = analyst
        self.cache_ttl = cache_ttl_seconds
        self.context_fn = context_fn
        self._cache: dict[str, tuple[SentimentResult, float]] = {}

    async def score(self, token: str) -> float:
        """Return composite 0-100 score. Suitable as a SubScore function."""
        result = await self.score_full(token)
        return result.composite

    def score_sync(self, token: str) -> float:
        """Synchronous wrapper for use in CompositeScorer SubScore callbacks.

        Runs the async score() in the current event loop or a new one.
        """
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # Inside an async context — schedule as a task
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor() as pool:
                    future = pool.submit(asyncio.run, self.score(token))
                    return future.result()
            return loop.run_until_complete(self.score(token))
        except RuntimeError:
            return asyncio.run(self.score(token))

    async def score_full(self, token: str) -> SentimentResult:
        """Return full breakdown across all three scoring dimensions."""
        cache_key = hashlib.sha256(token.upper().encode()).hexdigest()[:16]
        if cache_key in self._cache:
            result, ts = self._cache[cache_key]
            if time.time() - ts < self.cache_ttl:
                logger.debug("sentiment_cache_hit", token=token)
                return result

        context = ""
        if self.context_fn is not None:
            try:
                context = await self.context_fn(token)
            except Exception as exc:
                logger.warning("context_fn_failed", token=token, error=str(exc))

        leg, exp, soc = await asyncio.gather(
            self._score_legislation(token, context),
            self._score_exploit(token, context),
            self._score_sentiment(token, context),
        )

        composite = (leg.probability * 100 + exp.probability * 100 + soc.probability * 100) / 3
        confidence = (leg.confidence + exp.confidence + soc.confidence) / 3

        result = SentimentResult(
            token=token.upper(),
            legislation_score=round(leg.probability * 100, 1),
            exploit_score=round(exp.probability * 100, 1),
            sentiment_score=round(soc.probability * 100, 1),
            composite=round(composite, 1),
            confidence=round(confidence, 1),
            reasoning=(
                f"Legislation: {leg.probability:.0%} | "
                f"Exploit: {exp.probability:.0%} | "
                f"Sentiment: {soc.probability:.0%}"
            ),
        )

        self._cache[cache_key] = (result, time.time())
        logger.info(
            "crypto_sentiment_scored",
            token=token,
            composite=result.composite,
            confidence=result.confidence,
        )
        return result

    # ── Dimension prompts ──────────────────────────────────────────────────────

    async def _score_legislation(self, token: str, context: str):
        return await self.analyst.predict(
            question=(
                f"Is the near-term regulatory environment for {token} "
                f"favourable (i.e. no active ban, crackdown, or adverse legislation "
                f"likely in the next 30 days)?"
            ),
            context=context,
        )

    async def _score_exploit(self, token: str, context: str):
        return await self.analyst.predict(
            question=(
                f"Is {token}'s protocol/ecosystem currently free from active exploits, "
                f"hacks, or critical unpatched vulnerabilities that pose a near-term "
                f"price risk?"
            ),
            context=context,
        )

    async def _score_sentiment(self, token: str, context: str):
        return await self.analyst.predict(
            question=(
                f"Is the current social and community sentiment around {token} "
                f"net positive (i.e. more bullish narrative than FUD, rug-pull fears, "
                f"or developer exit concerns)?"
            ),
            context=context,
        )
