"""Backtesting engine for strategy evaluation.

Iterates historical data, calls strategy methods, tracks paper positions,
and computes performance metrics.

Usage::

    from strategies.examples.funding_arb import FundingArbStrategy
    runner = BacktestRunner(FundingArbStrategy(threshold=0.01))
    result = runner.run(candles)
    print(result.summary)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import math

from strategies.base import Strategy, Position, Candle, Direction


@dataclass
class TradeRecord:
    """A completed backtest trade."""

    symbol: str
    direction: str
    entry_price: float
    exit_price: float
    size: float
    pnl: float
    pnl_pct: float
    entry_time: datetime
    exit_time: datetime


@dataclass
class BacktestResult:
    """Results from a backtest run."""

    trades: list[TradeRecord] = field(default_factory=list)
    equity_curve: list[float] = field(default_factory=list)

    @property
    def total_trades(self) -> int:
        return len(self.trades)

    @property
    def winners(self) -> int:
        return sum(1 for t in self.trades if t.pnl > 0)

    @property
    def losers(self) -> int:
        return sum(1 for t in self.trades if t.pnl < 0)

    @property
    def win_rate(self) -> float:
        return self.winners / self.total_trades if self.total_trades else 0.0

    @property
    def total_pnl(self) -> float:
        return sum(t.pnl for t in self.trades)

    @property
    def max_drawdown(self) -> float:
        """Maximum drawdown as a fraction (e.g. 0.15 = 15%)."""
        if not self.equity_curve:
            return 0.0
        peak = self.equity_curve[0]
        max_dd = 0.0
        for eq in self.equity_curve:
            if eq > peak:
                peak = eq
            dd = (peak - eq) / peak if peak > 0 else 0
            max_dd = max(max_dd, dd)
        return max_dd

    @property
    def sharpe_ratio(self) -> float:
        """Annualized Sharpe ratio (assumes daily returns)."""
        if len(self.equity_curve) < 2:
            return 0.0
        returns = []
        for i in range(1, len(self.equity_curve)):
            prev = self.equity_curve[i - 1]
            if prev > 0:
                returns.append((self.equity_curve[i] - prev) / prev)
        if not returns:
            return 0.0
        mean_r = sum(returns) / len(returns)
        std_r = (sum((r - mean_r) ** 2 for r in returns) / len(returns)) ** 0.5
        if std_r == 0:
            return 0.0
        return (mean_r / std_r) * math.sqrt(252)

    @property
    def profit_factor(self) -> float:
        """Gross profit / gross loss."""
        gross_profit = sum(t.pnl for t in self.trades if t.pnl > 0)
        gross_loss = abs(sum(t.pnl for t in self.trades if t.pnl < 0))
        if gross_loss == 0:
            return float("inf") if gross_profit > 0 else 0.0
        return gross_profit / gross_loss

    @property
    def summary(self) -> dict:
        return {
            "total_trades": self.total_trades,
            "winners": self.winners,
            "losers": self.losers,
            "win_rate": f"{self.win_rate:.1%}",
            "total_pnl": round(self.total_pnl, 2),
            "max_drawdown": f"{self.max_drawdown:.1%}",
            "sharpe_ratio": round(self.sharpe_ratio, 2),
            "profit_factor": round(self.profit_factor, 2),
        }


class BacktestRunner:
    """Run a strategy against historical candle data.

    Args:
        strategy: A Strategy instance.
        initial_capital: Starting bankroll in USD.
        position_size_pct: Fraction of equity per trade.
        commission_pct: Round-trip commission as fraction.
    """

    def __init__(
        self,
        strategy: Strategy,
        initial_capital: float = 10_000.0,
        position_size_pct: float = 0.05,
        commission_pct: float = 0.001,
    ):
        self.strategy = strategy
        self.initial_capital = initial_capital
        self.position_size_pct = position_size_pct
        self.commission_pct = commission_pct

    async def run(self, candles: list[Candle]) -> BacktestResult:
        """Execute the backtest. Returns results with all trades and metrics."""
        result = BacktestResult()
        equity = self.initial_capital
        position: Position | None = None

        for candle in candles:
            result.equity_curve.append(equity)
            await self.strategy.on_data(candle)

            # Check exit first
            if position is not None:
                if await self.strategy.should_exit(position):
                    # Close at current candle's close
                    exit_price = candle.close
                    if position.direction == Direction.LONG:
                        raw_pnl = (exit_price - position.entry_price) * position.size
                    else:
                        raw_pnl = (position.entry_price - exit_price) * position.size

                    commission = abs(position.size * exit_price * self.commission_pct)
                    net_pnl = raw_pnl - commission
                    equity += net_pnl

                    pnl_pct = net_pnl / (position.entry_price * position.size) if position.size else 0

                    result.trades.append(
                        TradeRecord(
                            symbol=candle.asset,
                            direction=position.direction.value,
                            entry_price=position.entry_price,
                            exit_price=exit_price,
                            size=position.size,
                            pnl=round(net_pnl, 2),
                            pnl_pct=round(pnl_pct, 4),
                            entry_time=position.opened_at,
                            exit_time=candle.timestamp,
                        )
                    )

                    await self.strategy.on_close(position, net_pnl)
                    position = None

            # Check entry
            if position is None:
                signal = await self.strategy.should_enter()
                if signal is not None:
                    size_usd = equity * self.position_size_pct
                    size = size_usd / candle.close if candle.close > 0 else 0

                    position = Position(
                        asset=candle.asset,
                        direction=signal.direction,
                        entry_price=candle.close,
                        size=size,
                        opened_at=candle.timestamp,
                    )
                    await self.strategy.on_fill(position)

        # Final equity
        result.equity_curve.append(equity)
        return result
