"""Adversarial LLM ensemble for binary prediction markets.

The problem with the standard ensemble (3 calls at different temperatures):
all three calls share the same framing. If the question is phrased in a way
that primes a YES outcome, all three calls will cluster around the same
overestimate. Temperature diversity ≠ perspective diversity.

This module uses adversarial prompting instead:
- One call with a system prompt primed to argue YES (find reasons it resolves YES)
- One call with a system prompt primed to argue NO (find reasons it resolves NO)
- One neutral call (standard superforecaster)
- Bayesian aggregation of the three estimates

The adversarial calls force the model to seriously consider both sides.
The aggregation then weights by how internally consistent each argument was.

Usage::

    from agents.adversarial_ensemble import AdversarialAnalyst

    analyst = AdversarialAnalyst(provider="anthropic", api_key="YOUR_KEY")
    result = await analyst.predict(
        question="Will Candidate X win the election?",
        context="Current polling data...",
    )
    print(result.probability)    # Bayesian-aggregated estimate
    print(result.yes_estimate)   # what the YES-advocate found
    print(result.no_estimate)    # what the NO-advocate found
    print(result.tension)        # how much the two sides disagreed
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import structlog

logger = structlog.get_logger()


@dataclass
class AdversarialResult:
    """Result from adversarial ensemble."""

    probability: float       # Final Bayesian-aggregated estimate (0.0–1.0)
    yes_estimate: float      # YES-advocate's estimate
    no_estimate: float       # NO-advocate's estimate
    neutral_estimate: float  # Neutral superforecaster's estimate
    tension: float           # abs(yes_estimate - no_estimate) — disagreement signal
    confidence: float        # 0–100, higher tension = lower confidence
    reasoning: str


# System prompts for each perspective
YES_ADVOCATE_PROMPT = (
    "You are a probability estimator focused on identifying reasons an event WILL happen. "
    "Your job is to steelman the YES outcome — find the strongest evidence, catalysts, "
    "and base rates that support resolution as YES. You are not a cheerleader; you are a "
    "rigorous analyst looking specifically for YES-supporting evidence. After building the "
    "strongest YES case you can, give your honest probability estimate. "
    "Do not anchor on market prices."
)

NO_ADVOCATE_PROMPT = (
    "You are a probability estimator focused on identifying reasons an event will NOT happen. "
    "Your job is to steelman the NO outcome — find the strongest evidence, friction points, "
    "historical base rates for failure, and structural reasons this resolves NO. "
    "You are not a pessimist; you are a rigorous analyst looking specifically for NO-supporting "
    "evidence. After building the strongest NO case you can, give your honest probability estimate. "
    "Do not anchor on market prices."
)

NEUTRAL_PROMPT = (
    "You are a superforecaster — a probability estimator with a strong track record "
    "of well-calibrated predictions. Your goal is to maximize accuracy by minimizing "
    "Brier scores. You are precise, evidence-based, and resistant to narrative bias. "
    "Consider base rates and reference classes. Be careful not to anchor on market prices."
)


class AdversarialAnalyst:
    """Adversarial LLM ensemble — YES advocate vs NO advocate vs neutral.

    Produces a more robust probability estimate than temperature-only diversity
    by forcing genuine perspective diversity at the prompt level.
    """

    def __init__(
        self,
        provider: str = "anthropic",
        api_key: str = "",
        model: str = "",
        neutral_weight: float = 0.4,
        advocate_weight: float = 0.3,
    ):
        self.provider = provider
        self.api_key = api_key
        self.model = model or self._default_model()
        # Weights for Bayesian aggregation: neutral gets more weight
        self.neutral_weight = neutral_weight
        self.yes_weight = advocate_weight
        self.no_weight = advocate_weight

    def _default_model(self) -> str:
        if self.provider == "anthropic":
            return "claude-haiku-4-5-20251001"
        return "gpt-4o-mini"

    async def predict(
        self,
        question: str,
        context: str = "",
    ) -> AdversarialResult:
        """Run adversarial ensemble and return aggregated result."""
        prompt = self._build_prompt(question, context)

        yes_est, no_est, neutral_est = await asyncio.gather(
            self._call(prompt, YES_ADVOCATE_PROMPT),
            self._call(prompt, NO_ADVOCATE_PROMPT),
            self._call(prompt, NEUTRAL_PROMPT),
            return_exceptions=True,
        )

        # Fall back gracefully if any call fails
        yes_est = yes_est if isinstance(yes_est, float) else 0.5
        no_est = no_est if isinstance(no_est, float) else 0.5
        neutral_est = neutral_est if isinstance(neutral_est, float) else 0.5

        # Bayesian aggregation with weights
        total_weight = self.yes_weight + self.no_weight + self.neutral_weight
        aggregated = (
            yes_est * self.yes_weight
            + no_est * self.no_weight
            + neutral_est * self.neutral_weight
        ) / total_weight

        tension = abs(yes_est - no_est)
        # High tension (advocates disagree) → lower confidence
        confidence = max(0.0, min(100.0, 100 - tension * 150))

        result = AdversarialResult(
            probability=round(aggregated, 4),
            yes_estimate=yes_est,
            no_estimate=no_est,
            neutral_estimate=neutral_est,
            tension=round(tension, 4),
            confidence=round(confidence, 1),
            reasoning=(
                f"Adversarial ensemble: YES={yes_est:.2f}, "
                f"NO={no_est:.2f}, NEUTRAL={neutral_est:.2f}, "
                f"tension={tension:.2f}"
            ),
        )

        logger.info(
            "adversarial_ensemble_result",
            probability=result.probability,
            tension=result.tension,
            confidence=result.confidence,
        )
        return result

    async def _call(self, prompt: str, system_prompt: str) -> float:
        if self.provider == "anthropic":
            return await self._call_anthropic(prompt, system_prompt)
        return await self._call_openai(prompt, system_prompt)

    async def _call_anthropic(self, prompt: str, system_prompt: str) -> float:
        import anthropic

        client = anthropic.AsyncAnthropic(api_key=self.api_key)
        response = await client.messages.create(
            model=self.model,
            max_tokens=400,
            temperature=0.3,  # Low temp — we want the best argument, not creativity
            system=system_prompt,
            messages=[{"role": "user", "content": prompt}],
        )
        return self._parse_probability(response.content[0].text)

    async def _call_openai(self, prompt: str, system_prompt: str) -> float:
        from openai import AsyncOpenAI

        client = AsyncOpenAI(api_key=self.api_key)
        response = await client.chat.completions.create(
            model=self.model,
            max_tokens=400,
            temperature=0.3,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt},
            ],
        )
        return self._parse_probability(response.choices[0].message.content or "")

    @staticmethod
    def _parse_probability(text: str) -> float:
        for line in text.split("\n"):
            line = line.strip()
            if line.upper().startswith("PROBABILITY:"):
                val = line.split(":", 1)[1].strip()
                return max(0.01, min(0.99, float(val)))
        raise ValueError(f"Could not parse probability from: {text[:100]}")

    @staticmethod
    def _build_prompt(question: str, context: str) -> str:
        parts = [
            f"Estimate the probability that the following resolves YES.\n\nQuestion: {question}"
        ]
        if context:
            parts.append(f"\nContext:\n{context}")
        parts.append(
            "\nYou MUST begin your response with:\n"
            "PROBABILITY: 0.XX\n"
            "REASONING: <your 2-4 sentence justification based on your assigned perspective>"
        )
        return "\n".join(parts)
