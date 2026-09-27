"""The breakout rule and its one-account backtest.

Synthetic series pin down mechanics only: when it enters and exits, how it
sizes, and that the accounting charges the spread on both sides. Whether it
has an edge on real prices is what `rhca backtest` on Coinbase bars is for.
"""

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from robinhood_crypto_agent import portfolio_backtest as pb
from robinhood_crypto_agent.cli import EXIT_ERROR, EXIT_OK, main
from robinhood_crypto_agent.models import Candle
from robinhood_crypto_agent.strategy.breakout import (
    Breakout,
    DayView,
    daily_bars,
    day_views,
)

D = Decimal
T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
#: Small windows so a test's path stays short.
RULE = Breakout(
    breakout_days=5, regime_days=10, atr_days=5, stop_atr=D("3"), risk_pct=D("1"),
    max_weight_pct=D("50"),
)


def days(closes, *, symbol="BTC-USD", spread="0.01"):
    """Daily bars with a high and low 1% either side of each close."""
    out = []
    for index, close in enumerate(closes):
        close = D(str(close))
        start = T0 + timedelta(days=index)
        out.append(
            Candle(symbol, start, start + timedelta(days=1), close,
                   close * (1 + D(spread)), close * (1 - D(spread)), close, 24)
        )
    return out


def prepared(**paths):
    return {
        symbol.replace("_", "-"): pb.Prepared(bars, day_views(bars, RULE))
        for symbol, bars in paths.items()
    }


FLAT = [100] * 12
RALLY = FLAT + [104, 106, 108, 110, 112, 109, 90, 91]  # entry on day 12, exit on day 18


def view(**kw):
    base = dict(day=T0, close=D("110"), prior_high=D("100"), average=D("100"), atr=D("5"))
    base.update({k: D(str(v)) for k, v in kw.items()})
    return DayView(**base)


class TestRule:
    def test_enters_on_a_closing_high_above_the_average(self):
        assert RULE.enters(view())
        assert not RULE.enters(view(close=100))  # not above the prior high
        assert not RULE.enters(view(average=111))  # under the regime average
        assert not RULE.enters(view(atr=0))  # no range, no stop to size from

    def test_the_stop_trails_the_highest_close_by_atrs(self):
        assert RULE.stop(D("120"), view(atr=4)) == D("108")
        assert RULE.exits(D("120"), view(close=107, atr=4))
        assert not RULE.exits(D("120"), view(close=108, atr=4))

    def test_size_risks_a_fixed_share_of_the_account(self):
        # stop 3 x 5 = 15 under 110: 13.6% away; 1% of 1000 is 10 -> 73.33 in
        dollars = RULE.position_dollars(D("1000"), view())
        assert dollars == D("1000") * D("0.01") / (D("15") / D("110"))
        assert dollars * RULE.stop_fraction(view()) == pytest.approx(D("10"))

    def test_size_is_capped_per_coin(self):
        tight = view(atr="0.01")  # a tiny stop would ask for a huge position
        assert RULE.position_dollars(D("1000"), tight) == D("500")  # the 50% cap

    def test_settings_are_validated(self):
        with pytest.raises(ValueError):
            Breakout(stop_atr=D("0"))
        with pytest.raises(ValueError):
            Breakout(risk_pct=D("0"))
        with pytest.raises(ValueError):
            Breakout(breakout_days=0)
        assert Breakout().warmup_days == 99

    def test_defaults_are_the_preregistered_rule(self):
        rule = Breakout()
        assert (rule.breakout_days, rule.regime_days, rule.atr_days) == (20, 100, 20)
        assert (rule.stop_atr, rule.risk_pct, rule.max_weight_pct) == (3, 1, 10)


