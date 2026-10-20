"""``rhca run`` end to end, with every outside service faked.

What these pin down: what the split wants is logged once an hour, even
across a restart; nothing is logged when it wants nothing; one failing task
never stops the loop; and the loop never reads the API key's account balance.
"""

import json
from decimal import Decimal

import pytest

from robinhood_crypto_agent import runner as runner_mod
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
from robinhood_crypto_agent.runner import Runner, Services, running_elsewhere
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
    assert heartbeat["services"] == {
        "robinhood": True, "dashboard": False, "forward_test": False,
        "healthcheck": False, "push": False,
    }


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


HEALTHCHECK = "https://hc-ping.com/00000000-secret-uuid"


def test_the_loop_pings_its_healthcheck_and_keeps_a_failure_s_url_out_of_the_heartbeat(
    config, monkeypatch
):
    pinged = []
    monkeypatch.setattr(
        runner_mod.net, "request", lambda method, url, **kw: pinged.append((method, url))
    )
    make_runner(config, path=QUIET, healthcheck_url=HEALTHCHECK).cycle(force=True)
    assert pinged == [("GET", HEALTHCHECK)]
    assert json.loads(config.heartbeat_path.read_text())["services"]["healthcheck"] is True

    def down(method, url, **kw):
        raise runner_mod.net.HttpError(f"{method} {url} failed: timed out")

    monkeypatch.setattr(runner_mod.net, "request", down)
    make_runner(config, path=QUIET, healthcheck_url=HEALTHCHECK).cycle(force=True)
    heartbeat = config.heartbeat_path.read_text()
    assert "healthcheck ping failed (no connection)" in heartbeat
    assert "secret-uuid" not in heartbeat


PUSH_WEBHOOK = "https://ntfy.sh/rhca-secret-topic"


def test_the_first_proposal_per_coin_pushes_once_even_across_a_restart(config, monkeypatch):
    """The dead-zone-ending alert: one push the first time the split proposes a
    coin, never again -- not this hour, and not after a restart."""
    sent = []
    monkeypatch.setattr(
        runner_mod.push_mod, "notify", lambda url, title, msg: sent.append((url, title, msg)) or True
    )
    runner = make_runner(config, push_webhook=PUSH_WEBHOOK)
    runner.cycle(force=True)
    runner.cycle(force=True)  # same hour: logged once, pushed once
    make_runner(config, push_webhook=PUSH_WEBHOOK).cycle(force=True)  # a restart

    assert len(sent) == 1
    url, title, message = sent[0]
    assert url == PUSH_WEBHOOK
    assert "first BTC-USD proposal" in title
    assert "BTC-USD -- short-term entry buy" in message
    assert "breakout entry" in message  # the one-liner on what fired
    assert "approve by id" in message


def test_no_push_without_a_webhook(config, monkeypatch):
    sent = []
    monkeypatch.setattr(runner_mod.push_mod, "notify", lambda *a: sent.append(a) or True)
    make_runner(config).cycle(force=True)  # proposes, but no webhook configured
    assert proposals(config) and sent == []


def test_a_failed_push_is_retried_on_the_next_proposal(config, monkeypatch):
    """A push that fails leaves the coin un-announced, so the next hour's
    proposal tries again rather than losing the alert to a blip."""
    attempts = []
    monkeypatch.setattr(
        runner_mod.push_mod, "notify", lambda url, title, msg: attempts.append(title) or False
    )
    runner = make_runner(config, push_webhook=PUSH_WEBHOOK)
    runner.cycle(force=True)
    # A fresh hour re-logs the proposal; the failed push is retried.
    runner._last_key.clear()
    runner.cycle(force=True)
    assert len(attempts) == 2


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


def test_a_second_loop_is_refused_while_the_first_is_alive(config):
    """Started at sign-in, a loop must not run beside one started by hand."""
    Runner(
        config,
        Services(
            robinhood=FakeRobinhood(),
            daily_history=FakeCoinbaseDaily({"BTC-USD": QUIET}, last=latest_close(utcnow())),
        ),
    ).cycle(force=True)
    heartbeat = json.loads(config.heartbeat_path.read_text())
    assert running_elsewhere(heartbeat, stale_after_seconds=180) is None  # this process wrote it

    other = {**heartbeat, "pid": -1}
    assert "already running (pid -1" in running_elsewhere(other, stale_after_seconds=180)
    stale = {**other, "last_cycle_at": "2026-01-01T00:00:00+00:00"}
    assert running_elsewhere(stale, stale_after_seconds=180) is None  # it died
    assert running_elsewhere(None, stale_after_seconds=180) is None
