"""Multi-factor signal generation strategy.

Combines multiple data sources (whale consensus, AI prediction, price
momentum) into a single trading signal using the CompositeScorer.

Usage::

    strategy = MultiFactorStrategy()
    strategy.update_whale_score(symbol, 0.75)
    strategy.update_ai_score(symbol, 0.80)
"""

from __future__ import annotations


from strategies.base import Strategy, Signal, Position, Candle, Direction
from scoring.confidence import CompositeScorer, SubScore


class MultiFactorStrategy(Strategy):
    """Generates signals from multiple scored factors.

    Factors:
    - Whale consensus: How many large traders agree on direction
    - AI probability: LLM-estimated edge vs market price
    - Price momentum: Recent price trend strength

    Each factor is scored 0-100 and combined with configurable weights.
    """

    def __init__(
        self,
        whale_weight: float = 0.35,
        ai_weight: float = 0.35,
        momentum_weight: float = 0.30,
        min_score: float = 60.0,
        lookback: int = 20,
    ):
        super().__init__()
        self.min_score = min_score
        self.lookback = lookback

        # External scores set by data pipeline
        self._whale_scores: dict[str, float] = {}  # symbol → 0-100
        self._ai_scores: dict[str, float] = {}  # symbol → 0-100
        self._ai_directions: dict[str, Direction] = {}

        self._candle_history: list[Candle] = []
        self._current_candle: Candle | None = None

        self._scorer = CompositeScorer()
        self._scorer.register(SubScore("whale", whale_weight, self._score_whale))
        self._scorer.register(SubScore("ai", ai_weight, self._score_ai))
        self._scorer.register(SubScore("momentum", momentum_weight, self._score_momentum))

    def update_whale_score(self, symbol: str, score: float) -> None:
        """Set whale consensus score (0-100) for a symbol."""
        self._whale_scores[symbol] = max(0, min(100, score))

    def update_ai_score(self, symbol: str, score: float, direction: Direction = Direction.LONG) -> None:
        """Set AI prediction score (0-100) and suggested direction."""
        self._ai_scores[symbol] = max(0, min(100, score))
        self._ai_directions[symbol] = direction

    async def on_data(self, candle: Candle) -> None:
        self._current_candle = candle
        self._candle_history.append(candle)
        if len(self._candle_history) > self.lookback * 2:
            self._candle_history = self._candle_history[-self.lookback * 2 :]

    async def should_enter(self) -> Signal | None:
        if self._current_candle is None:
            return None

        symbol = self._current_candle.asset
        result = self._scorer.score(symbol)

        if result.total < self.min_score:
            return None

        # Direction from AI if available, else from momentum
        direction = self._ai_directions.get(symbol, self._momentum_direction())

        return Signal(
            asset=symbol,
            direction=direction,
            confidence=result.total / 100,
            entry_price=self._current_candle.close,
            source="multi_factor",
        )

    async def should_exit(self, position: Position) -> bool:
        if self._current_candle is None:
            return False
        pnl_pct = (
            (self._current_candle.close - position.entry_price) / position.entry_price
            if position.direction == Direction.LONG
            else (position.entry_price - self._current_candle.close) / position.entry_price
        )
        return pnl_pct <= -0.03 or pnl_pct >= 0.06

    async def on_fill(self, position: Position) -> None:
        pass

    async def on_close(self, position: Position, pnl: float) -> None:
        pass

    # -- Sub-score functions --

    def _score_whale(self, symbol: str) -> float:
        return self._whale_scores.get(symbol, 50.0)

    def _score_ai(self, symbol: str) -> float:
        return self._ai_scores.get(symbol, 50.0)

    def _score_momentum(self, symbol: str) -> float:
        """Score based on recent price momentum (0-100)."""
        if len(self._candle_history) < 2:
            return 50.0

        recent = [c.close for c in self._candle_history[-self.lookback :]]
        if len(recent) < 2 or recent[0] == 0:
            return 50.0

        pct_change = (recent[-1] - recent[0]) / recent[0]
        # Map: -10% → 10, 0% → 50, +10% → 90
        score = 50 + pct_change * 400
        return max(0, min(100, score))

    def _momentum_direction(self) -> Direction:
        if len(self._candle_history) < 2:
            return Direction.LONG
        return Direction.LONG if self._candle_history[-1].close >= self._candle_history[-2].close else Direction.SHORT
