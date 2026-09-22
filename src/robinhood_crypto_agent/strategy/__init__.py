"""Signal generation: indicators in, a bounded opinion out."""

from .base import SignalContext, SignalSource
from .composite import CompositeStrategy
from .regime import detect_regime
from .signals import (
    BreakoutSignal,
    MeanReversionSignal,
    MomentumSignal,
    NewsSignal,
    TrendSignal,
    default_signal_sources,
)

__all__ = [
    "SignalContext",
    "SignalSource",
    "CompositeStrategy",
    "detect_regime",
    "BreakoutSignal",
    "MeanReversionSignal",
    "MomentumSignal",
    "NewsSignal",
    "TrendSignal",
    "default_signal_sources",
]
