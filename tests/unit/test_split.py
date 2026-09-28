"""The long-term sleeve and the split account.

Synthetic series pin down mechanics: when each long-term mode buys, that
lump sum is exactly the hold baseline, that the sleeves keep their own cash,
and that the CLI wires it all up. Whether "buy low" beats buying at once on
real prices is what ``rhca backtest --strategies split`` on Coinbase bars is for.
"""

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from robinhood_crypto_agent import portfolio_backtest as pb
from robinhood_crypto_agent.cli import EXIT_ERROR, EXIT_OK, main
from robinhood_crypto_agent.models import Candle
from robinhood_crypto_agent.strategy.breakout import Breakout, day_views
from robinhood_crypto_agent.strategy.hodl import DCA, DIP, LUMP, Accumulate, moving_averages

D = Decimal
T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
RULE = Breakout(breakout_days=5, regime_days=10, atr_days=5, max_weight_pct=D("50"))
#: Short windows so a test's path stays short: a 5-day average, weekly buys.
DIP_PLAN = Accumulate(mode=DIP, tranches=2, every_days=7, average_days=5)


def days(closes, *, symbol="BTC-USD"):
    out = []
    for index, close in enumerate(closes):
        close = D(str(close))
        start = T0 + timedelta(days=index)
        out.append(Candle(symbol, start, start + timedelta(days=1), close,
                          close * D("1.01"), close * D("0.99"), close, 24))
    return out


def prepared(**paths):
    return {
        symbol.replace("_", "-"): pb.Prepared(bars, day_views(bars, RULE))
        for symbol, bars in paths.items()
    }


def day(n):
    return T0 + timedelta(days=n)


class TestRule:
    def test_modes_and_settings_are_validated(self):
        with pytest.raises(ValueError):
            Accumulate(mode="yolo")
        with pytest.raises(ValueError):
            Accumulate(tranches=0)
        rule = Accumulate()
        assert (rule.mode, rule.tranches, rule.every_days, rule.average_days) == (DIP, 10, 7, 200)
        assert Accumulate(mode=LUMP).tranche_count == 1 and rule.tranche_count == 10

    def test_dip_buys_only_under_its_average_and_at_most_weekly(self):
        rule = Accumulate(mode=DIP)
        under = dict(day=day(10), close=D(90), average=D(100))
        assert rule.due(**under, last_buy=None)
        assert not rule.due(**under, last_buy=day(4))  # six days since the last buy
        assert rule.due(**under, last_buy=day(3))
        assert not rule.due(day=day(10), close=D(100), average=D(100), last_buy=None)
        assert not rule.due(day=day(10), close=D(90), average=None, last_buy=None)

    def test_dca_and_lump_ignore_the_price(self):
        for mode in (DCA, LUMP):
            assert Accumulate(mode=mode).due(day=day(0), close=D(500), average=D(1), last_buy=None)

    def test_the_average_includes_today_and_waits_for_its_history(self):
        bars = days([1, 2, 3, 4, 5, 6])
        averages = moving_averages(bars, 3)
        assert bars[1].start not in averages
        assert averages[bars[2].start] == D(2)
        assert averages[bars[5].start] == D(5)


