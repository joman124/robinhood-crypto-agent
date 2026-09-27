"""The analysis pipeline: the trend ladder, live.

A symbol always gets either proposals or a reason. The ladder's state comes
from the fills recorded in the audit log, and the strongest claim here is the
last test: fed the same bars, the live pipeline makes exactly the trades the
backtest makes.
"""

from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from robinhood_crypto_agent import backtest
from robinhood_crypto_agent.agent import Agent, MarketState
from robinhood_crypto_agent.audit import KIND_PROPOSAL, AuditLog
from robinhood_crypto_agent.config import AgentConfig, RiskLimits, StrategyConfig
from robinhood_crypto_agent.execution.kill_switch import KillSwitch
from robinhood_crypto_agent.ledger import ladder_position
from robinhood_crypto_agent.models import (
    ExecutionRecord,
    PairConstraints,
    Position,
    Proposal,
    ProposalStatus,
    Quote,
    Side,
    utcnow,
)
from robinhood_crypto_agent.outcomes import outcome_from_proposal_record
from robinhood_crypto_agent.store import PriceStore
from tests.conftest import make_candles

D = Decimal
#: Half the spread each quote carries: bid and ask sit this far from the close.
HALF = D("0.0025")


def piecewise(points):
    """Closes interpolated linearly between (bar, price) points."""
    out = []
    for (i0, p0), (i1, p1) in zip(points, points[1:], strict=False):
        out += [round(p0 + (p1 - p0) * (i - i0) / (i1 - i0), 4) for i in range(i0, i1)]
    return out + [points[-1][1]]


RISE = piecewise([(0, 100), (30, 130)])  # ends on its high, well above its average


@pytest.fixture
def config(tmp_path):
    return AgentConfig(
        watchlist=("BTC-USD", "ETH-USD"),
        rhs_account_number="123456789",
        data_dir=tmp_path / "data",
        risk=RiskLimits(
            min_notional_per_trade_usd=D("5"),
            max_notional_per_trade_usd=D("50"),
            max_daily_notional_usd=D("200"),
            max_spread_pct=D("2"),
        ),
        # A one-day average (24 bars) keeps the series short.
        strategy=StrategyConfig(trend_days=1),
    )


class Harness:
    def __init__(self, config, prices=()):
        self.config = config
        self.store = PriceStore(config.price_store_path)
        self.audit = AuditLog(config.audit_path)
        self.agent = Agent(config, store=self.store, audit=self.audit)
        self.candles = []
        self.extend(prices)

    def extend(self, prices):
        start = self.candles[-1].end if self.candles else None
        new = make_candles(prices, anchor=start)
        self.store.import_candles(new)
        self.candles += new
        return self

    def market(self, *, held=None, as_of=None, close=None):
        close = D(str(close)) if close is not None else self.candles[-1].close
        return MarketState(
            quotes={
                "BTC-USD": Quote(
                    "BTC-USD", close * (1 - HALF), close * (1 + HALF), close, utcnow()
                )
            },
            constraints={
                "BTC-USD": PairConstraints(
                    "BTC-USD", D("0.00000001"), price_increment=D("0.01")
                )
            },
            positions={} if held is None else {"BTC-USD": Position("BTC-USD", D(str(held)))},
            portfolio_value=D("100000"),
            positions_as_of=as_of,
        )

    def analyze(self, **market):
        return self.agent.analyze(self.market(**market), symbols=["BTC-USD"])

    def fill(self, proposal, *, quantity=None, state="filled"):
        quantity = proposal.quantity if quantity is None else D(str(quantity))
        self.audit.record_execution(
            ExecutionRecord(
                proposal_id=proposal.proposal_id,
                symbol=proposal.symbol,
                side=proposal.side,
                recorded_at=utcnow(),
                requested_quantity=proposal.quantity,
                filled_quantity=quantity,
                notional=quantity * proposal.reference_price,
                order_id=f"ord-{proposal.proposal_id}",
                state=state,
            )
        )

    def held(self):
        return ladder_position(self.audit, "BTC-USD").held


def only(result) -> Proposal:
    [proposal] = result.proposals
    return proposal


