"""xAI/Grok adapter for LLMAnalyst — drop-in replacement using the xAI API.

xAI's API is OpenAI-compatible, so this extends LLMAnalyst with a
``"xai"`` provider that routes to ``api.x.ai``.

Setup::

    # .env
    XAI_API_KEY=xai-...

Usage::

    from agents.llm_analyst_xai import XAIAnalyst

    analyst = XAIAnalyst()                      # reads XAI_API_KEY from env
    result = await analyst.predict(
        question="Will ETH flip BTC in market cap this year?",
        context="ETH/BTC ratio currently 0.055, down from 0.08 ATH",
    )
    print(result.probability, result.confidence)

Or use it inside a MultiFactorStrategy::

    from agents.llm_analyst_xai import XAIAnalyst
    from scoring.crypto_sentiment import CryptoSentimentScorer

    analyst = XAIAnalyst()
    scorer  = CryptoSentimentScorer(analyst=analyst)
    score   = await scorer.score("ETH")         # 0-100

Notes:

- ``grok-3-mini`` is the default model — fast, cheap, good calibration.
  Use ``grok-3`` for higher accuracy at higher cost.
- The xAI API is otherwise identical to the OpenAI SDK (same base_url swap).
- API key is loaded from the ``XAI_API_KEY`` env variable (never pass it inline).
"""

from __future__ import annotations

import os

from agents.llm_analyst import LLMAnalyst, DEFAULT_TEMPS

XAI_BASE_URL = "https://api.x.ai/v1"
XAI_DEFAULT_MODEL = "grok-3-mini"


class XAIAnalyst(LLMAnalyst):
    """LLMAnalyst backed by xAI's Grok models.

    Inherits the full ensemble + caching behaviour from ``LLMAnalyst``.
    The only difference is the provider, base_url, and default model.

    Args:
        model: xAI model name. Defaults to ``grok-3-mini``.
               Use ``grok-3`` for max accuracy.
        temperatures: Ensemble temperature spread. Default (0.3, 0.5, 0.7).
        system_prompt: Override the default superforecaster prompt.

    The ``XAI_API_KEY`` environment variable must be set.
    """

    def __init__(
        self,
        model: str = XAI_DEFAULT_MODEL,
        temperatures: tuple[float, ...] = DEFAULT_TEMPS,
        system_prompt: str = "",
    ):
        api_key = os.environ.get("XAI_API_KEY", "")
        if not api_key:
            raise EnvironmentError(
                "XAI_API_KEY is not set. Add it to your .env:\n  XAI_API_KEY=xai-..."
            )

        super().__init__(
            provider="openai",          # xAI is OpenAI-compatible
            api_key=api_key,
            model=model,
            temperatures=temperatures,
            system_prompt=system_prompt,
        )
        self._xai_base_url = XAI_BASE_URL

    async def _call_openai(self, prompt: str, temperature: float) -> float:
        """Override to point the OpenAI client at api.x.ai."""
        from openai import AsyncOpenAI

        client = AsyncOpenAI(api_key=self.api_key, base_url=self._xai_base_url)
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
