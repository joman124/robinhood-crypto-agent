"""The backtest: costs charged honestly, and the losses a win rate hides shown.

The series here are synthetic, so they pin down mechanics -- fills, the ladder
rules, the caps, the paging -- and say nothing about how either strategy does
on real prices. That is what ``rhca backtest`` on real history is for.
"""

import json
import math
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from robinhood_crypto_agent import backtest
from robinhood_crypto_agent.bootstrap import fetch_coinbase_history
from robinhood_crypto_agent.cli import EXIT_ERROR, EXIT_OK, backtest_history, main
from robinhood_crypto_agent.config import AgentConfig
from robinhood_crypto_agent.models import Side
from tests.conftest import make_candles

T0 = datetime(2026, 6, 1, tzinfo=timezone.utc)


def oscillating(n=600, swing=0.12, period=15):
    return [100 * (1 + swing * math.sin(i / period)) for i in range(n)]


def declining(n=600, to=0.4):
    return [100 * (to ** (i / (n - 1))) for i in range(n)]


@pytest.fixture
def config(tmp_path):
    return AgentConfig(watchlist=("BTC-USD",), data_dir=tmp_path / "data")


class TestBook:
    def test_every_fill_crosses_half_the_spread(self):
        book = backtest.Book(Decimal("1"))  # 1% each side
        book.buy(T0, 0, Decimal("100"), Decimal("101"))  # paid 101 each: 1 coin
        assert book.held == Decimal("1")
        book.sell(T0, Decimal("100"), Decimal("1"))  # got 99
        [trade] = book.closed
        assert (trade.entry, trade.exit) == (Decimal("101"), Decimal("99"))
        assert trade.pnl == Decimal("-2")  # a flat price still loses the round trip

    def test_sells_close_the_oldest_lots_first(self):
        book = backtest.Book(Decimal("0"))
        book.buy(T0, 0, Decimal("10"), Decimal("10"))
        book.buy(T0, 1, Decimal("20"), Decimal("20"))
        book.sell(T0, Decimal("30"), Decimal("1.5"))
        assert [t.entry for t in book.closed] == [Decimal("10"), Decimal("20")]
        assert book.held == Decimal("0.5")


class TestLadder:
    def test_per_lot_mode_sells_each_lot_its_own_step_up(self):
        result = backtest.run_ladder(make_candles(oscillating()), mode="lot")
        assert result.sells > 0 and result.closed
        assert result.closed_win_rate == 1.0
        # the $5 lot sells about 5% over what it was bought at, less the spread
        smallest = min(result.closed, key=lambda t: t.entry * t.quantity)
        assert smallest.return_pct > Decimal("2")

    def test_a_decline_hides_its_losses_from_the_closed_win_rate(self):
        """The trap: nothing is ever sold, so no trade ever closes at a loss."""
        result = backtest.run_ladder(make_candles(declining()), mode="lot")
        assert result.buys == 3 and result.sells == 0
        assert result.closed_win_rate is None  # no closed trade to count
        assert result.win_rate_with_open == 0.0  # every open lot underwater
        assert result.unrealized < Decimal("-15")  # of the $35 put in
        assert result.max_capital == Decimal("35")

    def test_anchor_mode_sells_everything_left_at_the_top_step(self):
        prices = [100, 94, 89, 79, 90, 106, 111, 121, 121]
        result = backtest.run_ladder(make_candles(prices), mode="anchor")
        assert result.buys == 3
        assert not result.open_lots  # the +20% step emptied the position

    def test_an_unknown_mode_is_refused(self):
        with pytest.raises(ValueError):
            backtest.run_ladder(make_candles([100, 101]), mode="grid")

    def test_steps_parse_and_validate(self):
        assert backtest.parse_ladder("10:10, 5:5") == (
            (Decimal("5"), Decimal("5")),
            (Decimal("10"), Decimal("10")),
        )
        with pytest.raises(ValueError):
            backtest.parse_ladder("5:-5")

    def test_every_fill_is_logged(self):
        prices = [100, 94, 89, 79, 90, 106, 111, 121, 121]
        result = backtest.run_ladder(make_candles(prices), mode="anchor")
        assert [f.side for f in result.fills] == [Side.BUY] * 3 + [Side.SELL] * 3
        assert len(result.fills) == result.buys + result.sells


