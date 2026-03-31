"""Risk management — position sizing, exposure limits, gas guards."""

from __future__ import annotations

from dataclasses import dataclass

import structlog

logger = structlog.get_logger()


@dataclass
class RiskConfig:
    """Risk parameters. Override per-bot via config."""

    max_position_size_pct: float = 0.10
    max_total_exposure_pct: float = 0.50
    max_positions: int = 10
    max_daily_loss_pct: float = 0.05
    kill_switch_pct: float = 0.20
    min_order_usd: float = 10.0


class RiskManager:
    """Core risk gate — checks every entry against limits."""

    def __init__(self, bankroll: float, config: RiskConfig | None = None):
        self.bankroll = bankroll
        self.config = config or RiskConfig()
        self.open_positions: list[dict] = []
        self.daily_pnl: float = 0.0
        self.peak_equity: float = bankroll
        self._killed = False

    @property
    def is_killed(self) -> bool:
        return self._killed

    @property
    def current_exposure(self) -> float:
        return sum(abs(p.get("size_usd", 0)) for p in self.open_positions)

    def check_entry(self, size_usd: float) -> str | None:
        """Return None if OK, or a rejection reason string."""
        if self._killed:
            return "kill_switch_active"

        if size_usd < self.config.min_order_usd:
            return f"below_minimum: ${size_usd:.2f} < ${self.config.min_order_usd}"

        max_size = self.bankroll * self.config.max_position_size_pct
        if size_usd > max_size:
            return f"position_too_large: ${size_usd:.2f} > ${max_size:.2f}"

        if len(self.open_positions) >= self.config.max_positions:
            return f"max_positions: {len(self.open_positions)} >= {self.config.max_positions}"

        max_exposure = self.bankroll * self.config.max_total_exposure_pct
        if self.current_exposure + size_usd > max_exposure:
            return f"exposure_limit: ${self.current_exposure + size_usd:.2f} > ${max_exposure:.2f}"

        max_daily_loss = self.bankroll * self.config.max_daily_loss_pct
        if self.daily_pnl < 0 and abs(self.daily_pnl) >= max_daily_loss:
            return f"daily_loss_limit: ${abs(self.daily_pnl):.2f} >= ${max_daily_loss:.2f}"

        return None

    def record_pnl(self, pnl: float) -> None:
        self.daily_pnl += pnl
        self.bankroll += pnl
        if self.bankroll > self.peak_equity:
            self.peak_equity = self.bankroll
        drawdown = (self.peak_equity - self.bankroll) / self.peak_equity
        if drawdown >= self.config.kill_switch_pct:
            self._killed = True
            logger.critical("kill_switch_triggered", drawdown=f"{drawdown:.1%}")

    def reset_daily(self) -> None:
        self.daily_pnl = 0.0


class KellySizer:
    """Half-Kelly position sizing.

    Based on the Polymarket bot's PositionSizer — uses edge and confidence
    to compute a conservative (half-Kelly) position size.
    """

    def __init__(self, max_pct: float = 0.05):
        self.max_pct = max_pct

    def size(
        self,
        edge: float,
        confidence: float,
        bankroll: float,
    ) -> float:
        """Calculate position size in USD.

        Args:
            edge: Estimated edge as fraction (e.g. 0.10 for 10%).
            confidence: 0-100 confidence score.
            bankroll: Current bankroll.

        Returns:
            Position size in USD.
        """
        # Half-Kelly: f = (edge * confidence_frac) / 2
        kelly_fraction = abs(edge) * (confidence / 100) / 2
        kelly_fraction = min(kelly_fraction, self.max_pct)
        size = kelly_fraction * bankroll
        return max(1.0, round(size, 2))


class CorrelationTracker:
    """Track exposure per correlation group.

    From the HL bot's SignalCombiner — prevents overconcentration
    in correlated assets (e.g. all memecoins, all L1 alts).

    Usage::

        tracker = CorrelationTracker(
            groups={"meme": ["DOGE", "WIF", "PEPE"], "l1": ["SOL", "AVAX", "SUI"]},
            max_group_pct=0.15,
        )
        ok = tracker.check("DOGE", 50.0, bankroll=1000.0)
    """

    def __init__(
        self,
        groups: dict[str, list[str]],
        max_group_pct: float = 0.15,
    ):
        self.groups = groups
        self.max_group_pct = max_group_pct
        # Build reverse lookup: symbol → group name
        self._symbol_to_group: dict[str, str] = {}
        for group_name, symbols in groups.items():
            for sym in symbols:
                self._symbol_to_group[sym.upper()] = group_name
        self._group_exposure: dict[str, float] = {g: 0.0 for g in groups}

    def get_group(self, symbol: str) -> str | None:
        return self._symbol_to_group.get(symbol.upper())

    def record_position(self, symbol: str, size_usd: float) -> None:
        group = self.get_group(symbol)
        if group:
            self._group_exposure[group] += abs(size_usd)

    def remove_position(self, symbol: str, size_usd: float) -> None:
        group = self.get_group(symbol)
        if group:
            self._group_exposure[group] = max(0, self._group_exposure[group] - abs(size_usd))

    def check(self, symbol: str, size_usd: float, bankroll: float) -> str | None:
        """Return None if OK, or rejection reason."""
        group = self.get_group(symbol)
        if group is None:
            return None  # Ungrouped symbols are fine
        current = self._group_exposure.get(group, 0.0)
        limit = bankroll * self.max_group_pct
        if current + abs(size_usd) > limit:
            return f"correlated_exposure: {group} ${current + abs(size_usd):.2f} > ${limit:.2f}"
        return None

    def reset(self) -> None:
        self._group_exposure = {g: 0.0 for g in self.groups}


class GasGuard:
    """Pause trading when gas is too expensive (for on-chain bots).

    From the LP bot's risk module — checks gas price and ETH balance
    before allowing transactions.
    """

    def __init__(
        self,
        ceiling_gwei: float = 50.0,
        min_eth_balance: float = 0.005,
    ):
        self.ceiling_gwei = ceiling_gwei
        self.min_eth_balance = min_eth_balance

    def check(self, gas_price_gwei: float, eth_balance: float) -> str | None:
        """Return None if OK, or a reason to pause."""
        if gas_price_gwei > self.ceiling_gwei:
            return f"gas_too_high: {gas_price_gwei:.1f} gwei > {self.ceiling_gwei} ceiling"
        if eth_balance < self.min_eth_balance:
            return f"eth_too_low: {eth_balance:.6f} < {self.min_eth_balance} minimum"
        return None
