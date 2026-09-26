"""``rhca run`` end to end, with every outside service faked.

What these pin down: a strong candidate is escalated once and logged with
System 2's answer; everything that is not a "propose" is logged as something
else; the same idea is never logged twice, even across a restart; and one
failing source never stops the loop.
"""

import json
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest

from robinhood_crypto_agent.audit import KIND_PROPOSAL, AuditLog
from robinhood_crypto_agent.config import AgentConfig, PipelineConfig
from robinhood_crypto_agent.errors import AgentError
from robinhood_crypto_agent.models import (
    Account,
    NewsItem,
    NewsLabels,
    PairConstraints,
    Position,
    Quote,
    utcnow,
)
from robinhood_crypto_agent.net import HttpError
from robinhood_crypto_agent.news import NewsStore
from robinhood_crypto_agent.runner import Runner, Services, system2_tools
from robinhood_crypto_agent.store import PriceStore, StateCache
from robinhood_crypto_agent.system2 import System2Decision
from tests.conftest import make_candles, uptrend


class FakeRobinhood:
    """The API key's account is the owner's main one, so the loop must not
    read its balance or holdings: those two methods count their calls."""

    def __init__(self):
        self.fail_pairs = False
        self.account_reads = 0

    def best_bid_ask(self, symbols):
        mark = Decimal("80000")
        return [Quote("BTC-USD", mark * Decimal("0.999"), mark * Decimal("1.001"), mark, utcnow())]

    def trading_pairs(self, symbols):
        if self.fail_pairs:
            raise AgentError("trading pairs endpoint is down")
        return [PairConstraints("BTC-USD", Decimal("0.00000001"), min_order_size=Decimal("0.000001"))]

    def holdings(self):
        self.account_reads += 1
        return [Position("XRP-USD", Decimal("1000"))]

    def account(self):
        self.account_reads += 1
        return Account("311130671977", "", buying_power=Decimal("5000"))


class FakeSystem2:
    market_data_url = None

    def __init__(self, decision="propose"):
        self.decision = System2Decision(decision, "because", 0.7, model="claude-sonnet-5")
        self.calls = []

    def decide(self, proposal, news):
        self.calls.append((proposal, list(news)))
        return self.decision


class FakeJev:
    def __init__(self, error=None):
        self.error = error

    def label(self, item, watchlist):
        if self.error:
            raise self.error
        return NewsLabels("BTC", 0.95, "bullish", 0.9, 2.7, model="jev-test")


@pytest.fixture
def config(tmp_path):
    return AgentConfig(
        watchlist=("BTC-USD",),
        data_dir=tmp_path / "data",
        pipeline=PipelineConfig(rss_feeds=("https://feed.example/rss",)),
    )


def make_runner(config, *, system2=None, jev=None, feeds=(), **services):
    store = PriceStore(config.price_store_path)
    if not store.observations("BTC-USD"):
        store.import_candles(make_candles(uptrend(), symbol="BTC-USD"))
    return Runner(
        config,
        Services(
            robinhood=services.pop("robinhood", FakeRobinhood()),
            jev=jev,
            system2=system2,
            fetch_feed=lambda url: list(feeds),
            **services,
        ),
    )


def proposals(config):
    return list(AuditLog(config.audit_path).events(kind=KIND_PROPOSAL))


def headline(minutes_ago=5):
    published = utcnow() - timedelta(minutes=minutes_ago)
    return NewsItem("n1", "feed.example", "ETF inflows hit a record", published)


def test_a_strong_candidate_is_escalated_and_logged_with_the_answer(config):
    system2 = FakeSystem2("propose")
    make_runner(config, system2=system2).cycle(force=True)

    [record] = proposals(config)
    assert record["status"] == "proposed"
    assert record["escalated"] is True
    assert record["system2_decision"] == "propose"
    assert record["mode"] == "shadow"
    assert len(system2.calls) == 1

    heartbeat = json.loads(config.heartbeat_path.read_text())
    assert heartbeat["counts"]["escalations"] == 1
    assert heartbeat["services"]["system2"] is True


def test_a_system2_pass_is_logged_as_declined(config):
    make_runner(config, system2=FakeSystem2("pass")).cycle(force=True)
    assert proposals(config)[0]["status"] == "declined_by_system2"


def test_without_system2_nothing_is_approved(config):
    make_runner(config).cycle(force=True)
    [record] = proposals(config)
    assert record["status"] == "declined_by_system2"
    assert record["system2_decision"] == "error"
    assert record["escalated"] is False  # no Sonnet call, so no budget spent


def test_below_the_trigger_is_logged_but_not_escalated(config):
    # A clean synthetic uptrend is fully confident, so hold it back on strength.
    strict = replace(config, pipeline=replace(config.pipeline, trigger_min_abs_score=Decimal("1")))
    system2 = FakeSystem2()
    make_runner(strict, system2=system2).cycle(force=True)
    [record] = proposals(strict)
    assert record["status"] == "not_escalated"
    assert "below the trigger" in record["trigger_reason"]
    assert system2.calls == []


def test_the_same_idea_is_logged_once_even_across_a_restart(config):
    system2 = FakeSystem2()
    runner = make_runner(config, system2=system2)
    runner.cycle(force=True)
    runner.cycle(force=True)
    make_runner(config, system2=system2).cycle(force=True)  # a restart
    assert len(proposals(config)) == 1
    assert len(system2.calls) == 1
    assert make_runner(config)._escalations_today == 1


