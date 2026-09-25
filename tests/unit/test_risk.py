"""Risk engine: every rule runs, and a failure names itself."""

from datetime import timedelta
from decimal import Decimal

import pytest

from robinhood_crypto_agent.audit import DailyActivity
from robinhood_crypto_agent.config import AgentConfig, RiskLimits
from robinhood_crypto_agent.execution.kill_switch import KillSwitchState
from robinhood_crypto_agent.models import (
    CompositeView,
    Direction,
    ExecutionMode,
    OrderType,
    PairConstraints,
    Position,
    Quote,
    Regime,
    Side,
    utcnow,
)
from robinhood_crypto_agent.risk import RiskContext, RiskEngine, summarize
from robinhood_crypto_agent.sizing import SizingResult


def make_view(score=0.8, confidence=0.9):
    return CompositeView(
        symbol="BTC-USD",
        regime=Regime.TRENDING,
        score=score,
        confidence=confidence,
        direction=Direction.LONG,
        signals=[],
        weights={},
    )


def make_sizing(quantity="0.001", notional="80", side=Side.BUY):
    return SizingResult(
        symbol="BTC-USD",
        side=side,
        quantity=Decimal(quantity),
        notional=Decimal(notional),
        reference_price=Decimal("80000"),
    )


def make_activity(notional="0", pnl="0"):
    return DailyActivity(
        day=utcnow().date(),
        executed_notional=Decimal(notional),
        execution_count=0,
        realized_pnl=Decimal(pnl),
        proposal_count=0,
        symbols_traded=(),
    )


@pytest.fixture
def config(tmp_path):
    return AgentConfig(
        watchlist=("BTC-USD",), rhs_account_number="123456789", data_dir=tmp_path
    )


def make_context(config, **overrides):
    defaults = dict(
        config=config,
        quote=Quote("BTC-USD", Decimal("79900"), Decimal("80100"), Decimal("80000"), utcnow()),
        constraints=PairConstraints("BTC-USD", Decimal("0.00000001")),
        activity=make_activity(),
        kill_switch=KillSwitchState(engaged=False),
        positions={},
        portfolio_value=Decimal("10000"),
        order_type=OrderType.LIMIT,
    )
    defaults.update(overrides)
    return RiskContext(**defaults)


def rule(decision, name):
    return next(f for f in decision.findings if f.rule == name)


def test_a_clean_proposal_passes_everything(config):
    decision = RiskEngine(config).evaluate(make_view(), make_sizing(), make_context(config))
    assert decision.passed
    assert summarize(decision) == "PASS"


def test_every_rule_runs_even_after_a_failure(config):
    """The report must show all blockers, not just the first."""
    context = make_context(
        config,
        kill_switch=KillSwitchState(engaged=True, reason="manual"),
        activity=make_activity(notional="5000", pnl="-500"),
    )
    decision = RiskEngine(config).evaluate(make_view(), make_sizing(), context)
    failures = {f.rule for f in decision.blocking_failures}
    assert {"kill_switch", "daily_notional", "daily_loss"} <= failures


def test_kill_switch_blocks(config):
    context = make_context(config, kill_switch=KillSwitchState(engaged=True, reason="manual"))
    assert not RiskEngine(config).evaluate(make_view(), make_sizing(), context).passed


def test_symbol_off_the_watchlist_is_blocked(config):
    view = make_view()
    object.__setattr__(view, "symbol", "DOGE-USD")
    decision = RiskEngine(config).evaluate(view, make_sizing(), make_context(config))
    assert not rule(decision, "watchlist").passed


def test_auto_execution_mode_is_refused(config):
    auto = AgentConfig(
        execution_mode=ExecutionMode.AUTO,
        watchlist=("BTC-USD",),
        rhs_account_number="1",
        data_dir=config.data_dir,
    )
    decision = RiskEngine(auto).evaluate(make_view(), make_sizing(), make_context(auto))
    finding = rule(decision, "execution_mode")
    assert not finding.passed
    assert "not implemented" in finding.message