class TestBuys:
    def test_a_dip_above_the_trend_proposes_its_step_at_the_ask(self, config):
        h = Harness(config, RISE + [127, 123])  # 123 is 5.4% under the 130 anchor
        proposal = only(h.analyze())
        assert proposal.side is Side.BUY
        assert proposal.reference_price == h.market().quotes["BTC-USD"].ask
        assert proposal.notional == D("5.00")  # the $5 step
        assert proposal.reason.startswith("dip: closed 123.00, 5.4% under the anchor 130.00")
        assert proposal.risk.passed, [f.message for f in proposal.risk.blocking_failures]
        assert proposal.plan.style == "PROMPT" and len(proposal.plan.tranches) == 1
        detail = proposal.sizing_detail["ladder"]
        assert (detail["rule"], detail["step"], detail["anchor"]) == ("dip", 0, "130")

    def test_no_buy_at_or_under_the_trend_average(self, config):
        h = Harness(config, RISE + [118, 106, 100])  # every step's depth, but falling
        result = h.analyze()
        assert result.proposals == []
        assert "at or under its 1-day average" in result.outcomes[0].skipped_reason

    def test_no_buy_before_the_average_exists(self, config):
        h = Harness(config, [100, 101, 95])  # 3 bars; the average needs 24
        result = h.analyze()
        assert result.proposals == []
        assert "needs 24 bars and 3 are on hand" in result.outcomes[0].skipped_reason

    def test_a_filled_step_is_not_proposed_again_in_the_cycle(self, config):
        h = Harness(config, RISE + [127, 123])
        h.fill(only(h.analyze()))
        h.extend([122])
        assert h.analyze().proposals == []

    def test_an_open_order_holds_its_step_until_it_ends(self, config):
        h = Harness(config, RISE + [127, 123])
        buy = only(h.analyze())
        h.fill(buy, quantity=0, state="confirmed")  # placed, not filled
        h.extend([122])
        assert h.analyze().proposals == []
        h.fill(buy, quantity=0, state="canceled")  # ended unfilled: the step is free
        h.extend([122.5])
        assert only(h.analyze()).sizing_detail["ladder"]["step"] == 0

    def test_nothing_to_do_says_where_the_next_orders_trigger(self, config):
        h = Harness(config, RISE)
        reason = h.analyze().outcomes[0].skipped_reason
        assert reason.startswith("no order this bar: closed 130.00 (+0.0% from the anchor")
        assert "step 1 buys at 123.50" in reason
        assert "above its 1-day average" in reason

    def test_the_anchor_starts_at_anchor_since(self, config):
        later = replace(config, strategy=replace(config.strategy, anchor_since=None))
        h = Harness(later, RISE + [127, 123])
        since = h.candles[-3].end  # after the 130 high: the anchor is 127
        dated = replace(later, strategy=replace(later.strategy, anchor_since=since))
        result = Agent(dated, store=h.store, audit=h.audit).analyze(
            h.market(), symbols=["BTC-USD"]
        )
        assert result.proposals == []  # 123 is only 3.1% under 127


class TestSells:
    def bought(self, config, *more):
        h = Harness(config, RISE + [127, 123])
        h.fill(only(h.analyze()))
        return h.extend(list(more))

    def test_a_take_profit_sells_the_steps_dollars_at_the_bid(self, config):
        h = self.bought(config, 128, 133, 137)  # +5.4% over the 130 anchor
        proposal = only(h.analyze(held=h.held()))
        assert proposal.side is Side.SELL
        assert proposal.reference_price == h.market().quotes["BTC-USD"].bid
        assert proposal.notional == D("5.00")
        assert proposal.reason.startswith("take profit: closed 137.00, 5.4% over the anchor")
        assert proposal.risk.passed

    def test_the_trend_exit_sells_everything_the_ladder_holds(self, config):
        h = self.bought(config, 118, 110)
        proposal = only(h.analyze(held=h.held()))
        assert proposal.side is Side.SELL and proposal.quantity == h.held()
        assert proposal.reason.startswith("trend exit: closed 110.00, at or under its 1-day")
        assert proposal.sizing_detail["ladder"]["entry_proposal_ids"]

    def test_a_sell_passes_the_caps_on_new_exposure_and_says_so(self, config):
        h = self.bought(config, 118, 110)
        strict = replace(
            config,
            risk=replace(
                config.risk, max_notional_per_trade_usd=D("1"), max_daily_notional_usd=D("1")
            ),
        )
        result = Agent(strict, store=h.store, audit=h.audit).analyze(
            h.market(held=h.held()), symbols=["BTC-USD"]
        )
        proposal = only(result)
        assert proposal.risk.passed, [f.message for f in proposal.risk.blocking_failures]
        exempted = {
            f.rule for f in proposal.risk.findings if "not applied to a trend exit" in f.message
        }
        assert exempted == {"per_trade_notional", "daily_notional"}

    def test_the_kill_switch_still_blocks_a_sell(self, config):
        h = self.bought(config, 118, 110)
        KillSwitch(config.kill_switch_path).engage("manual")
        proposal = only(h.analyze(held=h.held()))
        assert "kill_switch" in [f.rule for f in proposal.risk.blocking_failures]

    def test_the_sale_is_capped_at_what_the_account_holds(self, config):
        h = self.bought(config, 118, 110)
        half = (h.held() / 2).quantize(D("0.00000001"))
        assert only(h.analyze(held=half)).quantity == half

    def test_a_coin_sold_elsewhere_is_not_chased(self, config):
        h = self.bought(config, 118, 110)
        result = h.analyze(held=None, as_of=utcnow() + timedelta(minutes=1))
        assert result.proposals == []
        assert "sold outside the agent" in result.outcomes[0].skipped_reason

    def test_a_stale_snapshot_still_proposes_and_coverage_says_why(self, config):
        h = self.bought(config, 118, 110)
        proposal = only(h.analyze(held=None, as_of=utcnow() - timedelta(hours=1)))
        assert [f.rule for f in proposal.risk.blocking_failures] == ["sell_coverage"]

    def test_an_open_sell_blocks_another(self, config):
        h = self.bought(config, 118, 110)
        h.fill(only(h.analyze(held=h.held())), quantity=0, state="confirmed")
        h.extend([108])
        result = h.analyze(held=h.held())
        assert result.proposals == []
        assert "a ladder sell order is still open" in result.outcomes[0].skipped_reason

    def test_a_coin_bought_outside_the_agent_is_never_sold(self, config):
        h = Harness(config, RISE + [118, 110])
        result = h.analyze(held="5")
        assert result.proposals == []  # the ladder holds none, so no trend exit