class TestIndicators:
    def test_hourly_bars_become_utc_days_and_an_open_day_is_left_out(self):
        hourly = []
        for hour in range(24 * 2 + 5):  # two whole days and five hours of a third
            start = T0 + timedelta(hours=hour)
            price = D(100 + hour)
            hourly.append(Candle("BTC-USD", start, start + timedelta(hours=1),
                                 price, price + 1, price - 1, price, 4))
        [first, second] = daily_bars(hourly)
        assert (first.start, first.end) == (T0, T0 + timedelta(days=1))
        assert (first.open, first.high, first.low, first.close) == (D(100), D(124), D(99), D(123))
        assert second.close == D(147)

    def test_views_wait_for_history_and_never_read_today_into_the_prior_high(self):
        bars = days(FLAT + [104])
        views = day_views(bars, RULE)
        assert bars[RULE.warmup_days - 1].start not in views
        today = views[bars[12].start]
        assert today.prior_high == D(100)  # the five days before, not today's 104
        assert today.average == (D(100) * 9 + D(104)) / 10
        # true range: today's high 105.04 against yesterday's 100 close
        assert today.atr == (D(2) * 4 + D("5.04")) / 5


class TestBacktest:
    def test_enters_on_the_breakout_and_exits_on_the_trailing_stop(self):
        paths = prepared(BTC_USD=days(RALLY))
        result = pb.run_breakout(paths, RULE, round_trip_pct=D("2"))
        [trade] = result.trades
        assert trade.entry_day == T0 + timedelta(days=12)
        assert trade.exit_day == T0 + timedelta(days=18)  # 109 held, 90 broke the stop

        entry = paths["BTC-USD"].views[T0 + timedelta(days=12)]
        assert trade.cost == RULE.position_dollars(D(500), entry)
        assert trade.quantity == trade.cost / (D(104) * D("1.01"))  # paid the ask
        assert trade.proceeds == trade.quantity * D(90) * D("0.99")  # sold at the bid
        assert trade.risk == trade.cost * RULE.stop_fraction(entry)
        assert trade.r == trade.pnl / trade.risk
        assert result.final_equity == D(500) - trade.cost + trade.proceeds

    def test_a_trade_still_open_is_marked_at_the_bid(self):
        result = pb.run_breakout(prepared(BTC_USD=days(RALLY[:16])), RULE)
        [trade] = result.trades
        assert trade.open and trade.exit_day is None
        assert result.closed == [] and result.expectancy_r is None

    def test_only_days_in_the_window_trade_and_the_averages_start_warm(self):
        paths = prepared(BTC_USD=days(RALLY))
        start = T0 + timedelta(days=13)
        result = pb.run_breakout(paths, RULE, start=start)
        assert result.equity_curve[0][0] == start
        assert result.trades[0].entry_day == start  # 106 is a fresh closing high

    def test_a_trade_below_the_minimum_is_skipped(self):
        # 1% of $30 is $0.30 at risk; at a 7.5% stop that is a $4 position
        result = pb.run_breakout(prepared(BTC_USD=days(RALLY)), RULE, capital=D(30))
        assert result.trades == []
        assert pb.run_breakout(prepared(BTC_USD=days(RALLY)), RULE, capital=D(100)).trades

    def test_cash_never_goes_negative(self):
        greedy = Breakout(breakout_days=5, regime_days=10, atr_days=5, risk_pct=D(100),
                          max_weight_pct=D(100))
        bars = {s: days(RALLY, symbol=s) for s in ("BTC-USD", "ETH-USD", "SOL-USD")}
        paths = {s: pb.Prepared(b, day_views(b, greedy)) for s, b in bars.items()}
        result = pb.run_breakout(paths, greedy)
        spent_on_day_12 = sum(t.cost for t in result.trades if t.entry_day.day == 13)
        assert spent_on_day_12 <= D(500)
        assert all(e >= 0 for e in result.exposure) and all(e <= 1 for e in result.exposure)

    def test_hold_splits_the_money_evenly_and_pays_the_spread(self):
        paths = prepared(BTC_USD=days([100, 110]), ETH_USD=days([50, 40], symbol="ETH-USD"))
        hold = pb.run_equal_hold(paths, round_trip_pct=D("2"))
        btc = D(250) / (D(100) * D("1.01")) * D(110) * D("0.99")
        eth = D(250) / (D(50) * D("1.01")) * D(40) * D("0.99")
        assert hold.final_equity == btc + eth
        assert hold.average_exposure_pct == D(100)

    def test_drawdown_is_measured_from_the_peak(self):
        result = pb.PortfolioResult("x", [], D(100), D(0))
        result.equity_curve = [(T0, D(120)), (T0, D(90)), (T0, D(130))]
        assert result.max_drawdown_pct == D(25)  # 120 -> 90
        assert result.total_return_pct == D(30)

    def test_the_breadth_check_leaves_out_the_best_coin(self):
        paths = prepared(
            BTC_USD=days(RALLY), ETH_USD=days([100] * len(RALLY), symbol="ETH-USD")
        )
        report = pb.evaluate(paths, RULE)
        best, rest = report.without_best
        assert best in ("BTC-USD", "ETH-USD")
        assert best not in rest.symbols
        text = pb.render(report, RULE)
        assert "BREAKOUT PORTFOLIO" in text and "expectancy" in text
        assert f"Without its most profitable coin ({best})" in text
        assert json.dumps(pb.summary(report))