def test_wide_spread_blocks(config):
    """Market-maker-routed crypto quotes routinely exceed the spread cap."""
    wide = Quote("BTC-USD", Decimal("80466"), Decimal("81982"), Decimal("81224"), utcnow())
    decision = RiskEngine(config).evaluate(
        make_view(), make_sizing(), make_context(config, quote=wide)
    )
    assert not rule(decision, "spread").passed


def test_stale_quote_blocks(config):
    stale = Quote(
        "BTC-USD",
        Decimal("79900"),
        Decimal("80100"),
        Decimal("80000"),
        utcnow() - timedelta(minutes=10),
    )
    decision = RiskEngine(config).evaluate(
        make_view(), make_sizing(), make_context(config, quote=stale)
    )
    assert not rule(decision, "quote_freshness").passed


def test_halted_pair_warns_but_a_global_halt_blocks(config):
    regional = PairConstraints(
        "BTC-USD", Decimal("0.00000001"), halted=True, halted_regions=("NY",)
    )
    decision = RiskEngine(config).evaluate(
        make_view(), make_sizing(), make_context(config, constraints=regional)
    )
    finding = rule(decision, "pair_tradable")
    assert not finding.passed and not finding.blocking  # a warning

    global_halt = PairConstraints(
        "BTC-USD", Decimal("0.00000001"), halted=True, halted_regions=("ALL",)
    )
    decision = RiskEngine(config).evaluate(
        make_view(), make_sizing(), make_context(config, constraints=global_halt)
    )
    assert not rule(decision, "pair_tradable").passed
    assert rule(decision, "pair_tradable").blocking


def test_market_only_pair_blocks_a_limit_order(config):
    constraints = PairConstraints("BTC-USD", Decimal("0.00000001"), market_orders_only=True)
    decision = RiskEngine(config).evaluate(
        make_view(),
        make_sizing(),
        make_context(config, constraints=constraints, order_type=OrderType.LIMIT),
    )
    assert not rule(decision, "order_type_supported").passed


def test_per_trade_cap_uses_the_worst_case_collar(config):
    """$248 nominal is under the $250 cap, but $250.48 after the buy collar is not."""
    limits = RiskLimits(max_notional_per_trade_usd=Decimal("250"))
    scoped = AgentConfig(
        watchlist=("BTC-USD",), rhs_account_number="1", risk=limits, data_dir=config.data_dir
    )
    decision = RiskEngine(scoped).evaluate(
        make_view(), make_sizing(notional="248"), make_context(scoped)
    )
    assert not rule(decision, "per_trade_notional").passed


def test_daily_notional_cap_counts_prior_executions(config):
    context = make_context(config, activity=make_activity(notional="960"))
    decision = RiskEngine(config).evaluate(make_view(), make_sizing(notional="80"), context)
    assert not rule(decision, "daily_notional").passed


def test_daily_loss_cap_blocks(config):
    context = make_context(config, activity=make_activity(pnl="-250"))
    decision = RiskEngine(config).evaluate(make_view(), make_sizing(), context)
    assert not rule(decision, "daily_loss").passed


def test_open_position_cap_blocks_a_sixth_new_position(config):
    positions = {
        f"SYM{i}-USD": Position(f"SYM{i}-USD", Decimal("1")) for i in range(5)
    }
    context = make_context(config, positions=positions)
    decision = RiskEngine(config).evaluate(make_view(), make_sizing(), context)
    assert not rule(decision, "open_positions").passed


def test_adding_to_an_existing_position_does_not_open_a_new_one(config):
    """At the cap, topping up a held symbol is allowed; a new symbol is not."""
    positions = {f"SYM{i}-USD": Position(f"SYM{i}-USD", Decimal("1")) for i in range(4)}
    positions["BTC-USD"] = Position("BTC-USD", Decimal("1"))  # 5 open, at the cap
    context = make_context(config, positions=positions)
    decision = RiskEngine(config).evaluate(make_view(), make_sizing(), context)
    assert rule(decision, "open_positions").passed


