from __future__ import annotations

import pytest

from robinhood_crypto_agent.execution.plan import build_execution_plan
from robinhood_crypto_agent.models import (
    ExecutionStyle,
    Regime,
    RiskCheckResult,
    Side,
    Signal,
    TradeProposal,
)


def _proposal(
    regime: Regime, side: Side = Side.BUY, reference_price: float = 100.0, quantity: float = 1.0
) -> TradeProposal:
    signal = Signal(
        symbol="BTC",
        regime=regime,
        composite_value=0.5,
        composite_confidence=0.8,
        side=side,
    )
    notional_usd = quantity * reference_price
    return TradeProposal(
        symbol="BTC",
        side=side,
        quantity=quantity,
        reference_price=reference_price,
        notional_usd=notional_usd,
        rationale="test",
        signal=signal,
        risk_check=RiskCheckResult(passed=True),
    )


def test_trending_produces_a_single_prompt_tranche():
    proposal = _proposal(Regime.TRENDING)
    plan = build_execution_plan(proposal)

    assert plan.style is ExecutionStyle.PROMPT
    assert len(plan.tranches) == 1
    tranche = plan.tranches[0]
    assert tranche.quantity == pytest.approx(proposal.quantity)
    assert tranche.notional_usd == pytest.approx(proposal.notional_usd)
    assert tranche.target_price == pytest.approx(proposal.reference_price)


def test_ranging_produces_multiple_staged_tranches_summing_to_the_total():
    proposal = _proposal(Regime.RANGING, quantity=3.0, reference_price=100.0)
    plan = build_execution_plan(proposal)

    assert plan.style is ExecutionStyle.STAGED
    assert len(plan.tranches) > 1
    assert sum(t.quantity for t in plan.tranches) == pytest.approx(proposal.quantity)
    assert sum(t.notional_usd for t in plan.tranches) == pytest.approx(proposal.notional_usd)


def test_ranging_buy_tranches_step_at_or_below_reference_price_in_increasing_distance():
    proposal = _proposal(Regime.RANGING, side=Side.BUY, reference_price=100.0)
    plan = build_execution_plan(proposal)

    prices = [t.target_price for t in plan.tranches]
    assert prices[0] == pytest.approx(100.0)
    for price in prices:
        assert price <= 100.0
    # Strictly more favorable (lower) with each successive tranche.
    assert prices == sorted(prices, reverse=True)
    assert len(set(prices)) == len(prices)


def test_ranging_sell_tranches_step_at_or_above_reference_price_in_increasing_distance():
    proposal = _proposal(Regime.RANGING, side=Side.SELL, reference_price=100.0)
    plan = build_execution_plan(proposal)

    prices = [t.target_price for t in plan.tranches]
    assert prices[0] == pytest.approx(100.0)
    for price in prices:
        assert price >= 100.0
    assert prices == sorted(prices)
    assert len(set(prices)) == len(prices)


def test_tranches_are_sequentially_numbered_from_zero():
    proposal = _proposal(Regime.RANGING)
    plan = build_execution_plan(proposal)
    assert [t.sequence for t in plan.tranches] == list(range(len(plan.tranches)))
