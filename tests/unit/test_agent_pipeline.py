"""The analysis pipeline: the split, decided live, on daily closes.

What these pin down: each rule's order is proposed at the price the backtest
fills at and sized the way the backtest sizes it; an order already working
holds its place; the per-coin limit trims a breakout and makes a tranche
wait, short-term first; every coin without an order says why; and -- the
one that matters most -- that live proposals, filled, make exactly the trades
``run_split`` makes on the same bars.
"""

import random
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from robinhood_crypto_agent import portfolio_backtest as pb
from robinhood_crypto_agent.agent import Agent, MarketState
from robinhood_crypto_agent.audit import KIND_PROPOSAL, AuditLog
from robinhood_crypto_agent.config import AgentConfig, RiskLimits
from robinhood_crypto_agent.daily import DailyBars
from robinhood_crypto_agent.execution.kill_switch import KillSwitch
from robinhood_crypto_agent.ledger import (
    LONG_TERM,
    RULE_ENTRY,
    RULE_STOP,
    RULE_TRANCHE,
    SHORT_TERM,
    Sleeve,
    SplitBook,
)
from robinhood_crypto_agent.models import ExecutionRecord, Position, Quote, Side, utcnow
from robinhood_crypto_agent.outcomes import outcome_from_proposal_record
from robinhood_crypto_agent.strategy.breakout import Breakout, DayView, day_views
from robinhood_crypto_agent.strategy.hodl import Accumulate
from robinhood_crypto_agent.strategy.split import CoinDay, decide
from tests.conftest import FakeCoinbaseDaily, daily_candles

D = Decimal
DAY = timedelta(days=1)
#: Decisions are made on the close of LAST, a few hours after it.
LAST = datetime(2026, 5, 31, tzinfo=timezone.utc)
NOW = LAST + DAY + timedelta(hours=6)
HALF = D("0.0095")  # half the 1.9% round trip

RISING = [50000 * 1.001**i for i in range(239)]
BREAKOUT = RISING + [RISING[-1] * 1.05]  # a 5% close over its 20-day high
FLAT = [3000] * 240
DIP = [100] * 230 + [90] * 10  # ten closes under the 200-day average
BASE = [50000 * 1.001**i for i in range(230)]
ENTRY = BASE[-1] * 1.06
STOP_PATH = BASE + [ENTRY, ENTRY * 1.01, ENTRY * 1.02, ENTRY * 1.03, ENTRY * 0.87]


class Harness:
    def __init__(self, tmp_path, paths, *, risk=None, watchlist=("BTC-USD", "ETH-USD")):
        self.now = NOW
        self.coinbase = FakeCoinbaseDaily(paths, last=LAST, clock=lambda: self.now)
        self.config = AgentConfig(
            watchlist=watchlist,
            rhs_account_number="123456789",
            data_dir=tmp_path / "data",
            risk=risk or RiskLimits(
                min_notional_per_trade_usd=D(5),
                max_spread_pct=D(2),
                max_position_pct_of_portfolio=D(20),
            ),
        )
        self.audit = AuditLog(self.config.audit_path)

    def agent(self):
        clock = lambda: self.now  # noqa: E731
        return Agent(
            self.config,
            audit=self.audit,
            daily=DailyBars(self.config.data_dir / "daily", fetch=self.coinbase, now=clock),
            now=clock,
        )

    def closes(self, symbol):
        return {b.start: b.close for b in self.coinbase.bars[symbol]}

    def state(self, *, positions=None, portfolio=None, spread=HALF, **overrides):
        day = self.now - DAY - timedelta(hours=self.now.hour)
        quotes = {}
        for symbol in self.config.watchlist:
            close = self.closes(symbol).get(day.replace(hour=0))
            if close is not None:
                quotes[symbol] = Quote(symbol, close * (1 - spread), close * (1 + spread),
                                       close, utcnow())
        quotes.update(overrides)
        return MarketState(
            quotes=quotes,
            positions={s: Position(s, q) for s, q in (positions or {}).items()},
            portfolio_value=portfolio,
        )

    def analyze(self, **state):
        return self.agent().analyze(self.state(**state))

    def fill(self, proposal, *, state="filled"):
        filled = proposal.quantity if state == "filled" else D(0)
        self.audit.record_execution(
            ExecutionRecord(
                proposal_id=proposal.proposal_id,
                symbol=proposal.symbol,
                side=proposal.side,
                recorded_at=utcnow(),
                requested_quantity=proposal.quantity,
                filled_quantity=filled,
                notional=filled * proposal.reference_price,
                order_id=f"ord-{proposal.proposal_id}",
                state=state,
            )
        )


