from __future__ import annotations

import pandas as pd
import pytest

from robinhood_crypto_agent.backtest.metrics import compute_metrics


def test_flat_equity_curve_has_zero_return_and_sharpe():
    curve = pd.Series([100.0] * 10)
    metrics = compute_metrics(curve, [], turnover_usd=0.0)
    assert metrics.total_return_pct == pytest.approx(0.0)
    assert metrics.sharpe == 0.0


def test_steadily_rising_curve_has_positive_return_and_sharpe():
    curve = pd.Series([100 * 1.01**i for i in range(30)])
    metrics = compute_metrics(curve, [10.0, 20.0], turnover_usd=1000.0)
    assert metrics.total_return_pct > 0
    assert metrics.sharpe > 0
    assert metrics.win_rate == 1.0
    assert metrics.avg_trade_pnl_usd == pytest.approx(15.0)
    assert metrics.num_trades == 2
    assert metrics.turnover_usd == 1000.0


def test_drawdown_is_negative_after_a_peak_and_decline():
    curve = pd.Series([100, 110, 120, 90, 100])
    metrics = compute_metrics(curve, [], turnover_usd=0.0)
    assert metrics.max_drawdown_pct < 0
    assert metrics.max_drawdown_pct == pytest.approx((90 - 120) / 120 * 100)


def test_win_rate_reflects_mixed_trades():
    metrics = compute_metrics(pd.Series([100, 101]), [10.0, -5.0, 3.0, -1.0], turnover_usd=0.0)
    assert metrics.win_rate == pytest.approx(0.5)


def test_empty_equity_curve_does_not_crash():
    metrics = compute_metrics(pd.Series(dtype=float), [], turnover_usd=0.0)
    assert metrics.total_return_pct == 0.0
    assert metrics.sharpe == 0.0
    assert metrics.num_trades == 0