class TestSkips:
    def test_no_history(self, config):
        result = Agent(config, store=PriceStore(config.price_store_path)).analyze(
            Harness(config).market(close=100), symbols=["BTC-USD"]
        )
        assert "no price history" in result.outcomes[0].skipped_reason
        assert "historicals" in result.outcomes[0].skipped_reason

    def test_no_quote(self, config):
        h = Harness(config, RISE)
        result = h.agent.analyze(MarketState(quotes={}), symbols=["BTC-USD"])
        assert "no live quote" in result.outcomes[0].skipped_reason

    def test_off_the_watchlist(self, config):
        h = Harness(config, RISE)
        result = h.agent.analyze(h.market(), symbols=["DOGE-USD"])
        assert result.proposals == [] and "allowlist" in result.outcomes[0].skipped_reason


class TestRecording:
    def test_proposals_are_recorded_as_the_ladders_and_are_not_hit_rate_scored(self, config):
        h = Harness(config, RISE + [127, 123])
        proposal = only(h.analyze())
        [record] = list(h.audit.events(kind=KIND_PROPOSAL))
        assert record["proposal_id"] == proposal.proposal_id
        assert (record["strategy"], record["rule"], record["step"]) == ("ladder", "dip", 0)
        assert record["trigger_reason"] == proposal.reason
        assert record["status"] == ProposalStatus.PROPOSED.value
        assert outcome_from_proposal_record(record, h.candles) is None

    def test_blocked_proposals_are_recorded_too(self, config):
        h = Harness(config, RISE + [127, 123])
        KillSwitch(config.kill_switch_path).engage("manual")
        h.analyze()
        [record] = list(h.audit.events(kind=KIND_PROPOSAL))
        assert record["risk_passed"] is False

    def test_no_record_leaves_the_audit_log_untouched(self, config):
        h = Harness(config, RISE + [127, 123])
        h.agent.analyze(h.market(), symbols=["BTC-USD"], record=False)
        assert list(h.audit.events(kind=KIND_PROPOSAL)) == []


SWINGS = piecewise(
    [(0, 100), (40, 160), (44, 151), (52, 170), (56, 180), (60, 168), (64, 150), (70, 138),
     (90, 130), (120, 190), (125, 178), (128, 168), (140, 205), (150, 250)]
)


def test_live_trades_exactly_what_the_backtest_trades(config, monkeypatch):
    """The same bars through ``rhca run``'s path and through ``rhca backtest``.

    Each bar, the live pipeline analyzes, and whatever it proposes fills at
    once, recorded at the bar's close. The backtest fills at the close too,
    crossing the same spread. The trades must match: bar, side and quantity.
    """
    candles = make_candles(SWINGS)
    trend = backtest.above_trend(candles, config.strategy.trend_bars)
    replay = backtest.run_ladder(
        candles,
        mode="anchor",
        trend=trend,
        trend_exit=True,
        round_trip_pct=HALF * 2 * 100,
    )
    kinds = {(f.side, f.quantity > 0) for f in replay.fills}
    assert kinds == {(Side.BUY, True), (Side.SELL, True)} and len(replay.fills) >= 8

    clock: list[datetime] = [candles[0].end]
    monkeypatch.setattr("robinhood_crypto_agent.audit.utcnow", lambda: clock[0])
    monkeypatch.setattr("robinhood_crypto_agent.agent.utcnow", lambda: clock[0])
    h = Harness(config)
    live = []
    for candle in candles:
        clock[0] = candle.end
        h.extend([candle.close])
        for proposal in h.analyze(held=h.held()).proposals:
            assert proposal.risk.passed, [f.message for f in proposal.risk.blocking_failures]
            h.fill(proposal)
            live.append((candle.end, proposal.side, proposal.quantity))

    expected = [(f.at, f.side, f.quantity) for f in replay.fills]
    assert [(at, side) for at, side, _ in live] == [(at, side) for at, side, _ in expected]
    for (_, _, got), (_, _, want) in zip(live, expected, strict=True):
        assert abs(got - want) < D("0.0000001")