def detail(proposal):
    return proposal.sizing_detail["split"]


def only(result, symbol="BTC-USD"):
    return [p for p in result.proposals if p.symbol == symbol]


def reason(result, symbol):
    [outcome] = [o for o in result.outcomes if o.symbol == symbol]
    return outcome.skipped_reason or ""


class TestEntries:
    def test_a_breakout_is_bought_at_the_ask_sized_by_risk_and_capped(self, tmp_path):
        h = Harness(tmp_path, {"BTC-USD": BREAKOUT, "ETH-USD": FLAT})
        result = h.analyze()
        [buy] = only(result)
        close = D(str(BREAKOUT[-1]))
        assert buy.side is Side.BUY and buy.reference_price == close * (1 + HALF)
        assert (detail(buy)["sleeve"], detail(buy)["rule"]) == (SHORT_TERM, RULE_ENTRY)
        assert detail(buy)["day"] == LAST.isoformat()
        # 1% of the $250 sleeve at risk asks for more than 10% of it: capped at $25.
        assert buy.notional == pytest.approx(D(25), abs=D("0.01"))
        assert buy.reason.startswith("breakout entry: closed")
        assert buy.plan.tranches[0].target_price == pytest.approx(buy.reference_price, abs=D("0.01"))
        assert buy.risk.passed

    def test_no_breakout_no_entry_and_the_report_says_why(self, tmp_path):
        h = Harness(tmp_path, {"BTC-USD": BREAKOUT, "ETH-USD": FLAT})
        result = h.analyze()
        assert only(result, "ETH-USD") == []
        text = reason(result, "ETH-USD")
        assert "short-term: no breakout" in text and "prior 20-day high" in text
        assert "long-term: 0 of 10 tranches bought; waits for a close under" in text

    def test_an_open_entry_order_holds_its_place(self, tmp_path):
        h = Harness(tmp_path, {"BTC-USD": BREAKOUT, "ETH-USD": FLAT})
        [buy] = only(h.analyze())
        h.fill(buy, state="confirmed")  # placed, not filled yet
        result = h.analyze()
        assert only(result) == []
        assert "an entry order is still open" in reason(result, "BTC-USD")
        h.fill(buy, state="canceled")  # ended unfilled: the place is free again
        assert len(only(h.analyze())) == 1


