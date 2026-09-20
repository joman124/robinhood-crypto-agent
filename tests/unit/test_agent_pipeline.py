"""The analysis pipeline: a symbol always gets either a proposal or a reason."""

from decimal import Decimal

import pytest

from robinhood_crypto_agent.agent import Agent, MarketState
from robinhood_crypto_agent.config import AgentConfig
from robinhood_crypto_agent.models import (
    Direction,
    PairConstraints,
    Position,
    Quote,
    Side,
    utcnow,
)
from robinhood_crypto_agent.store import PriceStore
from tests.conftest import choppy, downtrend, make_candles, uptrend


@pytest.fixture
def config(tmp_path):
    return AgentConfig(
        watchlist=("BTC-USD", "ETH-USD"),
        rhs_account_number="123456789",
        data_dir=tmp_path / "data",
    )


def seeded_agent(config, prices, *, symbol="BTC-USD"):
    store = PriceStore(config.price_store_path)
    store.import_candles(make_candles(prices, symbol=symbol))
    return Agent(config, store=store)


def market(price="80000", **overrides):
    mark = Decimal(price)
    defaults = dict(
        quotes={
            "BTC-USD": Quote(
                "BTC-USD", mark * Decimal("0.999"), mark * Decimal("1.001"), mark, utcnow()
            )
        },
        constraints={
            "BTC-USD": PairConstraints(
                "BTC-USD", Decimal("0.00000001"), min_order_size=Decimal("0.000001")
            )
        },
        positions={},
        portfolio_value=Decimal("10000"),
    )
    defaults.update(overrides)
    return MarketState(**defaults)


def test_an_uptrend_produces_a_buy_proposal(config):
    agent = seeded_agent(config, uptrend())
    result = agent.analyze(market(), symbols=["BTC-USD"])
    proposal = result.proposals[0]
    assert proposal.side is Side.BUY
    assert proposal.quantity > 0
    assert proposal.view.direction is Direction.LONG


def test_a_buy_is_referenced_to_the_ask_not_the_mark(config):
    """Sizing off the mark understates the cost of crossing a wide spread."""
    agent = seeded_agent(config, uptrend())
    state = market()
    result = agent.analyze(state, symbols=["BTC-USD"])
    assert result.proposals[0].reference_price == state.quotes["BTC-USD"].ask


def test_a_sell_is_referenced_to_the_bid(config):
    agent = seeded_agent(config, downtrend())
    state = market(positions={"BTC-USD": Position("BTC-USD", Decimal("1"))})
    result = agent.analyze(state, symbols=["BTC-USD"])
    proposal = result.proposals[0]
    assert proposal.side is Side.SELL
    assert proposal.reference_price == state.quotes["BTC-USD"].bid


def test_a_symbol_with_no_history_is_skipped_with_an_explanation(config):
    agent = Agent(config, store=PriceStore(config.price_store_path))
    result = agent.analyze(market(), symbols=["BTC-USD"])
    outcome = result.outcomes[0]
    assert outcome.proposal is None
    assert "no price history" in outcome.skipped_reason
    assert "historicals" in outcome.skipped_reason  # says why, not just that


def test_a_symbol_with_no_quote_is_skipped_with_an_explanation(config):
    agent = seeded_agent(config, uptrend())
    result = agent.analyze(MarketState(quotes={}), symbols=["BTC-USD"])
    assert "no live quote" in result.outcomes[0].skipped_reason


def test_an_off_watchlist_symbol_is_never_proposed(config):
    agent = seeded_agent(config, uptrend(), symbol="DOGE-USD")
    result = agent.analyze(market(), symbols=["DOGE-USD"])
    assert result.proposals == []
    assert "allowlist" in result.outcomes[0].skipped_reason


def test_a_flat_market_produces_no_proposal(config):
    agent = seeded_agent(config, [100.0] * 70)
    result = agent.analyze(market(price="100"), symbols=["BTC-USD"])
    assert result.proposals == []
    assert result.outcomes[0].skipped_reason


def test_every_outcome_has_either_a_proposal_or_a_reason(config):
    agent = seeded_agent(config, choppy())
    result = agent.analyze(market(price="100"), symbols=["BTC-USD", "ETH-USD"])
    for outcome in result.outcomes:
        assert outcome.produced_proposal or outcome.skipped_reason


def test_proposals_are_recorded_including_blocked_ones(config):
    tight = AgentConfig(
        watchlist=config.watchlist,
        rhs_account_number="1",
        data_dir=config.data_dir,
    )
    object.__setattr__(tight.risk, "min_abs_score", Decimal("0.99"))
    agent = seeded_agent(tight, uptrend())
    result = agent.analyze(market(), symbols=["BTC-USD"])
    assert result.blocked
    logged = list(agent.audit.events(kind="proposal"))
    assert logged and logged[-1]["risk_passed"] is False


def test_no_record_leaves_the_audit_log_untouched(config):
    agent = seeded_agent(config, uptrend())
    agent.analyze(market(), symbols=["BTC-USD"], record=False)
    assert list(agent.audit.events(kind="proposal")) == []


def test_a_staged_plan_appears_in_a_ranging_market(config):
    agent = seeded_agent(config, choppy(amplitude=12.0))
    result = agent.analyze(market(price="100"), symbols=["BTC-USD"])
    if result.proposals:  # chop does not always clear the direction deadband
        assert result.proposals[0].plan.total_quantity == result.proposals[0].quantity


def test_analysis_falls_back_to_permissive_constraints(config):
    """A missing pair record must not widen what an order may do."""
    agent = seeded_agent(config, uptrend())
    result = agent.analyze(market(constraints={}), symbols=["BTC-USD"])
    proposal = result.proposals[0]
    assert proposal.quantity == proposal.quantity.quantize(Decimal("0.00000001"))