class TestLongTermSleeve:
    def test_the_lump_sum_is_the_hold_baseline(self):
        paths = prepared(BTC_USD=days([100, 110, 90]), ETH_USD=days([50, 40, 60], symbol="ETH-USD"))
        lump = pb.run_accumulate(paths, Accumulate(mode=LUMP), min_trade=D(0))
        hold = pb.run_equal_hold(paths)
        assert lump.equity_curve == hold.equity_curve
        assert [(t.symbol, t.cost, t.quantity) for t in lump.trades] == [
            (t.symbol, t.cost, t.quantity) for t in hold.trades
        ]
        assert hold.name == "hold (equal weight)" and lump.name == "long-term: lump sum"

    def test_dca_buys_a_tranche_a_week_until_it_is_all_in(self):
        plan = Accumulate(mode=DCA, tranches=3, every_days=7)
        result = pb.run_accumulate(prepared(BTC_USD=days([100] * 30)), plan, capital=D(300))
        assert [t.entry_day for t in result.trades] == [day(0), day(7), day(14)]
        assert all(t.cost == D(100) for t in result.trades)
        assert result.final_exposure_pct == D(100)
        assert all(t.open for t in result.trades)

    def test_buy_low_waits_while_the_price_is_above_its_average(self):
        rising = [100 + i for i in range(30)]
        result = pb.run_accumulate(prepared(BTC_USD=days(rising)), DIP_PLAN)
        assert result.trades == []
        assert result.final_equity == pb.DEFAULT_CAPITAL
        assert result.final_exposure_pct == 0

    def test_buy_low_buys_under_the_average_a_week_apart(self):
        # Flat, then a slide: every close from day 6 is under the 5-day average.
        closes = [100] * 6 + [100 - 2 * i for i in range(1, 20)]
        result = pb.run_accumulate(prepared(BTC_USD=days(closes)), DIP_PLAN, capital=D(100))
        assert [t.entry_day for t in result.trades] == [day(6), day(13)]
        averages = moving_averages(days(closes), DIP_PLAN.average_days)
        for trade in result.trades:
            close = D(str(closes[(trade.entry_day - T0).days]))
            assert close < averages[trade.entry_day]
            assert trade.quantity == D(50) / (close * D("1.0095"))  # paid the ask

    def test_a_small_share_buys_fewer_minimum_sized_tranches(self):
        plan = Accumulate(mode=DCA, tranches=10)
        assert plan.tranches_for(D(62.5), D(5)) == 10  # $6.25 each: ten fit
        assert plan.tranches_for(D(25), D(5)) == 5  # $2.50 each would not
        assert plan.tranches_for(D(4), D(5)) == 1
        assert Accumulate(mode=LUMP).tranches_for(D(25), D(5)) == 1
        result = pb.run_accumulate(prepared(BTC_USD=days([100] * 60)), plan, capital=D(40))
        assert [t.cost for t in result.trades] == [D(5)] * 8  # $40 in eight $5 buys
        assert "one of 8 equal tranches" in plan.describe(8)

    def test_a_share_under_the_minimum_buys_nothing(self):
        plan = Accumulate(mode=DCA)
        result = pb.run_accumulate(prepared(BTC_USD=days([100] * 30)), plan, capital=D(4))
        assert result.trades == []


class TestSplit:
    def test_the_sleeves_keep_their_own_cash_and_add_up(self):
        paths = prepared(
            BTC_USD=days([100] * 12 + [104, 106, 108, 110, 112, 109, 90, 91]),
            ETH_USD=days([50] * 20, symbol="ETH-USD"),
        )
        split = pb.run_split(paths, RULE, Accumulate(mode=LUMP), capital=D(500), long_pct=D(40))
        assert (split.long.capital, split.short.capital) == (D(200), D(300))
        assert split.combined.capital == D(500)
        for (_, total), (_, long), (_, short) in zip(
            split.combined.equity_curve, split.long.equity_curve, split.short.equity_curve,
            strict=True,
        ):
            assert total == long + short
        # The breakout sized its trade off its own $300, not the account's $500.
        [trade] = [t for t in split.short.trades if t.symbol == "BTC-USD"]
        entry = paths["BTC-USD"].views[day(12)]
        assert trade.cost == RULE.position_dollars(D(300), entry)
        assert split.combined.trades == split.long.trades + split.short.trades

    def test_the_long_term_sleeve_can_hold_fewer_coins(self):
        paths = prepared(BTC_USD=days([100] * 20), ETH_USD=days([50] * 20, symbol="ETH-USD"))
        split = pb.run_split(paths, RULE, Accumulate(mode=LUMP), long_symbols=["BTC-USD"])
        assert {t.symbol for t in split.long.trades} == {"BTC-USD"}
        assert split.short.symbols == ["BTC-USD", "ETH-USD"]

    def test_a_split_needs_two_sleeves(self):
        paths = prepared(BTC_USD=days([100] * 20))
        for share in (D(0), D(100)):
            with pytest.raises(ValueError):
                pb.run_split(paths, RULE, Accumulate(), long_pct=share)

    def test_combining_carries_a_sleeve_over_a_day_it_has_no_bar(self):
        a = pb.PortfolioResult("a", ["BTC-USD"], D(100), D(0))
        a.equity_curve, a.exposure = [(day(0), D(100)), (day(1), D(120))], [D(1), D(1)]
        b = pb.PortfolioResult("b", ["ETH-USD"], D(100), D(0))
        b.equity_curve, b.exposure = [(day(0), D(100))], [D(0)]
        both = pb.combine("both", [a, b])
        assert both.equity_curve == [(day(0), D(200)), (day(1), D(220))]
        assert both.exposure == [D("0.5"), D(120) / D(220)]

    def test_the_report_weighs_the_split_against_the_alternatives(self):
        closes = [100] * 12 + [104, 106, 108, 110, 112, 109, 90, 91]
        paths = prepared(BTC_USD=days(closes), ETH_USD=days(closes[::-1], symbol="ETH-USD"))
        report = pb.evaluate_split(paths, RULE, DIP_PLAN)
        assert report.long_variants[DIP][0] is report.split.long
        assert set(report.long_variants) == {DIP, DCA, LUMP}
        assert report.all_hold[0].final_equity == pb.run_equal_hold(paths).final_equity
        text = pb.render_split(report, RULE)
        for row in ("long-term: buy low", "long-term: weekly DCA", "long-term: lump sum",
                    "short-term: breakout", "split: buy low + breakout", "all in the breakout",
                    "all in hold (lump sum)"):
            assert row in text
        summary = pb.split_summary(report)
        assert json.dumps(summary)
        assert summary["long_mode"] == DIP and set(summary["long_term"]) == {DIP, DCA, LUMP}


