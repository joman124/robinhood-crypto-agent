"""``rhca run`` end to end, with every outside service faked.

What these pin down: what the split wants is logged once an hour, even
across a restart; nothing is logged when it wants nothing; one failing task
never stops the loop; and the loop never reads the API key's account balance.
"""

import json
from decimal import Decimal

import pytest

from robinhood_crypto_agent.audit import KIND_PROPOSAL, AuditLog
from robinhood_crypto_agent.config import AgentConfig, RiskLimits
from robinhood_crypto_agent.daily import latest_close
from robinhood_crypto_agent.errors import AgentError
from robinhood_crypto_agent.models import (
    Account,
    PairConstraints,
    Position,
    Quote,
    utcnow,
)
from robinhood_crypto_agent.runner import Runner, Services
from robinhood_crypto_agent.store import StateCache
from tests.conftest import FakeCoinbaseDaily

RISING = [50000 * 1.001**i for i in range(239)]
BREAKOUT = RISING + [RISING[-1] * 1.05]
QUIET = RISING + [RISING[-1]]  # no new high: nothing to do
LAST_CLOSE = str(round(BREAKOUT[-1], 2))


class FakeRobinhood:
    """The API key's account is the owner's main one, so the loop must not
    read its balance or holdings: those two methods count their calls."""

    def __init__(self, mark=LAST_CLOSE):
        self.fail_pairs = False
        self.account_reads = 0
        self.mark = Decimal(mark)

    def best_bid_ask(self, symbols):
        mark = self.mark
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


@pytest.fixture
def config(tmp_path):
    return AgentConfig(
        watchlist=("BTC-USD",),
        data_dir=tmp_path / "data",
        risk=RiskLimits(min_notional_per_trade_usd=Decimal("5"), max_spread_pct=Decimal("2")),
    )


def make_runner(config, path=BREAKOUT, **services):
    daily = FakeCoinbaseDaily({"BTC-USD": path}, last=latest_close(utcnow()))
    return Runner(
        config,
        Services(
            robinhood=services.pop("robinhood", FakeRobinhood()),
            daily_history=daily,
            **services,
        ),
    )


def proposals(config):
    return list(AuditLog(config.audit_path).events(kind=KIND_PROPOSAL))


def test_a_breakout_is_proposed_and_logged_as_the_splits(config):
    make_runner(config).cycle(force=True)
    [record] = proposals(config)
    assert record["status"] == "proposed"
    assert (record["strategy"], record["sleeve"], record["rule"]) == (
        "split", "short-term", "entry"
    )
    assert record["mode"] == "shadow" and record["source"] == "rhca run"
    assert record["trigger_reason"].startswith("breakout entry: closed")
    heartbeat = json.loads(config.heartbeat_path.read_text())
    assert heartbeat["counts"]["proposed"] == 1
    assert heartbeat["services"] == {"robinhood": True, "dashboard": False, "forward_test": False}


def test_a_proposal_is_logged_once_an_hour_even_across_a_restart(config):
    runner = make_runner(config)
    runner.cycle(force=True)
    runner.cycle(force=True)
    make_runner(config).cycle(force=True)  # a restart
    assert len(proposals(config)) == 1


def test_nothing_is_logged_when_the_split_wants_nothing(config):
    make_runner(config, path=QUIET).cycle(force=True)
    assert proposals(config) == []
    heartbeat = json.loads(config.heartbeat_path.read_text())
    assert heartbeat["counts"]["quotes"] == 1


def test_one_failing_task_does_not_stop_the_others(config):
    robinhood = FakeRobinhood()
    robinhood.fail_pairs = True
    make_runner(config, robinhood=robinhood).cycle(force=True)
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


def test_the_heartbeat_names_the_keys_account_masked(config):
    Runner(
        config,
        Services(
            robinhood=FakeRobinhood(),
            daily_history=FakeCoinbaseDaily({"BTC-USD": QUIET}, last=latest_close(utcnow())),
        ),
        robinhood_account="****1977",
    ).cycle(force=True)
    heartbeat = json.loads(config.heartbeat_path.read_text())
    assert heartbeat["robinhood_account"] == "****1977"
