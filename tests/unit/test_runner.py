"""``rhca run`` end to end, with every outside service faked.

What these pin down: what the ladder wants is logged once per bar, even across
a restart; nothing is logged when it wants nothing; one failing task never
stops the loop; and the loop never reads the API key's account balance.
"""

import json
from datetime import timedelta
from decimal import Decimal

import pytest

from robinhood_crypto_agent.audit import KIND_PROPOSAL, AuditLog
from robinhood_crypto_agent.config import AgentConfig, RiskLimits, StrategyConfig
from robinhood_crypto_agent.errors import AgentError
from robinhood_crypto_agent.models import (
    Account,
    ExecutionRecord,
    PairConstraints,
    Position,
    Quote,
    Side,
    utcnow,
)
from robinhood_crypto_agent.runner import Runner, Services
from robinhood_crypto_agent.store import PriceStore, StateCache
from robinhood_crypto_agent.store.prices import floor_to_interval
from tests.conftest import make_candles

#: A steep climb to 80,000, then a close 5.1% under it but still above the
#: one-day average: the $5 step.
RISE = [55000 + i * 25000 / 29 for i in range(30)]
DIP = RISE + [78000, 75900]


class FakeRobinhood:
    """The API key's account is the owner's main one, so the loop must not
    read its balance or holdings: those two methods count their calls."""

    def __init__(self, mark="75900"):
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
        strategy=StrategyConfig(trend_days=1),
    )


def seed(config, prices):
    """Closed hourly bars, the last ending at the top of this hour."""
    store = PriceStore(config.price_store_path)
    start = floor_to_interval(utcnow(), 60) - timedelta(hours=len(prices))
    store.import_candles(make_candles(prices, anchor=start))


def make_runner(config, prices=DIP, **services):
    if not PriceStore(config.price_store_path).observations("BTC-USD"):
        seed(config, prices)
    return Runner(
        config, Services(robinhood=services.pop("robinhood", FakeRobinhood()), **services)
    )


def proposals(config):
    return list(AuditLog(config.audit_path).events(kind=KIND_PROPOSAL))


def test_a_dip_is_proposed_and_logged_as_the_ladders(config):
    make_runner(config).cycle(force=True)
    [record] = proposals(config)
    assert record["status"] == "proposed"
    assert (record["strategy"], record["rule"], record["step"]) == ("ladder", "dip", 0)
    assert record["mode"] == "shadow" and record["source"] == "rhca run"
    assert record["trigger_reason"].startswith("dip: closed 75,900.00")
    heartbeat = json.loads(config.heartbeat_path.read_text())
    assert heartbeat["counts"]["proposed"] == 1
    assert heartbeat["services"] == {"robinhood": True, "dashboard": False}


def test_a_proposal_is_logged_once_per_bar_even_across_a_restart(config):
    runner = make_runner(config)
    runner.cycle(force=True)
    runner.cycle(force=True)
    make_runner(config).cycle(force=True)  # a restart
    assert len(proposals(config)) == 1


def test_nothing_is_logged_when_the_ladder_wants_nothing(config):
    make_runner(config, prices=RISE).cycle(force=True)
    assert proposals(config) == []
    heartbeat = json.loads(config.heartbeat_path.read_text())
    assert heartbeat["counts"]["quotes"] == 1


def test_a_sell_dedupes_apart_from_the_buys(config):
    """Once the dip fills, a trend exit is its own proposal, logged once."""
    runner = make_runner(config)
    runner.cycle(force=True)
    [buy] = proposals(config)
    AuditLog(config.audit_path).record_execution(
        ExecutionRecord(
            proposal_id=buy["proposal_id"],
            symbol="BTC-USD",
            side=Side.BUY,
            recorded_at=utcnow(),
            requested_quantity=Decimal(buy["quantity"]),
            filled_quantity=Decimal(buy["quantity"]),
            notional=Decimal("5"),
            order_id="ord-1",
            state="filled",
        )
    )
    StateCache(config.data_dir / "market_state.json").put_positions(
        [Position("BTC-USD", Decimal(buy["quantity"]))]
    )
    # The same bars, but now the ladder holds: the dip is spent, nothing new.
    runner.cycle(force=True)
    assert len(proposals(config)) == 1

    # A fresh history that closes under its average: the trend exit.
    PriceStore(config.price_store_path).path.unlink()
    seed(config, DIP + [70000, 64000])
    runner = make_runner(config, robinhood=FakeRobinhood("64000"))
    runner.cycle(force=True)
    runner.cycle(force=True)
    sells = [r for r in proposals(config) if r["side"] == "sell"]
    assert [(r["rule"], r["step"]) for r in sells] == [("trend_exit", None)]


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
        Services(robinhood=FakeRobinhood()),
        robinhood_account="****1977",
    ).cycle(force=True)
    heartbeat = json.loads(config.heartbeat_path.read_text())
    assert heartbeat["robinhood_account"] == "****1977"