def test_rolling_windows_start_in_cash_with_warm_averages():
    bars = days([100 + (i % 17) * 2 + i * 0.5 for i in range(120)])
    hourly = [
        Candle(b.symbol, b.start + timedelta(hours=h), b.start + timedelta(hours=h + 1),
               b.close, b.high, b.low, b.close, 4)
        for b in bars
        for h in range(24)
    ]
    paths = prepared(BTC_USD=bars)
    rolling = pb.run_rolling(
        {"BTC-USD": hourly}, paths, RULE, window=timedelta(days=30), step=timedelta(days=30)
    )
    assert len(rolling.returns) == 4
    text = pb.render_rolling(rolling, window=timedelta(days=30), step=timedelta(days=30))
    assert "4 windows" in text and "beat hold" in text


def hourly_rows(closes):
    rows = []
    for day, close in enumerate(closes):
        for hour in range(24):
            start = T0 + timedelta(days=day, hours=hour)
            rows.append({"start": start.isoformat(), "open": close, "high": close * 1.01,
                         "low": close * 0.99, "close": close})
    return rows


class TestCli:
    def args(self, tmp_path, closes):
        path = tmp_path / "bars.json"
        path.write_text(json.dumps(hourly_rows(closes)))
        return ["--data-dir", str(tmp_path / "data"), "backtest", "--bars-file", str(path),
                "--symbols", "BTC-USD"]

    def test_the_breakout_prints_its_own_report(self, tmp_path, capsys):
        closes = [100] * 110 + [100 + i * 2 for i in range(40)]
        args = self.args(tmp_path, closes)
        assert main([*args, "--strategies", "breakout", "--risk-pct", "2"]) == EXIT_OK
        out = capsys.readouterr().out
        assert "BREAKOUT PORTFOLIO" in out and "risks 2% of the account" in out
        assert "ladder (lot)" not in out  # no per-coin report was asked for
        assert main([*args, "--json"]) == EXIT_OK
        payload = json.loads(capsys.readouterr().out)
        assert payload["breakout"]["symbols"] == ["BTC-USD"] and "BTC-USD" in payload

    def test_bad_sizing_is_refused(self, tmp_path):
        args = self.args(tmp_path, [100] * 30)
        assert main([*args, "--capital", "0"]) == EXIT_ERROR
        assert main([*args, "--risk-pct", "0"]) == EXIT_ERROR

    def test_the_breakouts_warmup_is_fetched_before_the_window(self, tmp_path, monkeypatch, capsys):
        requested = []

        def fake_history(cfg, symbol, *, days, refresh=False):
            requested.append(days)
            return [
                Candle(symbol, T0 + timedelta(hours=h), T0 + timedelta(hours=h + 1),
                       D(100), D(101), D(99), D(100), 4)
                for h in range(24 * 40)
            ]

        monkeypatch.setattr("robinhood_crypto_agent.cli.backtest_history", fake_history)
        args = ["--data-dir", str(tmp_path / "data"), "backtest", "--symbols", "BTC-USD,XRP-USD"]
        assert main([*args, "--days", "30", "--strategies", "breakout"]) == EXIT_OK
        assert requested == [30 + Breakout().warmup_days + 2] * 2
        assert "trading BTC-USD, XRP-USD" in capsys.readouterr().out
