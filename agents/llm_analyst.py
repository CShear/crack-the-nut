"""LLM-powered market analysis with ensemble predictions.

Uses temperature diversity (3 calls at different temperatures) and takes
the median estimate. Confidence is derived from prediction variance —
low variance = high confidence.

Supports both Anthropic and OpenAI SDKs.

Usage::

    analyst = LLMAnalyst(provider="anthropic", api_key="sk-ant-...")
    result = await analyst.predict(
        question="Will BTC exceed $100K by end of Q2?",
        context="Current price: $95K, momentum bullish...",
    )
    print(result)  # PredictionResult(probability=0.65, confidence=82, reasoning="...")
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import dataclass

import structlog

logger = structlog.get_logger()

# Default temperatures for ensemble diversity
DEFAULT_TEMPS = (0.3, 0.5, 0.7)
CACHE_TTL_SECONDS = 7200  # 2 hours


@dataclass
class PredictionResult:
    """Result of an LLM ensemble prediction."""

    probability: float  # 0.0 - 1.0
    confidence: float  # 0 - 100
    reasoning: str
    individual_estimates: list[float]
    variance: float


class LLMAnalyst:
    """Ensemble LLM analyst — runs multiple predictions at different
    temperatures and returns the median with confidence from variance.
    """

    def __init__(
        self,
        provider: str = "anthropic",
        api_key: str = "",
        model: str = "",
        temperatures: tuple[float, ...] = DEFAULT_TEMPS,
        system_prompt: str = "",
    ):
        self.provider = provider
        self.api_key = api_key
        self.model = model or self._default_model()
        self.temperatures = temperatures
        self.system_prompt = system_prompt or SUPERFORECASTER_PROMPT
        self._cache: dict[str, tuple[PredictionResult, float]] = {}

    def _default_model(self) -> str:
        if self.provider == "anthropic":
            return "claude-haiku-4-5-20251001"
        return "gpt-4o-mini"

    async def predict(
        self,
        question: str,
        context: str = "",
        use_cache: bool = True,
    ) -> PredictionResult:
        """Run ensemble prediction and return result."""
        cache_key = self._cache_key(question, context)

        if use_cache and cache_key in self._cache:
            result, ts = self._cache[cache_key]
            if time.time() - ts < CACHE_TTL_SECONDS:
                logger.debug("cache_hit", question=question[:50])
                return result

        prompt = self._build_prompt(question, context)
        estimates = await self._run_ensemble(prompt)

        if not estimates:
            return PredictionResult(
                probability=0.5,
                confidence=0,
                reasoning="Failed to get LLM predictions",
                individual_estimates=[],
                variance=0,
            )

        estimates.sort()
        median = estimates[len(estimates) // 2]
        variance = sum((e - median) ** 2 for e in estimates) / len(estimates)

        # Confidence: low variance = high confidence
        # variance of 0 → 100, variance of 0.05 → ~50, variance of 0.1+ → ~20
        confidence = max(0, min(100, 100 - variance * 1000))

        result = PredictionResult(
            probability=round(median, 3),
            confidence=round(confidence, 1),
            reasoning=f"Ensemble median of {len(estimates)} predictions",
            individual_estimates=estimates,
            variance=round(variance, 6),
        )

        self._cache[cache_key] = (result, time.time())
        return result

    async def _run_ensemble(self, prompt: str) -> list[float]:
        """Run predictions at each temperature in parallel."""
        tasks = [self._single_prediction(prompt, temp) for temp in self.temperatures]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        estimates = []
        for r in results:
            if isinstance(r, float):
                estimates.append(r)
            elif isinstance(r, Exception):
                logger.warning("ensemble_call_failed", error=str(r))
        return estimates

    async def _single_prediction(self, prompt: str, temperature: float) -> float:
        """Make a single LLM call and parse probability from response."""
        if self.provider == "anthropic":
            return await self._call_anthropic(prompt, temperature)
        return await self._call_openai(prompt, temperature)

    async def _call_anthropic(self, prompt: str, temperature: float) -> float:
        import anthropic

        client = anthropic.AsyncAnthropic(api_key=self.api_key)
        response = await client.messages.create(
            model=self.model,
            max_tokens=300,
            temperature=temperature,
            system=self.system_prompt,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text
        return self._parse_probability(text)

    async def _call_openai(self, prompt: str, temperature: float) -> float:
        from openai import AsyncOpenAI

        client = AsyncOpenAI(api_key=self.api_key)
        response = await client.chat.completions.create(
            model=self.model,
            max_tokens=300,
            temperature=temperature,
            messages=[
                {"role": "system", "content": self.system_prompt},
                {"role": "user", "content": prompt},
            ],
        )
        text = response.choices[0].message.content or ""
        return self._parse_probability(text)

    @staticmethod
    def _parse_probability(text: str) -> float:
        """Extract probability from LLM response.

        Expected format: PROBABILITY: 0.XX
        """
        for line in text.split("\n"):
            line = line.strip()
            if line.upper().startswith("PROBABILITY:"):
                val = line.split(":", 1)[1].strip()
                prob = float(val)
                return max(0.01, min(0.99, prob))
        raise ValueError(f"Could not parse probability from: {text[:100]}")

    def _build_prompt(self, question: str, context: str) -> str:
        parts = [f"Estimate the probability that the following resolves YES.\n\nQuestion: {question}"]
        if context:
            parts.append(f"\nContext:\n{context}")
        parts.append(
            "\nYou MUST begin your response with:\nPROBABILITY: 0.XX\nREASONING: <your 1-3 sentence justification>"
        )
        return "\n".join(parts)

    @staticmethod
    def _cache_key(question: str, context: str) -> str:
        raw = f"{question}|{context}"
        return hashlib.sha256(raw.encode()).hexdigest()[:16]


SUPERFORECASTER_PROMPT = (
    "You are a superforecaster — a probability estimator with a strong track record "
    "of well-calibrated predictions. Your goal is to maximize accuracy by minimizing "
    "Brier scores. You are precise, evidence-based, and resistant to narrative bias. "
    "Consider base rates and reference classes. Be careful not to anchor on market prices."
)