RALLY = [100] * 12 + [104, 106, 108, 110, 112, 109, 90, 91]  # breakout: in day 12, out day 18


class TestCoinCap:
    """One coin's holdings, both sleeves together, stay under a share of the account."""

    def paths(self):
        return prepared(BTC_USD=days(RALLY), ETH_USD=days([50] * len(RALLY), symbol="ETH-USD"))

    def test_without_a_cap_the_sleeves_run_as_if_alone(self):
        paths = self.paths()
        split = pb.run_split(paths, RULE, DIP_PLAN, capital=D(500), long_pct=D(40))
        alone_long = pb.run_accumulate(paths, DIP_PLAN, capital=D(200))
        alone_short = pb.run_breakout(paths, RULE, capital=D(300))
        assert split.long.equity_curve == alone_long.equity_curve
        assert split.short.equity_curve == alone_short.equity_curve
        assert split.short.trimmed == split.short.blocked == split.long.blocked == 0

    def test_a_breakout_entry_is_trimmed_to_the_room_left(self):
        paths = self.paths()
        lump = Accumulate(mode=LUMP)  # $125 of BTC and of ETH from day 0
        free = pb.run_split(paths, RULE, lump, capital=D(500))
        capped = pb.run_split(paths, RULE, lump, capital=D(500), coin_cap_pct=D(30))
        [wanted] = [t for t in free.short.trades if t.symbol == "BTC-USD"]
        [got] = [t for t in capped.short.trades if t.symbol == "BTC-USD"]
        assert capped.short.trimmed == 1 and got.cost < wanted.cost
        assert got.risk == got.cost * RULE.stop_fraction(paths["BTC-USD"].views[day(12)])
        # Right after the entry, BTC is 30% of the account, to the cent.
        [(_, account)] = [(d, e) for d, e in capped.combined.equity_curve if d == day(12)]
        long_btc = sum(t.quantity for t in capped.long.trades if t.symbol == "BTC-USD")
        btc = (long_btc + got.quantity) * D(104) * (1 - D("0.0095"))
        assert abs(btc + got.cost - got.quantity * D(104) * D("0.9905") - D("0.3") * (
            account + got.cost - got.quantity * D(104) * D("0.9905"))) < D("0.01")

    def test_an_entry_with_no_room_is_blocked(self):
        paths = self.paths()
        capped = pb.run_split(paths, RULE, Accumulate(mode=LUMP), coin_cap_pct=D(25))
        assert [t for t in capped.short.trades if t.symbol == "BTC-USD"] == []
        assert capped.short.blocked == 5  # turned away each day it still wanted in: 12 to 16

    def test_the_short_term_goes_first_and_a_long_term_tranche_waits(self):
        paths = self.paths()
        plan = Accumulate(mode=DCA, tranches=2, every_days=13)  # buys due on days 0 and 13
        split = pb.run_split(
            paths, RULE, plan, capital=D(500), long_pct=D(20), long_symbols=["BTC-USD"],
            coin_cap_pct=D(25),
        )
        [short] = [t for t in split.short.trades if t.symbol == "BTC-USD"]
        assert (short.entry_day, short.exit_day) == (day(12), day(18))
        # Day 13's tranche has no room while the breakout holds BTC, and buys on
        # the day it sells.
        assert [t.entry_day for t in split.long.trades] == [day(0), day(18)]
        assert split.long.blocked == 5

    def test_the_report_says_what_the_cap_did(self):
        report = pb.evaluate_split(self.paths(), RULE, Accumulate(mode=LUMP), coin_cap_pct=D(30))
        text = pb.render_split(report, RULE)
        assert "No coin may be more than 30% of the whole account" in text
        assert (
            "The 30% per-coin limit trimmed 1 short-term entry; it turned a short-term entry "
            "away 0 times and held a long-term tranche back 0 times" in text
        )
        summary = pb.split_summary(report)
        assert summary["coin_cap_pct"] == "30" and summary["short_term"]["trimmed"] == 1

    def test_a_cap_must_be_a_percent(self):
        with pytest.raises(ValueError):
            pb.run_split(self.paths(), RULE, DIP_PLAN, coin_cap_pct=D(0))


