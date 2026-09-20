from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass
class BacktestMetrics:
    total_return_pct: float
    sharpe: float
    sortino: float
    max_drawdown_pct: float
    win_rate: float
    avg_trade_pnl_usd: float
    num_trades: int
    turnover_usd: float


def compute_metrics(
    equity_curve: pd.Series,
    trade_pnls: list[float],
    turnover_usd: float,
    periods_per_year: int = 365,
) -> BacktestMetrics:
    """All costs must already be baked into `equity_curve`/`trade_pnls` by
    the caller (see backtest/engine.py + costs.py) - this function only
    computes statistics, it never adjusts for costs itself."""
    if len(equity_curve) > 1:
        returns = equity_curve.pct_change().dropna()
        total_return_pct = (equity_curve.iloc[-1] / equity_curve.iloc[0] - 1) * 100
    else:
        returns = pd.Series(dtype=float)
        total_return_pct = 0.0

    if len(returns) and returns.std(ddof=0) > 0:
        sharpe = float(returns.mean() / returns.std(ddof=0) * np.sqrt(periods_per_year))
    else:
        sharpe = 0.0

    downside = returns[returns < 0]
    if len(downside) and downside.std(ddof=0) > 0:
        sortino = float(returns.mean() / downside.std(ddof=0) * np.sqrt(periods_per_year))
    else:
        sortino = 0.0

    if len(equity_curve):
        running_max = equity_curve.cummax()
        drawdown = (equity_curve - running_max) / running_max.replace(0, np.nan)
        max_dd = drawdown.min()
        max_drawdown_pct = float(max_dd * 100) if pd.notna(max_dd) else 0.0
    else:
        max_drawdown_pct = 0.0

    wins = [p for p in trade_pnls if p > 0]
    win_rate = (len(wins) / len(trade_pnls)) if trade_pnls else 0.0
    avg_trade_pnl_usd = (sum(trade_pnls) / len(trade_pnls)) if trade_pnls else 0.0

    return BacktestMetrics(
        total_return_pct=float(total_return_pct),
        sharpe=sharpe,
        sortino=sortino,
        max_drawdown_pct=max_drawdown_pct,
        win_rate=win_rate,
        avg_trade_pnl_usd=avg_trade_pnl_usd,
        num_trades=len(trade_pnls),
        turnover_usd=turnover_usd,
    )