def test_concentration_limit_blocks(config):
    context = make_context(
        config,
        portfolio_value=Decimal("500"),
        positions={"BTC-USD": Position("BTC-USD", Decimal("0.0006"))},
    )
    decision = RiskEngine(config).evaluate(make_view(), make_sizing(notional="80"), context)
    assert not rule(decision, "concentration").passed


def test_selling_an_over_concentrated_position_is_not_blocked_by_concentration(config):
    """A sell can only shrink the position -- the rule must not trap it (first live run)."""
    context = make_context(
        config,
        portfolio_value=Decimal("500"),
        positions={"BTC-USD": Position("BTC-USD", Decimal("0.005"))},  # ~$400: 80% of it
    )
    decision = RiskEngine(config).evaluate(
        make_view(score=-0.8), make_sizing(notional="80", side=Side.SELL), context
    )
    assert rule(decision, "concentration").passed


def test_missing_portfolio_value_warns_instead_of_silently_passing(config):
    context = make_context(config, portfolio_value=None)
    decision = RiskEngine(config).evaluate(make_view(), make_sizing(), context)
    finding = rule(decision, "concentration")
    assert finding.passed and not finding.blocking
    assert "could not be checked" in finding.message


def test_sell_without_enough_holding_is_blocked(config):
    context = make_context(config, positions={"BTC-USD": Position("BTC-USD", Decimal("0.0001"))})
    decision = RiskEngine(config).evaluate(
        make_view(), make_sizing(quantity="0.001", side=Side.SELL), context
    )
    assert not rule(decision, "sell_coverage").passed


def test_low_confidence_and_weak_score_block(config):
    engine = RiskEngine(config)
    weak = engine.evaluate(make_view(score=0.1), make_sizing(), make_context(config))
    assert not rule(weak, "signal_strength").passed
    unsure = engine.evaluate(make_view(confidence=0.05), make_sizing(), make_context(config))
    assert not rule(unsure, "signal_confidence").passed


def test_sizing_rejection_surfaces_as_a_finding(config):
    rejected = SizingResult(
        symbol="BTC-USD",
        side=Side.BUY,
        quantity=Decimal("0"),
        notional=Decimal("0"),
        reference_price=Decimal("80000"),
        rejected_reason="quantity rounds to zero",
    )
    decision = RiskEngine(config).evaluate(make_view(), rejected, make_context(config))
    assert not rule(decision, "sizing").passed
    assert "BLOCKED by" in summarize(decision)


def test_a_sizing_rejection_does_not_masquerade_as_missing_coverage(config):
    """The blocked rule must be the one that actually blocked.

    A weak signal sizes to zero, and a zero quantity used to fail
    ``sell_coverage`` too -- reporting a coverage problem on an account holding
    plenty, and pointing whoever read the audit log at the wrong rule.
    """
    rejected = SizingResult(
        symbol="BTC-USD",
        side=Side.SELL,
        quantity=Decimal("0"),
        notional=Decimal("0"),
        reference_price=Decimal("80000"),
        rejected_reason="sized notional $4.38 is below the $5.00 minimum trade size",
    )
    context = make_context(config, positions={"BTC-USD": Position("BTC-USD", Decimal("10"))})
    decision = RiskEngine(config).evaluate(make_view(), rejected, context)

    assert rule(decision, "sell_coverage").passed
    assert "sizing produced no order" in rule(decision, "sell_coverage").message
    assert not rule(decision, "sizing").passed
    # Still blocked -- this changes which rule explains it, never the verdict.
    assert not decision.passed


def test_sell_coverage_still_blocks_a_real_shortfall(config):
    """The rule it replaces must keep working when sizing did produce an order."""
    context = make_context(config, positions={"BTC-USD": Position("BTC-USD", Decimal("0.0001"))})
    decision = RiskEngine(config).evaluate(
        make_view(), make_sizing(quantity="0.001", side=Side.SELL), context
    )
    assert not rule(decision, "sell_coverage").passed
    assert not decision.passed
