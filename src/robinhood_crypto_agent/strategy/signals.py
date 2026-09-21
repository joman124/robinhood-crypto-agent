"""The four signal sources.

Each returns a score in [-1, 1] and a confidence in [0, 1], or ``None`` when
history is too thin to have an opinion. Scores are deliberately *normalized by
volatility* wherever a raw price difference would otherwise be compared across
assets -- a $300 MACD histogram means something very different on BTC than on
DOGE, and an un-normalized score would rank symbols by price level rather than
by signal strength.
"""

from __future__ import annotations

from .. import indicators
from ..indicators import clamp, latest
from ..models import Direction, Signal
from .base import SignalContext


def _direction(score: float, deadband: float = 0.05) -> Direction:
    if score > deadband:
        return Direction.LONG
    if score < -deadband:
        return Direction.SHORT
    return Direction.FLAT


class TrendSignal:
    """Trend following: MACD histogram, normalized by ATR, plus MA alignment.

    Dividing the histogram by ATR puts the reading in units of "typical bar
    range", which is comparable across symbols and across volatility regimes.
    """

    name = "trend"

    def evaluate(self, context: SignalContext) -> Signal | None:
        needed = context.config.slow_ma + context.config.signal_ma
        if len(context.candles) < needed:
            return None

        _, _, histogram = context.macd
        hist = latest(histogram)
        atr_value = latest(context.atr)
        if hist is None or atr_value is None or atr_value <= 0:
            return None

        # One ATR of MACD histogram is a strong trend; scale so that saturates.
        normalized = clamp(hist / atr_value)

        fast = latest(indicators.sma(context.closes, context.config.fast_ma))
        slow = latest(indicators.sma(context.closes, context.config.slow_ma))
        alignment = 0.0
        if fast is not None and slow is not None and slow > 0:
            alignment = clamp((fast - slow) / slow * 20.0)

        score = clamp(0.65 * normalized + 0.35 * alignment)
        confidence = context.sample_quality * context.history_factor(needed)

        return Signal(
            name=self.name,
            symbol=context.symbol,
            score=score,
            confidence=confidence,
            direction=_direction(score),
            rationale=(
                f"MACD histogram {hist:+.4f} ({normalized:+.2f} ATR), "
                f"SMA{context.config.fast_ma} vs SMA{context.config.slow_ma} {alignment:+.2f}"
            ),
            detail={
                "macd_histogram": hist,
                "atr": atr_value,
                "normalized_histogram": normalized,
                "ma_alignment": alignment,
            },
        )


class MomentumSignal:
    """Momentum: the z-score of recent price against its own recent range.

    A z-score is self-normalizing, so this reads the same way on any symbol.
    It is capped at +/-2 sigma before scaling because beyond that the move is
    more likely a data artifact or a liquidation cascade than a tradable trend.
    """

    name = "momentum"

    def evaluate(self, context: SignalContext) -> Signal | None:
        lookback = context.config.breakout_lookback
        if len(context.candles) < lookback + 1:
            return None

        z = indicators.zscore(context.closes, lookback)
        if z is None:
            return None

        score = clamp(z / 2.0)
        confidence = context.sample_quality * context.history_factor(lookback + 1)

        return Signal(
            name=self.name,
            symbol=context.symbol,
            score=score,
            confidence=confidence,
            direction=_direction(score),
            rationale=f"close is {z:+.2f} sigma from its {lookback}-bar mean",
            detail={"zscore": z, "lookback": lookback},
        )


class MeanReversionSignal:
    """Mean reversion: RSI plus position within the Bollinger bands.

    Contrarian by construction -- a low RSI at the lower band is a *long*. The
    composite down-weights this in a trending regime, because "oversold" in a
    downtrend is a description, not an entry.
    """

    name = "mean_reversion"

    def evaluate(self, context: SignalContext) -> Signal | None:
        needed = max(context.config.rsi_period + 1, context.config.breakout_lookback)
        if len(context.candles) < needed:
            return None

        rsi_value = latest(context.rsi)
        if rsi_value is None:
            return None

        # RSI 30 -> +1 (buy), RSI 70 -> -1 (sell), RSI 50 -> 0.
        rsi_score = clamp((50.0 - rsi_value) / 20.0)

        upper, middle, lower = context.bollinger
        band_score = 0.0
        band_position: float | None = None
        upper_value, middle_value, lower_value = latest(upper), latest(middle), latest(lower)
        close = context.closes[-1]
        if None not in (upper_value, middle_value, lower_value):
            width = upper_value - lower_value  # type: ignore[operator]
            if width > 0:
                # 0 at the lower band, 1 at the upper band.
                band_position = (close - lower_value) / width  # type: ignore[operator]
                band_score = clamp((0.5 - band_position) * 2.0)

        score = clamp(0.6 * rsi_score + 0.4 * band_score)
        confidence = context.sample_quality * context.history_factor(needed)

        return Signal(
            name=self.name,
            symbol=context.symbol,
            score=score,
            confidence=confidence,
            direction=_direction(score),
            rationale=(
                f"RSI {rsi_value:.1f}"
                + (
                    f", {band_position:.0%} of the way up the Bollinger band"
                    if band_position is not None
                    else ", Bollinger bands unavailable"
                )
            ),
            detail={
                "rsi": rsi_value,
                "rsi_score": rsi_score,
                "band_position": band_position,
                "band_score": band_score,
            },
        )


class BreakoutSignal:
    """Donchian breakout: close beyond the prior N-bar high or low.

    The channel deliberately excludes the current bar (see
    :func:`indicators.donchian`), so a bar that makes a new high can actually
    register as a breakout.
    """

    name = "breakout"

    def evaluate(self, context: SignalContext) -> Signal | None:
        lookback = context.config.breakout_lookback
        if len(context.candles) < lookback + 1:
            return None

        upper, lower = context.donchian
        upper_value, lower_value = latest(upper), latest(lower)
        atr_value = latest(context.atr)
        if upper_value is None or lower_value is None:
            return None

        close = context.closes[-1]
        score = 0.0
        breach = 0.0
        if close > upper_value:
            breach = close - upper_value
            score = clamp(breach / atr_value) if atr_value and atr_value > 0 else 0.5
        elif close < lower_value:
            breach = close - lower_value
            score = -clamp(abs(breach) / atr_value) if atr_value and atr_value > 0 else -0.5

        confidence = context.sample_quality * context.history_factor(lookback + 1)
        if score == 0.0:
            # Inside the channel is a real, informative "no breakout" reading,
            # but it should not dilute the blend as if it were a measurement,
            # so it is reported with reduced confidence.
            confidence *= 0.5

        return Signal(
            name=self.name,
            symbol=context.symbol,
            score=score,
            confidence=confidence,
            direction=_direction(score),
            rationale=(
                f"close {close:.6g} vs {lookback}-bar channel "
                f"[{lower_value:.6g}, {upper_value:.6g}]"
                + (f", breach {breach:+.6g}" if breach else ", inside the channel")
            ),
            detail={
                "channel_high": upper_value,
                "channel_low": lower_value,
                "breach": breach,
                "atr": atr_value,
            },
        )


def default_signal_sources() -> list:
    """The signal sources the agent runs, in report order."""
    return [TrendSignal(), MomentumSignal(), MeanReversionSignal(), BreakoutSignal()]
