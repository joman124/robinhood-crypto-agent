"""The candidate rules of docs/candidates.md, and the engine that scores them.

Synthetic series pin down mechanics only: when each rule enters and exits,
how the engine fills taker and resting orders, and how each check reads its
numbers. Whether any candidate has an edge on real prices is what
`rhca backtest-candidates` on Coinbase bars is for.
"""

import json
import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from robinhood_crypto_agent import candidate_backtest as cb
from robinhood_crypto_agent import portfolio_backtest as pb
from robinhood_crypto_agent.cli import EXIT_ERROR, EXIT_OK, main
from robinhood_crypto_agent.models import Candle
from robinhood_crypto_agent.strategy import candidates as cm
from robinhood_crypto_agent.strategy.breakout import Breakout
from robinhood_crypto_agent.strategy.candidates import (
    DailyBreakout,
    DayBarView,
    DayBreakout,
    DipInUptrend,
    DipView,
    FourHourBreakout,
    Held,
    Hours,
)

D = Decimal
T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
HOUR = timedelta(hours=1)
DAY = timedelta(days=1)


def bar(index, close, *, low=None, high=None, open_=None, symbol="BTC-USD", start=T0):
    close = D(str(close))
    begin = start + HOUR * index
    return Candle(
        symbol, begin, begin + HOUR,
        D(str(open_)) if open_ is not None else close,
        D(str(high)) if high is not None else close * D("1.001"),
        D(str(low)) if low is not None else close * D("0.999"),
        close, 4,
    )


def hourly(closes, *, symbol="BTC-USD", start=T0):
    return [bar(i, c, symbol=symbol, start=start) for i, c in enumerate(closes)]


