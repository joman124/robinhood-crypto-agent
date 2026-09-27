"""The time exit: what the agent bought is proposed for sale once held the horizon.

What these pin down: only the agent's own recorded buys are ever sold, first in
first out; nothing is proposed before the horizon; the proposal is capped at
what the account holds; it passes the rules that do not apply to closing a
position -- saying so -- while the kill switch and sell coverage still bind;
and a coin sold outside the agent is not chased.
"""

import json
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest

from robinhood_crypto_agent.agent import Agent, MarketState
from robinhood_crypto_agent.audit import KIND_PROPOSAL, AuditLog
from robinhood_crypto_agent.config import AgentConfig
from robinhood_crypto_agent.execution.kill_switch import KillSwitch
from robinhood_crypto_agent.exits import due_lots, open_lots
from robinhood_crypto_agent.models import (
    ExecutionRecord,
    PairConstraints,
    Position,
    ProposalStatus,
    Quote,
    Side,
    utcnow,
)
from robinhood_crypto_agent.store import PriceStore
from tests.conftest import make_candles

LATER = timedelta(hours=7)


@pytest.fixture
def config(tmp_path):
    return AgentConfig(
        watchlist=("BTC-USD",),
        rhs_account_number="123456789",
        data_dir=tmp_path / "data",
    )


def fill(audit, *, side="buy", quantity="0.001", state="filled", proposal="entry1"):
    return audit.record_execution(
        ExecutionRecord(
            proposal_id=proposal,
            symbol="BTC-USD",
            side=Side(side),
            recorded_at=utcnow(),
            requested_quantity=Decimal(quantity),
            filled_quantity=Decimal(quantity),
            notional=Decimal(quantity) * Decimal("80000"),
            order_id=f"ord-{proposal}-{side}",
            state=state,
        )
    )


def market(held="0.001", *, as_of=None, bid="79900"):
    bid = Decimal(bid)
    # Inside the code's default 0.75% spread cap, so the spread rule stays quiet.
    ask, mark = bid * Decimal("1.004"), bid * Decimal("1.002")
    return MarketState(
        quotes={"BTC-USD": Quote("BTC-USD", bid, ask, mark, utcnow())},
        constraints={
            "BTC-USD": PairConstraints(
                "BTC-USD", Decimal("0.00000001"), price_increment=Decimal("0.01")
            )
        },
        positions={"BTC-USD": Position("BTC-USD", Decimal(held))} if held else {},
        portfolio_value=Decimal("500"),
        positions_as_of=as_of,
    )


def agent(config):
    return Agent(config, store=PriceStore(config.price_store_path))


class TestOpenLots:
    def test_sells_close_the_oldest_lots_first(self, config):
        audit = AuditLog(config.audit_path)
        fill(audit, quantity="0.002", proposal="a")
        fill(audit, quantity="0.001", proposal="b")
        fill(audit, side="sell", quantity="0.0025", proposal="x")
        [lot] = open_lots(audit)["BTC-USD"]
        assert lot.proposal_id == "b" and lot.quantity == Decimal("0.0005")

    def test_unfilled_and_rejected_orders_open_nothing(self, config):
        audit = AuditLog(config.audit_path)
        fill(audit, quantity="0", state="new")
        fill(audit, state="rejected")
        assert open_lots(audit) == {}

    def test_a_lot_is_due_only_after_the_hold(self, config):
        audit = AuditLog(config.audit_path)
        fill(audit)
        lots = open_lots(audit)
        hold = timedelta(hours=6)
        assert due_lots(lots, now=utcnow(), hold=hold) == {}
        assert due_lots(lots, now=utcnow() + LATER, hold=hold)


