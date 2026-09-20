from __future__ import annotations

import pandas as pd

from robinhood_crypto_agent.models import Signal
from robinhood_crypto_agent.risk.limits import RiskLimits
from robinhood_crypto_agent.strategy.indicators import atr

# A "typical" realized volatility (ATR as a fraction of price) to size 1.0x
# against. Symbols realizing less volatility than this get sized up (within
# limits); symbols realizing more get sized down.
DEFAULT_TARGET_VOLATILITY_PCT = 0.02


def size_position(
    signal: Signal,
    candles: pd.DataFrame,
    reference_price: float,
    risk_limits: RiskLimits,
    atr_period: int = 14,
    target_volatility_pct: float = DEFAULT_TARGET_VOLATILITY_PCT,
) -> tuple[float, float]:
    """Return (notional_usd, quantity) sized from `signal`.

    Scales with signal confidence and inversely with realized volatility
    (ATR relative to price) - higher-conviction, lower-volatility setups get
    more size. Always clamped to risk_limits.max_position_usd; sizing only
    decides how big to *propose* - the risk engine independently and
    unconditionally decides whether to *allow* it (see risk/engine.py).
    """
    base = risk_limits.max_position_usd

    atr_series = atr(candles, period=atr_period)
    latest_atr = atr_series.iloc[-1] if len(atr_series) else float("nan")

    if pd.isna(latest_atr) or reference_price <= 0:
        vol_scale = 1.0
    else:
        realized_vol_pct = latest_atr / reference_price
        if realized_vol_pct <= 0:
            vol_scale = 1.0
        else:
            vol_scale = target_volatility_pct / realized_vol_pct
            # Volatility scaling alone shouldn't blow past the cap or shrink
            # a valid signal to near-zero; confidence and the hard clamp
            # below do the rest of the work.
            vol_scale = max(0.25, min(1.5, vol_scale))

    confidence_scale = max(0.0, min(1.0, signal.composite_confidence))

    notional_usd = base * vol_scale * confidence_scale
    notional_usd = max(0.0, min(base, notional_usd))

    quantity = (notional_usd / reference_price) if reference_price > 0 else 0.0
    return notional_usd, quantity
