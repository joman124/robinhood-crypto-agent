"""Regime detection.

Which regime a symbol is in decides how the signals get weighted: trend
following and mean reversion are close to opposites, so blending them with
fixed weights averages out to noise. ADX answers "is there a trend?" without
saying which way, which is exactly the question being asked.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..indicators import latest
from ..models import Regime
from .base import SignalContext


@dataclass(frozen=True)
class RegimeRead:
    regime: Regime
    adx: float | None
    plus_di: float | None
    minus_di: float | None
    rationale: str


def detect_regime(context: SignalContext) -> RegimeRead:
    """Classify the symbol as trending, ranging, or unknown.

    ``UNKNOWN`` is returned when ADX has not warmed up, and it is not a synonym
    for ranging: it means the regime question has no answer yet, and the
    composite falls back to equal weights rather than committing to either
    playbook.
    """
    adx_series, plus_series, minus_series = context.adx
    adx_value = latest(adx_series)
    plus_di = latest(plus_series)
    minus_di = latest(minus_series)

    if adx_value is None:
        needed = context.config.adx_period * 2
        return RegimeRead(
            regime=Regime.UNKNOWN,
            adx=None,
            plus_di=plus_di,
            minus_di=minus_di,
            rationale=(
                f"ADX needs {needed} bars to warm up; only {len(context.candles)} available"
            ),
        )

    threshold = float(context.config.adx_trend_threshold)
    if adx_value >= threshold:
        bias = "up" if (plus_di or 0) >= (minus_di or 0) else "down"
        return RegimeRead(
            regime=Regime.TRENDING,
            adx=adx_value,
            plus_di=plus_di,
            minus_di=minus_di,
            rationale=f"ADX {adx_value:.1f} >= {threshold:.0f}, directional bias {bias}",
        )

    return RegimeRead(
        regime=Regime.RANGING,
        adx=adx_value,
        plus_di=plus_di,
        minus_di=minus_di,
        rationale=f"ADX {adx_value:.1f} < {threshold:.0f}, no dominant trend",
    )