class TestStops:
    def entered(self, tmp_path):
        h = Harness(tmp_path, {"BTC-USD": STOP_PATH, "ETH-USD": FLAT})
        h.now = NOW - 4 * DAY  # the entry close
        [buy] = only(h.analyze())
        h.fill(buy)
        return h, buy

    def test_the_stop_sells_everything_the_sleeve_holds_at_the_bid(self, tmp_path):
        h, buy = self.entered(tmp_path)
        h.now = NOW
        # A per-trade cap under the sale's size: a stop is exempt from it.
        h.config = replace(h.config, risk=replace(h.config.risk, max_notional_per_trade_usd=D(10)))
        result = h.analyze(positions={"BTC-USD": buy.quantity})
        [sell] = only(result)
        assert sell.side is Side.SELL and sell.quantity == buy.quantity
        assert (detail(sell)["sleeve"], detail(sell)["rule"]) == (SHORT_TERM, RULE_STOP)
        close = D(str(STOP_PATH[-1]))
        assert sell.reference_price == close * (1 - HALF)
        assert D(detail(sell)["highest"]) == D(str(STOP_PATH[-2]))
        assert sell.reason.startswith("breakout stop: closed")
        findings = {f.rule: f for f in sell.risk.findings}
        assert findings["per_trade_notional"].passed
        assert "not applied to a breakout stop" in findings["per_trade_notional"].message
        assert sell.risk.passed

    def test_while_above_the_stop_the_report_says_where_it_is(self, tmp_path):
        h, buy = self.entered(tmp_path)
        h.now = NOW - DAY
        result = h.analyze(positions={"BTC-USD": buy.quantity})
        assert only(result) == []
        assert "short-term: holding" in reason(result, "BTC-USD")
        assert "stop at" in reason(result, "BTC-USD")

    def test_the_kill_switch_still_blocks_a_stop(self, tmp_path):
        h, buy = self.entered(tmp_path)
        h.now = NOW
        KillSwitch(h.config.kill_switch_path).engage("testing")
        [sell] = only(h.analyze(positions={"BTC-USD": buy.quantity}))
        assert not sell.risk.passed
        assert "kill_switch" in [f.rule for f in sell.risk.blocking_failures]

    def test_a_coin_sold_elsewhere_is_not_chased(self, tmp_path):
        h, _ = self.entered(tmp_path)
        h.now = NOW
        state = h.state(positions={})
        state.positions_as_of = utcnow() + timedelta(minutes=1)
        result = h.agent().analyze(state)
        assert only(result) == []
        assert "sold outside the agent" in reason(result, "BTC-USD")


class TestTranches:
    def test_a_close_under_the_200_day_average_buys_a_tranche(self, tmp_path):
        h = Harness(tmp_path, {"BTC-USD": FLAT, "ETH-USD": DIP})
        [buy] = only(h.analyze(), "ETH-USD")
        assert (detail(buy)["sleeve"], detail(buy)["rule"]) == (LONG_TERM, RULE_TRANCHE)
        # $250 over the two watchlist coins, in ten tranches.
        assert buy.notional == pytest.approx(D("12.50"), abs=D("0.01"))
        assert "tranche 1 of 10" in buy.reason and "under its 200-day average" in buy.reason

    def test_the_next_tranche_waits_a_week(self, tmp_path):
        h = Harness(tmp_path, {"BTC-USD": FLAT, "ETH-USD": DIP})
        h.now = NOW - 5 * DAY
        [buy] = only(h.analyze(), "ETH-USD")
        h.fill(buy)
        h.now = NOW
        result = h.analyze()
        assert only(result, "ETH-USD") == []
        assert "1 of 10 tranches bought; the next waits a week" in reason(result, "ETH-USD")
        h.now = NOW + 2 * DAY  # a week on, still under the average
        h.coinbase.bars["ETH-USD"] += daily_candles([90, 90], symbol="ETH-USD", last=LAST + 2 * DAY)
        h.coinbase.bars["BTC-USD"] += daily_candles([3000, 3000], symbol="BTC-USD",
                                                    last=LAST + 2 * DAY)
        [second] = only(h.analyze(), "ETH-USD")
        assert "tranche 2 of 10" in second.reason