def test_news_is_labeled_stored_and_shown_to_system2(config):
    system2 = FakeSystem2()
    make_runner(config, system2=system2, jev=FakeJev(), feeds=[headline()]).cycle(force=True)

    [stored] = NewsStore(config.news_path).items()
    assert stored.labels.direction == "bullish"
    _, news = system2.calls[0]
    assert [n.item_id for n in news] == ["n1"]
    assert "news" in [s["name"] for s in proposals(config)[0]["proposal"]["view"]["signals"]]


def test_a_jev_outage_leaves_headlines_for_the_next_poll(config):
    runner = make_runner(config, jev=FakeJev(HttpError("timed out")), feeds=[headline()])
    runner.cycle(force=True)
    assert NewsStore(config.news_path).items() == []

    runner.services.jev = FakeJev()
    runner.cycle(force=True)
    assert [i.item_id for i in NewsStore(config.news_path).items()] == ["n1"]


def test_one_failing_task_does_not_stop_the_others(config):
    robinhood = FakeRobinhood()
    robinhood.fail_pairs = True
    make_runner(config, system2=FakeSystem2(), robinhood=robinhood).cycle(force=True)
    heartbeat = json.loads(config.heartbeat_path.read_text())
    assert heartbeat["last_error"]["task"] == "pairs"
    assert heartbeat["counts"]["quotes"] == 1
    assert len(proposals(config)) == 1


def agentic_snapshot(config):
    """What `rhca ingest positions` and `rhca ingest portfolio` cache."""
    cache = StateCache(config.data_dir / "market_state.json")
    cache.put_positions([Position("BTC-USD", Decimal("0.002"))])
    cache.put_portfolio_value(Decimal("500"))
    cache.put_crypto_buying_power(Decimal("340"))
    return cache


def test_the_loop_leaves_the_ingested_agentic_snapshot_alone(config):
    """The key reads the owner's main account; orders go to the Agentic one."""
    cache = agentic_snapshot(config)
    robinhood = FakeRobinhood()
    runner = make_runner(config, robinhood=robinhood)
    runner.cycle(force=True)
    runner.cycle(force=True)

    assert robinhood.account_reads == 0
    assert list(cache.positions()) == ["BTC-USD"]
    assert cache.portfolio_value() == Decimal("500")
    assert cache.crypto_buying_power() == Decimal("340")
    assert cache.account() is None
    assert "BTC-USD" in cache.pairs()  # market data still comes from the key


def test_system2_sees_the_ingested_holdings_not_the_keys_account(config):
    cache = agentic_snapshot(config)
    robinhood = FakeRobinhood()
    fetch_quote, fetch_holdings = system2_tools(robinhood, cache)

    holdings = fetch_holdings()
    assert holdings["positions"] == [{"symbol": "BTC-USD", "quantity": "0.002"}]
    assert holdings["crypto_buying_power"] == "340"
    assert holdings["positions_as_of"] and holdings["crypto_buying_power_as_of"]
    assert robinhood.account_reads == 0
    assert fetch_quote("BTC-USD").symbol == "BTC-USD"  # quotes are still live


def test_system2_is_told_when_nothing_was_ingested(config):
    cache = StateCache(config.data_dir / "market_state.json")
    _, fetch_holdings = system2_tools(FakeRobinhood(), cache)
    assert fetch_holdings() == {
        "positions": [],
        "positions_as_of": None,
        "crypto_buying_power": None,
        "crypto_buying_power_as_of": None,
    }


def test_the_heartbeat_names_the_keys_account_masked(config):
    Runner(
        config,
        Services(robinhood=FakeRobinhood(), fetch_feed=lambda url: []),
        robinhood_account="****1977",
    ).cycle(force=True)
    heartbeat = json.loads(config.heartbeat_path.read_text())
    assert heartbeat["robinhood_account"] == "****1977"


def test_a_time_exit_is_logged_once_per_bar_and_never_sent_to_system2(config, monkeypatch):
    """An exit is the owner's rule: no trigger, no Sonnet, no duplicate rows."""
    from datetime import timedelta

    from robinhood_crypto_agent.models import ExecutionRecord, Side

    AuditLog(config.audit_path).record_execution(
        ExecutionRecord(
            proposal_id="entry1",
            symbol="BTC-USD",
            side=Side.BUY,
            recorded_at=utcnow(),
            requested_quantity=Decimal("0.001"),
            filled_quantity=Decimal("0.001"),
            notional=Decimal("80"),
            order_id="ord-1",
            state="filled",
        )
    )
    StateCache(config.data_dir / "market_state.json").put_positions(
        [Position("BTC-USD", Decimal("0.001"))]
    )
    later = utcnow() + timedelta(hours=7)
    monkeypatch.setattr("robinhood_crypto_agent.agent.utcnow", lambda: later)

    system2 = FakeSystem2()
    runner = make_runner(config, system2=system2)
    runner.cycle(force=True)
    runner.cycle(force=True)
    make_runner(config, system2=system2).cycle(force=True)  # a restart

    [exit_] = [r for r in proposals(config) if r.get("exit")]
    assert exit_["side"] == "sell" and exit_["status"] == "proposed"
    assert exit_["escalated"] is False
    assert exit_["candidate_key"].startswith("exit|")
    assert all(proposal.side is Side.BUY for proposal, _ in system2.calls)
