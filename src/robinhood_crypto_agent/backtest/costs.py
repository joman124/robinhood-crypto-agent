from __future__ import annotations

from dataclasses import dataclass


@dataclass
class CostModel:
    """Applied to every simulated fill. Defaults are conservative
    placeholders, not confirmed Robinhood crypto figures - override with
    real fee/spread numbers once confirmed, and always run a backtest with
    at least these defaults, never zero costs (a zero-cost backtest
    overstates edge)."""

    fee_bps: float = 10.0  # platform fee, basis points of notional
    slippage_bps: float = 5.0  # assumed market-impact/slippage, bps of notional
    spread_bps: float = 5.0  # assumed bid-ask spread cost, bps of notional

    @property
    def total_cost_bps(self) -> float:
        return self.fee_bps + self.slippage_bps + self.spread_bps

    def apply(self, price: float, side_is_buy: bool) -> float:
        """Effective fill price after costs: buys fill worse (higher),
        sells fill worse (lower)."""
        cost_fraction = self.total_cost_bps / 10_000
        return price * (1 + cost_fraction) if side_is_buy else price * (1 - cost_fraction)