class TestCoinLimit:
    def test_an_entry_is_trimmed_to_the_room_under_the_limit(self, tmp_path):
        h = Harness(tmp_path, {"BTC-USD": BREAKOUT, "ETH-USD": FLAT})
        close = D(str(BREAKOUT[-1]))
        held = D(90) / close  # $90 of BTC already in a $500 account: $10 of room at 20%
        [buy] = only(h.analyze(positions={"BTC-USD": held}, portfolio=D(500)))
        assert buy.notional == pytest.approx(D(10), abs=D("0.01"))
        assert "trimmed from $25.00 to the per-coin limit" in buy.reason
        assert buy.risk.passed

    def test_an_entry_with_no_room_is_turned_away(self, tmp_path):
        h = Harness(tmp_path, {"BTC-USD": BREAKOUT, "ETH-USD": FLAT})
        held = D(97) / D(str(BREAKOUT[-1]))
        result = h.analyze(positions={"BTC-USD": held}, portfolio=D(500))
        assert only(result) == []
        assert "turned away" in reason(result, "BTC-USD")

    def test_the_short_term_gets_the_room_first(self):
        view = DayView(LAST, D(110), prior_high=D(100), average=D(100), atr=D(2))
        coin = CoinDay("BTC-USD", LAST, D(110), view, average=D(120), highest=None,
                       bid=D(110), account_holding=D(70))
        book = SplitBook(Sleeve(LONG_TERM, D(250)), Sleeve(SHORT_TERM, D(250)))
        decision = decide(
            {"BTC-USD": coin}, book, Breakout(), Accumulate(), min_trade=D(5),
            coin_cap_pct=D(20), account_value=D(500), long_symbols=["BTC-USD", "ETH-USD"],
        )
        [entry] = decision.orders
        assert (entry.rule, entry.dollars) == (RULE_ENTRY, D(25))
        assert any("tranche 1 of 10 waits" in n for n in decision.notes["BTC-USD"])


class TestInputs:
    def test_coinbase_unreachable_is_explained(self, tmp_path):
        h = Harness(tmp_path, {"BTC-USD": BREAKOUT, "ETH-USD": FLAT})
        h.coinbase.fail = True
        result = h.analyze()
        assert result.proposals == []
        assert "no daily bars from Coinbase: Coinbase is unreachable" in reason(result, "BTC-USD")

    def test_a_close_not_yet_published_is_waited_for(self, tmp_path):
        h = Harness(tmp_path, {"BTC-USD": BREAKOUT, "ETH-USD": FLAT})
        h.now = NOW + DAY  # the next close is due, but the fake has none past LAST
        result = h.analyze()
        assert result.proposals == []
        assert "has not published the 2026-06-01 daily close yet" in reason(result, "BTC-USD")

    def test_an_order_without_a_quote_is_not_priced(self, tmp_path):
        h = Harness(tmp_path, {"BTC-USD": BREAKOUT, "ETH-USD": FLAT})
        state = h.state()
        del state.quotes["BTC-USD"]
        result = h.agent().analyze(state)
        assert result.proposals == []
        assert "no live quote to price them" in reason(result, "BTC-USD")

    def test_off_the_watchlist(self, tmp_path):
        h = Harness(tmp_path, {"BTC-USD": BREAKOUT, "ETH-USD": FLAT})
        result = h.agent().analyze(h.state(), symbols=["DOGE-USD"])
        assert "not on the watchlist allowlist" in reason(result, "DOGE-USD")


class TestRecords:
    def test_proposals_are_recorded_as_the_splits_and_are_not_hit_rate_scored(self, tmp_path):
        h = Harness(tmp_path, {"BTC-USD": BREAKOUT, "ETH-USD": FLAT})
        [buy] = only(h.analyze())
        [record] = list(h.audit.events(kind=KIND_PROPOSAL))
        assert record["proposal_id"] == buy.proposal_id
        assert (record["strategy"], record["sleeve"], record["rule"]) == (
            "split", SHORT_TERM, RULE_ENTRY
        )
        assert record["trigger_reason"] == buy.reason
        assert outcome_from_proposal_record(record, []) is None

    def test_blocked_proposals_are_recorded_too(self, tmp_path):
        h = Harness(tmp_path, {"BTC-USD": BREAKOUT, "ETH-USD": FLAT})
        [buy] = only(h.analyze(spread=D("0.02")))  # a 4% spread
        assert not buy.risk.passed
        [record] = list(h.audit.events(kind=KIND_PROPOSAL))
        assert "spread" in record["risk_failures"]

    def test_no_record_leaves_the_audit_log_untouched(self, tmp_path):
        h = Harness(tmp_path, {"BTC-USD": BREAKOUT, "ETH-USD": FLAT})
        h.agent().analyze(h.state(), record=False)
        assert list(h.audit.events()) == []