class TestExitProposals:
    def test_a_lot_held_six_hours_is_proposed_for_sale(self, config):
        fill(AuditLog(config.audit_path))
        [exit_] = agent(config).exit_proposals(market(), now=utcnow() + LATER)
        assert exit_.side is Side.SELL
        assert exit_.quantity == Decimal("0.001")
        assert exit_.reference_price == Decimal("79900")  # sells at the bid
        assert exit_.plan.tranches[0].target_price == Decimal("79900")
        assert exit_.sizing_detail["entry_proposal_ids"] == ["entry1"]
        assert exit_.risk.passed, [f.message for f in exit_.risk.blocking_failures]
        assert exit_.status is ProposalStatus.PROPOSED

    def test_nothing_is_proposed_before_the_horizon(self, config):
        fill(AuditLog(config.audit_path))
        assert agent(config).exit_proposals(market(), now=utcnow() + timedelta(hours=5)) == []

    def test_a_coin_bought_outside_the_agent_is_never_sold(self, config):
        """Holdings with no recorded agent buy are not the agent's to sell."""
        assert agent(config).exit_proposals(market(held="5"), now=utcnow() + LATER) == []

    def test_the_sale_is_capped_at_what_the_account_holds(self, config):
        fill(AuditLog(config.audit_path), quantity="0.002")
        [exit_] = agent(config).exit_proposals(market(held="0.0015"), now=utcnow() + LATER)
        assert exit_.quantity == Decimal("0.0015")

    def test_a_coin_sold_elsewhere_after_the_buy_is_not_chased(self, config):
        fill(AuditLog(config.audit_path))
        fresh = market(held=None, as_of=utcnow() + timedelta(minutes=1))
        assert agent(config).exit_proposals(fresh, now=utcnow() + LATER) == []

    def test_a_stale_snapshot_still_proposes_and_coverage_says_why(self, config):
        """Silence would hide the exit; the coverage finding names the fix."""
        fill(AuditLog(config.audit_path))
        stale = market(held=None, as_of=utcnow() - timedelta(hours=1))
        [exit_] = agent(config).exit_proposals(stale, now=utcnow() + LATER)
        assert [f.rule for f in exit_.risk.blocking_failures] == ["sell_coverage"]

    def test_exempt_rules_pass_and_say_what_they_would_have_found(self, config):
        limits = replace(
            config.risk,
            disable_sell_side=True,
            max_notional_per_trade_usd=Decimal("10"),
            max_daily_notional_usd=Decimal("20"),
        )
        strict = replace(config, risk=limits)
        fill(AuditLog(strict.audit_path))
        [exit_] = agent(strict).exit_proposals(market(), now=utcnow() + LATER)
        assert exit_.risk.passed
        exempted = {
            f.rule: f.message for f in exit_.risk.findings if "not applied to a time exit" in f.message
        }
        assert set(exempted) == {
            "sell_side_disabled",
            "signal_confidence",
            "signal_strength",
            "per_trade_notional",
            "daily_notional",
        }
        assert "would have read" in exempted["sell_side_disabled"]

    def test_the_kill_switch_still_blocks_an_exit(self, config):
        fill(AuditLog(config.audit_path))
        KillSwitch(config.kill_switch_path).engage("manual")
        [exit_] = agent(config).exit_proposals(market(), now=utcnow() + LATER)
        assert "kill_switch" in [f.rule for f in exit_.risk.blocking_failures]

    def test_an_exit_already_filled_is_not_proposed_again(self, config):
        audit = AuditLog(config.audit_path)
        fill(audit)
        fill(audit, side="sell", proposal="the-exit")
        assert agent(config).exit_proposals(market(), now=utcnow() + LATER) == []

    def test_zero_bars_turns_it_off(self, config):
        off = replace(config, strategy=replace(config.strategy, exit_after_bars=0))
        fill(AuditLog(off.audit_path))
        assert agent(off).exit_proposals(market(), now=utcnow() + LATER) == []


def test_analyze_records_the_exit_as_unscored_and_labelled(config, monkeypatch):
    fill(AuditLog(config.audit_path))
    store = PriceStore(config.price_store_path)
    store.import_candles(make_candles([80000] * 40))
    monkeypatch.setattr("robinhood_crypto_agent.agent.utcnow", lambda: utcnow() + LATER)

    result = Agent(config, store=store).analyze(market())

    [exit_] = result.exits
    assert exit_ in result.proposals and exit_ in result.executable
    [record] = [
        r for r in AuditLog(config.audit_path).events(kind=KIND_PROPOSAL) if r.get("exit")
    ]
    assert record["exit"] == "time"
    assert record["entry_proposal_ids"] == ["entry1"]
    assert record["trigger_reason"].startswith("time exit: bought")
    assert json.dumps(record)  # serializable, as the dashboard reads it