def test_rolling_split_windows_start_in_cash():
    bars = days([100 + (i % 17) * 2 + i * 0.5 for i in range(120)])
    hourly = [
        Candle(b.symbol, b.start + timedelta(hours=h), b.start + timedelta(hours=h + 1),
               b.close, b.high, b.low, b.close, 4)
        for b in bars
        for h in range(24)
    ]
    rolling = pb.run_rolling_split(
        {"BTC-USD": hourly}, prepared(BTC_USD=bars), RULE, DIP_PLAN,
        window=timedelta(days=30), step=timedelta(days=30),
    )
    assert len(rolling.starts) == 4 and len(rolling.hold) == 4
    assert set(rolling.rows) == {"split: buy low + breakout", "long-term: buy low",
                                 "short-term: breakout"}
    text = pb.render_rolling_split(rolling, window=timedelta(days=30), step=timedelta(days=30))
    assert "SPLIT ROLLING WINDOWS" in text and "4 windows" in text


def hourly_rows(closes):
    rows = []
    for n, close in enumerate(closes):
        for hour in range(24):
            start = T0 + timedelta(days=n, hours=hour)
            rows.append({"start": start.isoformat(), "open": close, "high": close * 1.01,
                         "low": close * 0.99, "close": close})
    return rows


class TestCli:
    def args(self, tmp_path, closes):
        path = tmp_path / "bars.json"
        path.write_text(json.dumps(hourly_rows(closes)))
        return ["--data-dir", str(tmp_path / "data"), "backtest", "--bars-file", str(path),
                "--symbols", "BTC-USD", "--strategies", "split"]

    def test_the_split_prints_its_own_report(self, tmp_path, capsys):
        closes = [100] * 210 + [100 + i * 2 for i in range(40)] + [180 - i * 3 for i in range(30)]
        args = self.args(tmp_path, closes)
        assert main([*args, "--long-pct", "40", "--long-mode", "dca"]) == EXIT_OK
        out = capsys.readouterr().out
        assert "SPLIT ACCOUNT" in out and "split: weekly DCA + breakout" in out
        assert "BREAKOUT PORTFOLIO" not in out  # only the split was asked for
        assert "long-term  $200.00" in out
        assert main([*args, "--json"]) == EXIT_OK
        payload = json.loads(capsys.readouterr().out)
        assert payload["split"]["long_mode"] == DIP
        assert payload["split"]["split"]["capital"] == "500.00"

    def test_the_split_defaults_to_the_forward_tests(self, tmp_path, capsys):
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        (config_dir / "shadow.yaml").write_text(
            "shadow:\n  start: 2026-10-01\n  long_pct: 30\n  long_mode: lump\n"
        )
        (config_dir / "risk_limits.yaml").write_text(
            "limits:\n  max_position_pct_of_portfolio: 20\n  min_notional_per_trade_usd: 5\n"
        )
        closes = [100] * 210 + [100 + i * 2 for i in range(40)]
        args = ["--config-dir", str(config_dir), *self.args(tmp_path, closes)]
        assert main(args) == EXIT_OK
        out = capsys.readouterr().out
        assert "long-term  $150.00" in out and "split: lump sum + breakout" in out
        assert "No coin may be more than 20% of the whole account" in out

    def test_bad_splits_are_refused(self, tmp_path, capsys):
        args = self.args(tmp_path, [100] * 30)
        assert main([*args, "--long-pct", "0"]) == EXIT_ERROR
        assert main([*args, "--long-pct", "100"]) == EXIT_ERROR
        assert main([*args, "--long-symbols", "ETH-USD"]) == EXIT_ERROR
        assert main([*args, "--capital", "8"]) == EXIT_ERROR  # $4 of the coin
        assert "under the $5 minimum trade" in capsys.readouterr().err
        assert main([*args, "--coin-cap-pct", "0"]) == EXIT_ERROR
        assert main([*args, "--coin-cap-pct", "101"]) == EXIT_ERROR

    def test_the_200_day_average_is_fetched_before_the_window(self, tmp_path, monkeypatch):
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
        assert main([*args, "--days", "30", "--strategies", "split"]) == EXIT_OK
        assert requested == [30 + Accumulate().average_days + 1] * 2