def walk(seed, start, days, drift):
    rng = random.Random(seed)
    price, out = start, []
    for index in range(days):
        trend = drift * (1 if (index // 70) % 2 == 0 else -1.2)
        price *= 1 + trend + rng.gauss(0, 0.025)
        out.append(round(price, 2))
    return out


def test_live_trades_exactly_what_the_backtest_trades(tmp_path):
    """Day by day, filling every proposal at its price, the live path makes the
    same trades on the same days as ``run_split`` -- the evidence the paper
    test and the backtest describe what the agent actually proposes."""
    days = 360
    paths = {"BTC-USD": walk(3, 40000, days, 0.004), "ETH-USD": walk(8, 2500, days, 0.005)}
    h = Harness(tmp_path, paths)
    window = 150
    first = LAST - (window - 1) * DAY

    for offset in range(window):
        day = first + offset * DAY
        h.now = day + DAY + timedelta(hours=1)
        held = {}
        book = h.agent().book()
        for sleeve in (book.long, book.short):
            for symbol, holding in sleeve.holdings.items():
                held[symbol] = held.get(symbol, D(0)) + holding.quantity
        result = h.analyze(positions=held)
        for proposal in result.proposals:
            h.fill(proposal)

    live = []
    for record in h.audit.events(kind=KIND_PROPOSAL):
        if not any(e.get("proposal_id") == record["proposal_id"]
                   for e in h.audit.events(kind="execution")):
            continue
        live.append((record["symbol"], record["sleeve"], record["side"],
                     record["proposal"]["sizing_detail"]["split"]["day"]))

    bars = {s: daily_candles(c, symbol=s, last=LAST) for s, c in paths.items()}
    rule, plan = Breakout(), Accumulate()
    prepared = {s: pb.Prepared(b, day_views(b, rule)) for s, b in bars.items()}
    split = pb.run_split(prepared, rule, plan, capital=D(500), long_pct=D(50), start=first,
                         end=LAST + DAY, min_trade=D(5))
    expected = []
    for trade in split.short.trades:
        expected.append((trade.symbol, SHORT_TERM, "buy", trade.entry_day.isoformat()))
        if trade.exit_day is not None:
            expected.append((trade.symbol, SHORT_TERM, "sell", trade.exit_day.isoformat()))
    for trade in split.long.trades:
        expected.append((trade.symbol, LONG_TERM, "buy", trade.entry_day.isoformat()))

    assert sorted(live) == sorted(expected)
    assert any(side == "sell" for _, _, side, _ in expected)  # the path exercises a stop
    assert any(sleeve == LONG_TERM for _, sleeve, _, _ in expected)

    # And the money matches: each sleeve's cash as the ledger has it.
    book = h.agent().book()
    assert book.long.cash == pytest.approx(
        split.long.capital - sum(t.cost for t in split.long.trades), abs=D("0.05")
    )


def test_config_split_settings_reach_the_agent(tmp_path):
    h = Harness(tmp_path, {"BTC-USD": FLAT, "ETH-USD": DIP})
    h.config = replace(
        h.config, strategy=replace(h.config.strategy, split_long_pct=D(20), split_long_mode="dca")
    )
    [buy] = only(h.analyze(), "ETH-USD")
    assert "long-term weekly DCA" in buy.reason
    # $100 over two coins is $50 a coin: ten $5 tranches.
    assert buy.notional == pytest.approx(D(5), abs=D("0.01"))
