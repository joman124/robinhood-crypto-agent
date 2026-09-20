from __future__ import annotations

import pytest

from robinhood_crypto_agent.backtest.costs import CostModel


def test_buy_fills_worse_than_quoted_price():
    model = CostModel(fee_bps=10, slippage_bps=5, spread_bps=5)
    fill = model.apply(100.0, side_is_buy=True)
    assert fill > 100.0
    assert fill == pytest.approx(100.0 * 1.002)


def test_sell_fills_worse_than_quoted_price():
    model = CostModel(fee_bps=10, slippage_bps=5, spread_bps=5)
    fill = model.apply(100.0, side_is_buy=False)
    assert fill < 100.0
    assert fill == pytest.approx(100.0 * 0.998)


def test_zero_cost_model_is_a_noop():
    model = CostModel(fee_bps=0, slippage_bps=0, spread_bps=0)
    assert model.apply(100.0, True) == pytest.approx(100.0)
    assert model.apply(100.0, False) == pytest.approx(100.0)


def test_total_cost_bps_sums_components():
    model = CostModel(fee_bps=10, slippage_bps=5, spread_bps=5)
    assert model.total_cost_bps == 20