def walk(hours, *, seed, symbol="BTC-USD", drift=0.0002):
    """A seeded random walk that trends up and down in 30-day spells."""
    rng = random.Random(seed)
    price, out = 100.0, []
    for i in range(hours):
        before = price
        spell = drift if (i // 720) % 2 == 0 else -drift
        price *= 1 + spell + rng.gauss(0, 0.008)
        high = max(before, price) * (1 + abs(rng.gauss(0, 0.003)))
        low = min(before, price) * (1 - abs(rng.gauss(0, 0.003)))
        begin = T0 + HOUR * i
        q = lambda x: D(f"{x:.6f}")  # noqa: E731
        out.append(Candle(symbol, begin, begin + HOUR, q(before), q(high), q(low), q(price), 4))
    return out


@dataclass(frozen=True)
class View:
    start: datetime
    close: Decimal


@dataclass(frozen=True)
class Scripted:
    """Enters on the bars named, sized at 10% of equity with its stop 10% under;
    exits after ``hold`` bars."""

    entries: frozenset = frozenset()
    hold: int = 2
    stop: Decimal = D("0.1")
    interval_minutes: int = 60
    warmup_days: int = 0
    name: str = "scripted"

    def prepare(self, hourly_bars):
        return list(hourly_bars), {b.start: View(b.start, b.close) for b in hourly_bars}

    def enters(self, view):
        return view.start in self.entries

    def stop_fraction(self, view):
        return self.stop

    def position_dollars(self, equity, view):
        return equity * D("0.1")

    def exits(self, held, view):
        return held.bars >= self.hold

    def describe(self):
        return "scripted"


def market(rule, *paths):
    return cb.Market.build(rule, {p[0].symbol: p for p in paths})


# -- the engine --------------------------------------------------------------


class TestEngine:
    def test_a_taker_crosses_half_the_round_trip_each_way(self):
        rule = Scripted(frozenset({T0 + HOUR}))
        bars = hourly([100, 100, 105, 110, 110])
        result = cb.run(market(rule, bars), costs=cb.Costs(D("2")))
        (trade,) = result.trades
        quantity = D("50") / (D("100") * D("1.01"))
        assert trade.cost == D("50")  # 10% of $500
        assert trade.quantity == quantity
        assert (trade.entry_day, trade.exit_day) == (T0 + HOUR, T0 + 3 * HOUR)
        assert trade.proceeds == quantity * D("110") * D("0.99")
        assert trade.risk == D("5")  # $50 with the stop 10% under

    def test_a_resting_entry_fills_at_its_limit_on_a_bar_that_trades_below_it(self):
        rule = Scripted(frozenset({T0 + HOUR}))
        bars = [bar(0, 100), bar(1, 100, low=99.9), bar(2, 101, low="99.99"), bar(3, 102),
                bar(4, 103), bar(5, 103)]
        costs = cb.Costs(D("2"), maker_pct=D("0.1"))
        result = cb.run(market(rule, bars), costs=costs)
        (trade,) = result.trades
        assert trade.entry_day == T0 + 2 * HOUR  # rested a bar, filled on the next
        assert trade.quantity == D("50") / (D("100") * D("1.001"))
        assert trade.exit_day == T0 + 4 * HOUR  # two bars after the fill
        assert trade.proceeds == trade.quantity * D("103") * D("0.99")  # exits are taker
        assert result.missed == 0

    def test_touching_the_limit_is_not_a_fill_and_a_resting_entry_expires(self):
        rule = Scripted(frozenset({T0 + HOUR}))
        bars = [bar(0, 100), bar(1, 100), bar(2, 101, low=100), bar(3, 102, low="100.5"),
                bar(4, 103, low=101), bar(5, 90, low=80)]
        costs = cb.Costs(D("2"), maker_pct=D("0.1"), maker_bars=3)
        result = cb.run(market(rule, bars), costs=costs)
        assert result.trades == [] and result.missed == 1
        assert result.final_equity == D("500")  # the reserved cash came back

    def test_a_resting_entry_counts_as_cash_while_it_waits(self):
        rule = Scripted(frozenset({T0 + HOUR}))
        bars = [bar(0, 100), bar(1, 100), bar(2, 101, low=101)]
        result = cb.run(market(rule, bars), costs=cb.Costs(D("2"), maker_pct=D("0.1")))
        assert [e for _, e in result.equity_curve] == [D("500")] * 3
        assert result.exposure[-1] == 0

    def test_the_cost_filter_turns_away_a_stop_too_close_to_pay_for_itself(self):
        rule = Scripted(frozenset({T0 + HOUR}), stop=D("0.05"))
        bars = hourly([100, 100, 105, 110, 110])
        kept = cb.run(market(rule, bars), costs=cb.Costs(D("1")), min_stop_trips=D("4"))
        cut = cb.run(market(rule, bars), costs=cb.Costs(D("2")), min_stop_trips=D("4"))
        assert len(kept.trades) == 1 and kept.filtered == 0  # 5% >= 4 x 1%
        assert cut.trades == [] and cut.filtered == 1  # 5% < 4 x 2%

    def test_a_window_trades_only_its_own_bars(self):
        rule = Scripted(frozenset({T0 + HOUR, T0 + 5 * HOUR}))
        bars = hourly([100] * 10)
        result = cb.run(market(rule, bars), costs=cb.Costs(D("1")), start=T0 + 3 * HOUR,
                        end=T0 + 9 * HOUR)
        assert [t for t, _ in result.equity_curve] == [T0 + HOUR * i for i in range(3, 9)]
        assert [t.entry_day for t in result.trades] == [T0 + 5 * HOUR]

    def test_sleeves_share_the_account_equally(self):
        early, late = Scripted(frozenset({T0 + HOUR})), Scripted(frozenset({T0 + 2 * HOUR}))
        bars = hourly([100, 100, 100, 100, 100, 100])
        candidate = cm.Candidate("x", "two sleeves", (early, late), 1)
        result = cb.run_candidate(
            [market(early, bars), market(late, bars)], candidate, costs=cb.Costs(D("0"))
        )
        assert result.capital == D("500")
        assert sorted(t.cost for t in result.trades) == [D("25"), D("25")]  # 10% of $250 each
        assert result.final_equity == D("500")

    def test_it_makes_the_live_breakouts_trades_on_daily_bars(self):
        rule = Breakout(breakout_days=5, regime_days=10, atr_days=5, max_weight_pct=D("50"))
        series = {
            "BTC-USD": walk(24 * 200, seed=1),
            "ETH-USD": walk(24 * 200, seed=2, symbol="ETH-USD"),
        }
        start = T0 + DAY * 60
        old = pb.run_breakout(pb.prepare(series, rule), rule, start=start)
        new = cb.run(cb.Market.build(DailyBreakout(rule), series), costs=cb.Costs(D("1.9")),
                     start=start)

        def key(trade):
            return (trade.symbol, trade.entry_day, trade.exit_day, trade.cost, trade.proceeds)

        assert len(old.trades) >= 4
        assert sorted(map(key, new.trades)) == sorted(map(key, old.trades))
        assert new.equity_curve == old.equity_curve


class TestCosts:
    def test_the_stressed_run_charges_half_as_much_again(self):
        costs = cb.Costs(D("1.9"), maker_pct=D("0.2")).stressed()
        assert (costs.round_trip_pct, costs.maker_pct) == (D("2.85"), D("0.30"))

    def test_nonsense_costs_are_refused(self):
        with pytest.raises(ValueError):
            cb.Costs(D("-1"))
        with pytest.raises(ValueError):
            cb.Costs(D("1"), maker_pct=D("0.1"), maker_bars=0)


# -- the rules ---------------------------------------------------------------


class TestBars:
    def test_resampling_aligns_to_midnight_and_drops_a_bar_still_forming(self):
        bars = cm.resample(hourly([1, 2, 3, 4, 5, 6, 7, 8, 9, 10]), 240)
        assert [(b.start, b.close, b.observations) for b in bars] == [
            (T0, D(4), 4), (T0 + 4 * HOUR, D(8), 4),  # 08:00-12:00 has not closed
        ]
        assert bars[0].open == D(1) and bars[0].high == D(4) * D("1.001")
        assert bars[0].low == D(1) * D("0.999")

    def test_a_bar_must_divide_the_day(self):
        with pytest.raises(ValueError):
            cm.resample([], 300)

    def test_the_daily_trend_is_the_previous_days_close_against_its_average(self):
        rising = hourly([100 + day for day in range(103) for _ in range(24)])
        trend = cm.daily_trend(rising)
        assert T0 + 99 * DAY not in trend  # day 98 has no 100-day average behind it
        assert trend[T0 + 100 * DAY] is True
        falling = hourly([300 - day for day in range(103) for _ in range(24)])
        assert cm.daily_trend(falling)[T0 + 100 * DAY] is False


def day_view(**kw):
    base = dict(start=T0 + 30 * HOUR, close=D("105"), day=T0 + DAY, day_open=D("100"),
                level=D("104"), trend_up=True, first_cross=True, last_of_day=False)
    base.update(kw)
    return DayBarView(**base)


def held(view, *, bars=0, highest=None):
    return Held("BTC-USD", D(1), view.start, view.close, D(50), D(5),
                highest if highest is not None else view.close, view, bars)


class TestDayBreakout:
    def test_the_level_is_the_open_plus_k_of_yesterdays_range_and_only_its_first_cross_counts(
        self,
    ):
        yesterday = [bar(i, 100, high=110, low=90) for i in range(24)]  # a 20-point range
        today = [bar(24 + i, c) for i, c in enumerate([100, 109, 111, 108, 112] + [100] * 19)]
        _, views = DayBreakout().prepare(yesterday + today)
        first = views[T0 + DAY]
        assert first.day_open == D(100) and first.level == D(110)
        assert first.first_cross is False
        assert [views[T0 + DAY + HOUR * i].first_cross for i in range(1, 5)] == [
            False, True, False, False,  # 111 crosses; 112 later does not count again
        ]
        assert views[T0 + DAY + 23 * HOUR].last_of_day is True

    def test_it_enters_the_first_cross_in_an_uptrend_never_on_the_last_bar(self):
        rule = DayBreakout()
        assert rule.enters(day_view())
        assert not rule.enters(day_view(trend_up=False))
        assert not rule.enters(day_view(first_cross=False))
        assert not rule.enters(day_view(last_of_day=True))

    def test_the_stop_is_the_days_open(self):
        rule = DayBreakout()
        entry = day_view()
        assert rule.stop_fraction(entry) == D(5) / D(105)
        assert rule.exits(held(entry), day_view(close=D("99.99"), first_cross=False))
        assert not rule.exits(held(entry), day_view(close=D(100), first_cross=False))

    def test_it_is_out_by_the_days_close(self):
        rule = DayBreakout()
        entry = day_view()
        assert rule.exits(held(entry), day_view(last_of_day=True))
        assert rule.exits(held(entry), day_view(day=T0 + 2 * DAY))


def dip_view(**kw):
    base = dict(start=T0, close=D("95"), mean=D("100"), deviation=D("2"), atr=D("1"),
                trend_up=True)
    base.update(kw)
    return DipView(**base)


class TestDipInUptrend:
    def test_it_enters_two_deviations_under_the_mean_in_an_uptrend(self):
        rule = DipInUptrend()
        assert rule.enters(dip_view())
        assert not rule.enters(dip_view(close=D(96)))  # exactly 2 deviations under
        assert not rule.enters(dip_view(trend_up=False))
        assert not rule.enters(dip_view(deviation=D(0)))

    def test_it_exits_at_the_mean_at_its_stop_or_on_time(self):
        rule = DipInUptrend()
        entry = dip_view()
        assert rule.stop_fraction(entry) == D(3) / D(95)
        assert not rule.exits(held(entry, bars=1), dip_view(close=D(97)))
        assert rule.exits(held(entry, bars=1), dip_view(close=D(100)))  # back to the mean
        assert rule.exits(held(entry, bars=1), dip_view(close=D("91.99")))  # 3 ATRs under
        assert rule.exits(held(entry, bars=24), dip_view(close=D(97)))

    def test_its_view_is_the_mean_and_population_deviation_of_the_last_bars(self):
        bars, views = DipInUptrend(bars=4).prepare(hourly([1, 2, 3, 4, 5]))
        view = views[T0 + 3 * HOUR]
        assert view.mean == D("2.5") and view.deviation == D("1.25").sqrt()
        assert T0 + 2 * HOUR not in views and len(bars) == 5


class TestFourHourBreakout:
    def test_its_view_reads_4h_closes(self):
        closes = [100] * 4 * 21 + [101, 102, 103, 120]
        _, views = FourHourBreakout().prepare(hourly(closes))
        last = views[T0 + 4 * HOUR * 21]
        assert last.close == D(120) and last.prior_high == D(100)
        assert last.trend_up is False  # no 100-day history behind it

    def test_it_trails_a_stop_and_holds_at_most_thirty_bars(self):
        rule = FourHourBreakout()
        view = cm.BarView(T0, D(100), D(90), D(2), True)
        assert rule.enters(view) and not rule.enters(cm.BarView(T0, D(100), D(90), D(2), False))
        assert rule.exits(held(view, highest=D(110)), cm.BarView(T0, D("103.9"), D(90), D(2), True))
        assert not rule.exits(held(view, highest=D(110)), cm.BarView(T0, D(104), D(90), D(2), True))
        assert rule.exits(held(view, bars=30), view)


class TestHours:
    def test_it_enters_only_inside_its_hours_and_exits_at_any(self):
        rule = Hours(DayBreakout())
        assert rule.enters(day_view(start=T0 + 13 * HOUR))
        assert rule.enters(day_view(start=T0 + 16 * HOUR))
        assert not rule.enters(day_view(start=T0 + 17 * HOUR))
        assert rule.exits(held(day_view()), day_view(start=T0 + 20 * HOUR, last_of_day=True))
        assert rule.name == "day breakout k=0.5, 13-17 UTC"


class TestRegistry:
    def test_the_candidates_are_the_documents(self):
        assert cm.CANDIDATE_KEYS == ("ref", "1", "2", "3", "4", "5", "6", "7a", "7b")
        by_key = {c.key: c for c in cm.CANDIDATES}
        assert by_key["ref"].sleeves == (DailyBreakout(Breakout()),)
        assert by_key["1"].min_stop_trips == D(4)
        assert len(by_key["2"].symbols) == 9 and by_key["2"].symbols[:4] == (
            "BTC-USD", "ETH-USD", "SOL-USD", "XRP-USD",
        )
        assert [r.rule.breakout_days for r in by_key["3"].sleeves] == [10, 20, 55]
        assert by_key["3"].neighbours == ()
        assert all(len(c.neighbours) == 2 for c in cm.CANDIDATES if c.key != "3")
        assert {c.min_trades for c in cm.CANDIDATES if c.key in ("4", "5", "6", "7a", "7b")} == {
            100
        }

    def test_select_keeps_the_documents_order_and_refuses_unknown_keys(self):
        assert [c.key for c in cm.select(["6", "ref"])] == ["ref", "6"]
        with pytest.raises(ValueError, match="unknown candidates"):
            cm.select(["8"])


# -- the checks --------------------------------------------------------------


def stats(**kw):
    base = dict(return_pct=D(10), max_drawdown_pct=D(5), return_over_drawdown=D(2), closed=40,
                open=0, win_rate=0.4, expectancy_r=D("0.5"), exposure_pct=D(10),
                pnl_by_symbol={})
    base.update(kw)
    return cb.Stats(**base)


DAILY = cm.Candidate("x", "x", (Scripted(),), 30)


class TestChecks:
    def test_edge_needs_trades_expectancy_and_a_stressed_profit(self):
        assert cb._edge(DAILY, stats(), stats()).passed
        assert not cb._edge(DAILY, stats(closed=29), stats()).passed
        assert not cb._edge(DAILY, stats(expectancy_r=D("0.2")), stats()).passed
        assert not cb._edge(DAILY, stats(), stats(return_pct=D(0))).passed

    def test_risk_needs_half_holds_drawdown_and_a_better_ratio(self):
        hold = stats(max_drawdown_pct=D(10), return_over_drawdown=D(1))
        assert cb._risk(stats(), hold).passed
        assert not cb._risk(stats(max_drawdown_pct=D("5.1")), hold).passed
        assert not cb._risk(stats(return_over_drawdown=D(1)), hold).passed

    def test_the_bad_year_needs_a_profit_or_half_holds_loss(self):
        assert cb._bad_year(stats(return_pct=D(1)), stats(return_pct=D(20))).passed
        assert cb._bad_year(stats(return_pct=D(-9)), stats(return_pct=D(-20))).passed
        assert not cb._bad_year(stats(return_pct=D(-11)), stats(return_pct=D(-20))).passed
        assert not cb._bad_year(stats(return_pct=D(-1)), stats(return_pct=D(20))).passed

    def test_consistency_needs_most_windows_and_a_bounded_worst(self):
        assert cb._consistency([1, 2, -3], [5, 5, -10]).passed
        assert not cb._consistency([1, -2, -3], [5, 5, -10]).passed  # 1 of 3
        assert not cb._consistency([1, 2, -6], [5, 5, -10]).passed  # worse than -5
        assert not cb._consistency([1, 2, -1], [5, 5, 1]).passed  # hold never lost
        assert cb._consistency([], []).passed is None

    def test_breadth_and_plateau(self):
        assert cb._breadth("SOL-USD", stats(return_pct=D(1))).passed
        assert not cb._breadth("SOL-USD", stats(return_pct=D(0))).passed
        assert cb._breadth(None, None).passed is None
        candidate = cm.Candidate("x", "x", (Scripted(),), 30, neighbours=(DAILY, DAILY))
        assert cb._plateau(candidate, [("a", stats()), ("b", stats())]).passed
        assert not cb._plateau(candidate, [("a", stats()), ("b", stats(expectancy_r=D(0)))]).passed
        assert cb._plateau(DAILY, []).passed  # no parameter to move

    def test_a_check_that_did_not_run_leaves_the_verdict_incomplete(self):
        def verdict(*passed):
            checks = [cb.Check(str(i), "c", p, "") for i, p in enumerate(passed)]
            return cb.Verdict(DAILY, cb.Costs(D(1)), [], stats(), stats(), stats(), checks)

        assert verdict(True, True).status == "PASS"
        assert verdict(True, None).status == "incomplete"
        assert verdict(False, None).status == "fail"


# -- the suite and the command -------------------------------------------------


def series_for(symbols, *, days):
    return {s: walk(24 * days, seed=i, symbol=s) for i, s in enumerate(symbols)}


class TestSuite:
    def test_every_candidate_gets_a_verdict_at_every_cost(self):
        series = series_for(["BTC-USD", "ETH-USD"], days=140)
        suite = cb.Suite(
            series, symbols=["BTC-USD", "ETH-USD"], start=T0 + 110 * DAY,
            window=timedelta(days=10), step=timedelta(days=10),
        )
        chosen = cm.select(["ref", "5", "6"])
        report = suite.run(chosen, cb.sweep([D("1.9"), D("0.2")], maker_pct=D("0.1")))
        assert sorted(report.verdicts) == ["5", "6", "ref"]
        for verdicts in report.verdicts.values():
            assert [v.costs.round_trip_pct for v in verdicts] == [D("1.9"), D("0.2")]
            assert [c.key for c in verdicts[0].checks] == ["1", "2", "3", "4", "5", "6"]
            assert verdicts[0].checks[3].passed is not None  # windows ran
        text = cb.render(report, verbose=True)
        assert "CANDIDATES AGAINST THEIR BAR" in text and "dip in uptrend" in text
        assert "hold (2 coins, equal weight)" in text
        payload = json.loads(json.dumps(cb.summary(report)))
        assert payload["maker_pct"] == "0.1"
        assert set(payload["candidates"]["5"]["by_round_trip"]) == {"1.9", "0.2"}

    def test_a_candidate_whose_coins_have_no_history_is_reported_not_run(self):
        series = series_for(["BTC-USD"], days=120)
        suite = cb.Suite(series, symbols=["BTC-USD"], start=T0 + 110 * DAY, window=None)
        report = suite.run(cm.select(["2"]), cb.sweep([D("1.9")]))
        assert report.missing["2"] == sorted(set(cm.BREADTH_SYMBOLS) - {"BTC-USD"})
        (verdict,) = report.verdicts["2"]
        assert verdict.symbols == ["BTC-USD"]
        assert verdict.status in ("incomplete", "fail")  # no windows, one coin


class TestCommand:
    @pytest.fixture
    def fetched(self, monkeypatch):
        calls = []

        def fake_history(config, symbol, *, days, refresh=False, interval=None):
            calls.append((symbol, days, interval))
            return walk(24 * 130, seed=len(calls), symbol=symbol)

        monkeypatch.setattr("robinhood_crypto_agent.cli.backtest_history", fake_history)
        return calls

    def args(self, tmp_path, *extra):
        return ["--data-dir", str(tmp_path / "data"), "backtest-candidates", "--days", "20",
                "--roll-window", "0", *extra]

    def test_it_fetches_hourly_bars_with_the_warmup_and_prints_the_matrix(
        self, tmp_path, fetched, capsys
    ):
        code = main(self.args(tmp_path, "--symbols", "BTC-USD", "--candidates", "ref,5",
                              "--round-trips", "1.9,0.2"))
        assert code == EXIT_OK
        # The day breakout's trend filter needs the longer warm-up of the two.
        assert cm.TREND_WARMUP_DAYS > Breakout().warmup_days + 2
        assert fetched == [("BTC-USD", 20 + cm.TREND_WARMUP_DAYS, 60)]
        out = capsys.readouterr().out
        assert "1.9% round trip" in out and "0.2% round trip" in out
        assert "day breakout" in out
        assert "4 consistency?" in out  # --roll-window 0: not run, never passed

    def test_breadth_fetches_its_own_coins(self, tmp_path, fetched, capsys):
        assert main(self.args(tmp_path, "--candidates", "2", "--json")) == EXIT_OK
        assert {s for s, _, _ in fetched} >= set(cm.BREADTH_SYMBOLS)
        payload = json.loads(capsys.readouterr().out)
        assert len(payload["candidates"]["2"]["by_round_trip"]["1.9"]["symbols"]) == 9

    def test_bad_arguments_are_refused(self, tmp_path, fetched):
        assert main(self.args(tmp_path, "--candidates", "9")) == EXIT_ERROR
        assert main(self.args(tmp_path, "--round-trips", "-1")) == EXIT_ERROR
        assert main(self.args(tmp_path, "--maker-pct", "0.1", "--maker-bars", "0")) == EXIT_ERROR
        assert fetched == []
