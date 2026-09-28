"""The trading rules.

The agent trades the split (``strategy.split``): the long-term sleeve
(``strategy.hodl``) and the breakout (``strategy.breakout``). The retired trend
ladder (``strategy.ladder``) stays for ``rhca backtest``'s comparison rows.
"""

from .ladder import (
    DEFAULT_STEPS,
    MODE_ANCHOR,
    MODE_LOT,
    MODES,
    REASON_DIP,
    REASON_TAKE_PROFIT,
    REASON_TREND_EXIT,
    HeldLot,
    Ladder,
    Order,
    above_trend,
    describe_steps,
    flat_anchor,
    parse_steps,
    trend_average,
    trend_bars,
)

__all__ = [
    "DEFAULT_STEPS",
    "MODE_ANCHOR",
    "MODE_LOT",
    "MODES",
    "REASON_DIP",
    "REASON_TAKE_PROFIT",
    "REASON_TREND_EXIT",
    "HeldLot",
    "Ladder",
    "Order",
    "above_trend",
    "describe_steps",
    "flat_anchor",
    "parse_steps",
    "trend_average",
    "trend_bars",
]
