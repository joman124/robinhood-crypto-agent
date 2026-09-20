from __future__ import annotations

from datetime import date

from robinhood_crypto_agent.backtest.costs import CostModel
from robinhood_crypto_agent.backtest.engine import run_backtest, run_single_pass
from robinhood_crypto_agent.models import Regime, Side, Signal
from robinhood_crypto_agent.risk.limits import RiskLimits
from tests.fixtures.helpers import make_trending_ohlcv


class AlwaysBuyStrategy:
    """A deterministic fake strategy for engine-level tests - the real
    composite/signal-source logic is tested separately (test_composite.py,
    test_signals.py)."""

    def generate_signal(self, snapshot):
        return Signal(
            symbol=snapshot.symbol,
            regime=Regime.TRENDING,
            composite_value=0.9,
            composite_confidence=0.9,
            side=Side.BUY,
        )


def _risk_limits(**overrides) -> RiskLimits:
    kwargs = dict(
        max_position_usd=100.0,
        max_daily_loss_usd=900.0,
        max_daily_exposure_usd=2000.0,
        max_open_positions=2,
        price_drift_tolerance_pct=1.0,
        allowed_symbols=["BTC", "ETH"],
    )
    kwargs.update(overrides)
    return RiskLimits(**kwargs)


def _candles() -> dict:
    btc = make_trending_ohlcv(n=120, daily_return=0.01, noise=0.0, seed=1)
    eth = make_trending_ohlcv(n=120, daily_return=0.01, noise=0.0, seed=2)
    btc.index = btc.index.date
    eth.index = eth.index.date
    return {"BTC": btc, "ETH": eth}


def test_single_pass_opens_and_closes_positions_on_holding_period():
    equity_curve, trades, turnover = run_single_pass(
        candles_by_symbol=_candles(),
        strategy=AlwaysBuyStrategy(),
        risk_limits=_risk_limits(max_open_positions=1),
        fear_greed=None,
        initial_cash=10_000.0,
        cost_model=CostModel(),
        holding_period_days=5,
    )
    assert len(trades) > 0
    assert all(t.exit_reason == "holding_period" for t in trades)
    assert turnover > 0
    assert len(equity_curve) > 0


def test_never_exceeds_max_open_positions():
    _, trades, _ = run_single_pass(
        candles_by_symbol=_candles(),
        strategy=AlwaysBuyStrategy(),
        risk_limits=_risk_limits(max_open_positions=1),
        fear_greed=None,
        initial_cash=10_000.0,
        cost_model=CostModel(),
        holding_period_days=5,
    )
    # With max_open_positions=1, BTC and ETH can never both be open at once -
    # every closed trade must fully exit before the next entry, so no two
    # trades should overlap in time.
    trades_sorted = sorted(trades, key=lambda t: t.entry_date)
    for earlier, later in zip(trades_sorted, trades_sorted[1:]):
        assert earlier.exit_date <= later.entry_date


def test_costs_reduce_final_equity_versus_zero_cost():
    kwargs = dict(
        candles_by_symbol=_candles(),
        strategy=AlwaysBuyStrategy(),
        risk_limits=_risk_limits(max_open_positions=1),
        fear_greed=None,
        initial_cash=10_000.0,
        holding_period_days=5,
    )
    zero_cost_curve, _, _ = run_single_pass(cost_model=CostModel(0, 0, 0), **kwargs)
    real_cost_curve, _, _ = run_single_pass(cost_model=CostModel(50, 50, 50), **kwargs)

    assert real_cost_curve.iloc[-1] <= zero_cost_curve.iloc[-1]


def test_run_backtest_produces_folds_and_aggregate_metrics():
    candles = _candles()
    start = date(2023, 4, 1)  # well past the 60-day warm-up
    end = date(2023, 4, 30)

    result = run_backtest(
        candles_by_symbol=candles,
        strategy=AlwaysBuyStrategy(),
        risk_limits=_risk_limits(),
        start=start,
        end=end,
        num_folds=2,
    )
    assert len(result.folds) == 2
    for fold in result.folds:
        assert fold.start >= start
        assert fold.end <= end
    assert result.aggregate_metrics.num_trades == sum(f.metrics.num_trades for f in result.folds)


def test_run_backtest_rejects_end_before_start():
    import pytest

    with pytest.raises(ValueError):
        run_backtest(
            candles_by_symbol=_candles(),
            strategy=AlwaysBuyStrategy(),
            risk_limits=_risk_limits(),
            start=date(2023, 4, 30),
            end=date(2023, 4, 1),
        )