def test_hold_is_the_baseline(config):
    result = backtest.run_hold(make_candles(declining()))
    assert result.buys == 1 and result.max_capital == backtest.HOLD_DOLLARS
    assert result.total < 0


def test_the_report_leads_with_what_a_win_rate_hides(config):
    candles = make_candles(declining(120))
    trend = backtest.above_trend(candles, 24)
    runs = {
        cost: {"BTC-USD": backtest.run_all(candles, round_trip_pct=cost, trend=trend, trend_days=1)}
        for cost in (Decimal("1.9"), Decimal("2.85"))
    }
    report = backtest.render_report(
        runs[Decimal("1.9")],
        runs[Decimal("2.85")],
        steps=backtest.DEFAULT_LADDER,
        round_trip_pct=Decimal("1.9"),
        config=config,
        trend_days=1,
    )
    assert "ladder (lot)" in report and "ladder (anchor) +1d exit" in report
    assert "trend +1d" in report and "hold" in report
    assert "Read total P&L, worst drawdown and 'win +open' first" in report
    assert "authorizes nothing" in report
    # The ladder is the retired rule: the report says what the agent trades now.
    assert "The agent no longer trades the ladder" in report
    assert "--strategies split" in report


class TestHistory:
    def fake_coinbase(self, *, total_bars):
        """Serves ``total_bars`` hourly candles ending now, 300 per request."""
        now = datetime(2026, 9, 26, 12, tzinfo=timezone.utc)
        starts = [now - timedelta(hours=h) for h in range(1, total_bars + 1)]
        calls = []

        def http(method, url):
            calls.append(url)
            query = dict(part.split("=") for part in url.split("?")[1].split("&"))
            lo = datetime.strptime(query["start"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            hi = datetime.strptime(query["end"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            rows = [
                [int(s.timestamp()), 99, 101, 100, 100.5, 1]
                for s in starts
                if lo <= s <= hi
            ]
            return rows[:300]

        return now, http, calls

    def test_history_pages_back_300_bars_at_a_time(self):
        now, http, calls = self.fake_coinbase(total_bars=2000)
        candles = fetch_coinbase_history(
            "BTC-USD", interval_minutes=60, days=30, http=http, now=now
        )
        assert len(calls) == 3  # 720 bars = 300 + 300 + 120
        assert len(candles) == 720
        assert candles == sorted(candles, key=lambda c: c.start)
        assert len({c.start for c in candles}) == len(candles)

    def test_paging_stops_where_the_exchange_has_nothing_older(self):
        now, http, calls = self.fake_coinbase(total_bars=400)
        candles = fetch_coinbase_history(
            "BTC-USD", interval_minutes=60, days=90, http=http, now=now
        )
        assert len(candles) == 400
        assert len(calls) == 3  # two pages of data, then an empty one

    def test_a_cached_rerun_matches_the_fresh_fetch(self, config, monkeypatch):
        """Cached bars must read as fully sampled, or the rerun disagrees."""
        fresh = make_candles(oscillating(300))
        fresh = [c for c in fresh]
        monkeypatch.setattr(
            "robinhood_crypto_agent.cli.fetch_coinbase_history",
            lambda symbol, interval_minutes, days: fresh,
        )
        first = backtest_history(config, "BTC-USD", days=10_000)
        monkeypatch.setattr(
            "robinhood_crypto_agent.cli.fetch_coinbase_history",
            lambda *a, **k: pytest.fail("the cache should have been used"),
        )
        second = backtest_history(config, "BTC-USD", days=10_000)
        assert [c.close for c in second] == [c.close for c in first]
        assert {c.observations for c in second} == {4}
        assert (config.data_dir / "backtest" / "BTC-USD-60m.json").exists()


def test_the_cli_replays_a_bars_file(tmp_path, capsys):
    bars = [
        {"start": (T0 + timedelta(hours=i)).isoformat(), "open": p, "high": p, "low": p, "close": p}
        for i, p in enumerate(oscillating(80))
    ]
    path = tmp_path / "bars.json"
    path.write_text(json.dumps(bars))
    args = ["--data-dir", str(tmp_path / "data"), "backtest", "--bars-file", str(path)]
    assert main([*args, "--symbols", "BTC-USD"]) == EXIT_OK
    assert "BTC-USD (80 bars)" in capsys.readouterr().out
    assert main([*args, "--symbols", "BTC-USD,ETH-USD"]) == EXIT_ERROR  # one file, one symbol
    assert main([*args, "--symbols", "BTC-USD", "--json"]) == EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert {r["strategy"] for r in payload["BTC-USD"]} == {
        "ladder (lot)",
        "ladder (anchor)",
        "ladder (lot) +50d",
        "ladder (anchor) +50d",
        "ladder (lot) +50d exit",
        "ladder (anchor) +50d exit",
        "trend +50d",
        "hold",
    }
    assert main([*args, "--symbols", "BTC-USD", "--strategies", "signal"]) == EXIT_ERROR


class TestTrendFilter:
    def test_the_average_includes_the_bar_and_waits_until_it_exists(self):
        candles = make_candles([10, 12, 11, 9, 14])
        above = backtest.above_trend(candles, 3)
        # no entry until 3 closes exist; then close vs mean of the last 3
        assert list(above.values()) == [11 > 11, 9 > Decimal("32") / 3, 14 > Decimal("34") / 3]
        assert candles[0].start not in above and candles[1].start not in above

    def test_bars_for_a_number_of_days(self):
        assert backtest.trend_bars(50, 60) == 1200
        assert backtest.trend_bars(1, 15) == 96

    def test_no_buy_on_a_bar_below_its_average(self):
        """A steady decline sits below its average on every bar: nothing is bought."""
        candles = make_candles(declining(400))
        trend = backtest.above_trend(candles, 48)
        assert not any(trend.values())
        assert backtest.run_ladder(candles, mode="lot").buys == 3
        assert backtest.run_ladder(candles, mode="lot", trend=trend).buys == 0

    def test_sells_are_unchanged_by_the_filter(self):
        """A lot bought while above the average still sells by the ladder's rules."""
        candles = make_candles([100, 101, 102, 103, 104, 98, 110, 111])
        trend = {c.start: True for c in candles[:6]}  # above until the buy, below after
        result = backtest.run_ladder(candles, mode="lot", trend=trend)
        assert result.buys == 1 and result.sells == 1

    def test_run_all_adds_filtered_ladders_alongside(self, config):
        candles = make_candles(oscillating(120))
        results = backtest.run_all(
            candles,
            trend=backtest.above_trend(candles, 24),
            trend_days=1,
        )
        assert [r.strategy for r in results] == [
            "ladder (lot)",
            "ladder (anchor)",
            "ladder (lot) +1d",
            "ladder (anchor) +1d",
            "ladder (lot) +1d exit",
            "ladder (anchor) +1d exit",
            "trend +1d",
            "hold",
        ]

    def test_without_a_trend_there_are_no_trend_rows(self, config):
        results = backtest.run_all(make_candles(oscillating(60)))
        assert [r.strategy for r in results] == ["ladder (lot)", "ladder (anchor)", "hold"]


class TestTrendExit:
    def test_a_close_under_the_average_sells_everything(self):
        """The filter's bag: a lot bought above the average, then a slide under it."""
        prices = [100 + i for i in range(30)] + [123, 120, 110, 100, 95, 90, 85]
        candles = make_candles(prices)
        trend = backtest.above_trend(candles, 24)
        held = backtest.run_ladder(candles, mode="anchor", trend=trend)
        exited = backtest.run_ladder(candles, mode="anchor", trend=trend, trend_exit=True)
        assert held.buys == exited.buys >= 1
        assert held.open_lots and held.sells == 0  # the filter alone rides it down
        assert not exited.open_lots and exited.sells == 1
        assert exited.total > held.total

    def test_the_exit_needs_the_filter(self):
        with pytest.raises(ValueError):
            backtest.run_ladder(make_candles([100, 101]), mode="anchor", trend_exit=True)


class TestTrendBaseline:
    def test_it_holds_above_the_average_and_nothing_below(self):
        prices = [100] * 24 + [110, 120, 130, 100, 90, 95, 120]
        candles = make_candles(prices)
        trend = backtest.above_trend(candles, 24)
        result = backtest.run_trend(candles, trend=trend, name="trend +1d")
        assert [f.side for f in result.fills] == [Side.BUY, Side.SELL, Side.BUY]
        assert result.max_capital == backtest.HOLD_DOLLARS
        assert result.strategy == "trend +1d"


class TestRolling:
    def series(self, days=40):
        return {"BTC-USD": make_candles(oscillating(24 * days))}

    def test_windows_end_on_the_last_bar_and_fit_the_history(self):
        series = self.series()
        starts = backtest.window_starts(
            series, window=timedelta(days=10), step=timedelta(days=7)
        )
        bars = series["BTC-USD"]
        assert starts[-1] + timedelta(days=10) == bars[-1].end
        assert starts[0] >= bars[0].start
        assert len(starts) == 5  # 30 days of room, a start every 7

    def test_every_strategy_runs_in_every_window_from_flat(self):
        series = self.series()
        trends = {"BTC-USD": backtest.above_trend(series["BTC-USD"], 24)}
        windows = backtest.run_rolling(
            series, trends, window=timedelta(days=10), step=timedelta(days=10), trend_days=1
        )
        assert len(windows) == 4
        for window in windows:
            results = window.base["BTC-USD"]
            assert {r.bars for r in results} == {240}
            assert len(window.stressed["BTC-USD"]) == len(results)
        rows = {r.strategy: r for r in backtest.rolling_rows(windows)}
        assert rows["hold"].windows == 4 and rows["hold"].beat_hold is None
        assert rows["ladder (anchor) +1d exit"].beat_hold is not None
        report = backtest.render_rolling(
            windows, window=timedelta(days=10), step=timedelta(days=10)
        )
        assert "4 windows" in report and "beat hold" in report

    def test_a_window_that_never_trades_counts_as_zero(self):
        empty = backtest.Result("ladder (lot)", "BTC-USD", 10, None, None, Decimal("1.9"))
        hold = replace(empty, strategy="hold", realized=Decimal("-5"), max_capital=Decimal("100"))
        window = backtest.RollingWindow(
            T0, T0, {"BTC-USD": [empty, hold]}, {"BTC-USD": [empty, hold]}
        )
        [row, _] = backtest.rolling_rows([window])
        assert row.returns == [0.0] and row.beat_hold == 1

    def test_no_whole_window_says_so(self):
        text = backtest.render_rolling([], window=timedelta(days=90), step=timedelta(days=30))
        assert "no whole 90-day window" in text


def test_the_cli_fetches_warmup_and_trades_the_same_window(config, monkeypatch, capsys, tmp_path):
    """The filter's history comes before the window; the window itself is unchanged."""
    bars = make_candles(oscillating(24 * 12), anchor=T0)  # 12 days of hourly bars
    requested = []

    def fake_history(cfg, symbol, *, days, refresh=False):
        requested.append(days)
        return bars

    monkeypatch.setattr("robinhood_crypto_agent.cli.backtest_history", fake_history)
    args = ["--data-dir", str(tmp_path / "data"), "backtest", "--symbols", "BTC-USD"]
    assert main([*args, "--days", "10", "--trend-days", "2", "--strategies", "ladder,hold"]) == 0
    out = capsys.readouterr().out
    assert requested == [12]  # 10 days traded + 2 of warm-up
    assert "BTC-USD (240 bars)" in out  # only the 10-day window is traded
    assert "ladder (anchor) +2d exit" in out
    assert "buy only on a bar that closed above its 2-day average" in out


def test_the_cli_adds_rolling_windows(config, monkeypatch, capsys, tmp_path):
    bars = make_candles(oscillating(24 * 30), anchor=T0)
    monkeypatch.setattr(
        "robinhood_crypto_agent.cli.backtest_history", lambda *a, **k: bars
    )
    args = ["--data-dir", str(tmp_path / "data"), "backtest", "--symbols", "BTC-USD"]
    assert main([*args, "--days", "28", "--trend-days", "2", "--roll-window", "7"]) == 0
    out = capsys.readouterr().out
    assert "ROLLING WINDOWS" in out
    assert "7-day windows, a new one every 30 days: 1 window from" in out
    assert main([*args, "--roll-window", "7", "--roll-step", "0"]) == EXIT_ERROR
