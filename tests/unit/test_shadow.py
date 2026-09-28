"""The forward test: the split account on paper, recorded one daily close at a time.

What these pin down: the paper account is exactly what the split backtest
computes for the same days; each closed day is recorded once, with
Robinhood's real spread for what it traded, even across a restart; a late
Coinbase bar is waited for, then recorded without; the report reads the bar;
and nothing here can become a proposal.
"""

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from robinhood_crypto_agent import shadow as sh
from robinhood_crypto_agent.audit import KIND_PROPOSAL, KIND_SHADOW_DAY, AuditLog
from robinhood_crypto_agent.cli import EXIT_ERROR, EXIT_OK, main
from robinhood_crypto_agent.config import (
    AgentConfig,
    ConfigError,
    RiskLimits,
    ShadowConfig,
    StrategyConfig,
    load_config,
)
from robinhood_crypto_agent.errors import AgentError
from robinhood_crypto_agent.models import Candle, Quote, utcnow
from robinhood_crypto_agent.portfolio_backtest import Prepared, run_split
from robinhood_crypto_agent.runner import Runner, Services, describe_heartbeat
from robinhood_crypto_agent.strategy.breakout import day_views

D = Decimal
START = datetime(2026, 10, 1, tzinfo=timezone.utc)
#: 230 flat days of warm-up, then a breakout on the start day, a run up, and a
#: crash through the stop on the fourth day.
BTC = [100] * 230 + [110, 112, 115, 90]
ETH = [50] * 234


def daily(closes, symbol, *, last=START + timedelta(days=3)):
    first = last - timedelta(days=len(closes) - 1)
    bars = []
    for index, close in enumerate(closes):
        close = D(str(close))
        start = first + timedelta(days=index)
        bars.append(Candle(symbol, start, start + timedelta(days=1), close,
                           close * D("1.01"), close * D("0.99"), close, 4))
    return bars


class FakeCoinbase:
    """Daily bars up to ``through``, however many days are asked for."""

    def __init__(self, through=START + timedelta(days=3), lag=None):
        self.through = through
        #: Days a coin's feed runs behind the others.
        self.lag = lag or {}
        self.calls = []

    def __call__(self, symbol, *, interval_minutes, days):
        assert interval_minutes == 24 * 60
        self.calls.append((symbol, days))
        bars = daily(BTC if symbol == "BTC-USD" else ETH, symbol)
        through = self.through - timedelta(days=self.lag.get(symbol, 0))
        return [b for b in bars if b.start <= through]


class FakeRobinhood:
    def __init__(self, fail=False):
        self.fail = fail
        self.asked = []

    def best_bid_ask(self, symbols):
        self.asked.append(list(symbols))
        if self.fail:
            raise AgentError("Robinhood is down")
        mark = D(100)
        return [Quote(s, mark * D("0.991"), mark * D("1.009"), mark, utcnow()) for s in symbols]


def shadow_config(tmp_path, *, mode="dca", start=START):
    return AgentConfig(
        watchlist=("BTC-USD",),
        data_dir=tmp_path / "data",
        risk=RiskLimits(min_notional_per_trade_usd=D(5), max_spread_pct=D(2)),
        strategy=StrategyConfig(trend_days=1),
        shadow=ShadowConfig(start=start, symbols=("BTC-USD", "ETH-USD"), long_mode=mode),
    )


def at(day_offset, hour=6):
    """A moment ``hour`` hours into the UTC day ``day_offset`` days after the start."""
    return START + timedelta(days=day_offset, hours=hour)


def tracker(config, *, now, fetch=None, robinhood=None):
    return sh.ShadowTracker(
        config, AuditLog(config.audit_path), robinhood or FakeRobinhood(),
        fetch=fetch or FakeCoinbase(), now=lambda: now,
    )


def shadow_days(config):
    return list(AuditLog(config.audit_path).events(kind=KIND_SHADOW_DAY))


class TestConfig:
    def test_the_shipped_forward_test_is_the_owners_split(self):
        shadow = load_config("config").shadow
        assert shadow.start == datetime(2026, 9, 28, tzinfo=timezone.utc)
        assert shadow.symbols == ("BTC-USD", "ETH-USD", "SOL-USD", "XRP-USD")
        assert (shadow.capital, shadow.long_pct, shadow.long_mode) == (500, 50, "dip")
        assert (shadow.long_capital, shadow.short_capital) == (250, 250)

    def test_the_breakout_keeps_its_own_cap_under_the_accounts(self):
        """The account-wide limit (20%) is the split's cap on one coin; the
        breakout still sizes to its own 10% of its sleeve, as it was tested."""
        config = load_config("config")
        rule, _ = sh.rules(config)
        assert rule.max_weight_pct == 10
        assert sh.coin_cap_pct(config) == 20
        assert sh.setup(config)["coin_cap_pct"] == "20"
        assert sh.long_tranches(config) == 10  # $62.50 a coin in $6.25 buys

    def test_no_file_means_no_forward_test(self, tmp_path):
        assert load_config(tmp_path).shadow is None
        assert ShadowConfig.from_mapping({}) is None

    def test_a_start_is_a_utc_day(self):
        for start in ("2026-10-01", "2026-10-01T17:30:00+00:00", datetime(2026, 10, 1).date()):
            assert ShadowConfig.from_mapping({"start": start}).start == START

    @pytest.mark.parametrize(
        "bad",
        [
            {},
            {"start": "2026-10-01", "long_pct": 100},
            {"start": "2026-10-01", "long_pct": 0},
            {"start": "2026-10-01", "long_mode": "yolo"},
            {"start": "2026-10-01", "capital": 0},
            {"start": "2026-10-01", "symbols": ["BTC-USD"], "long_symbols": ["ETH-USD"]},
            {"start": "2026-10-01", "surprise": 1},
            {"start": "not a date"},
        ],
    )
    def test_bad_settings_are_refused(self, bad):
        with pytest.raises(ConfigError):
            ShadowConfig.from_mapping(bad or {"capital": 5})


class TestReplay:
    def test_the_paper_account_is_the_split_backtest(self, tmp_path):
        config = shadow_config(tmp_path)
        bars = {s: FakeCoinbase()(s, interval_minutes=1440, days=300) for s in ("BTC-USD", "ETH-USD")}
        result = sh.replay(bars, config, through=START + timedelta(days=3))
        rule, plan = sh.rules(config)
        prepared = {s: Prepared(b, day_views(b, rule)) for s, b in bars.items()}
        direct = run_split(prepared, rule, plan, start=START, long_pct=D(50))
        assert result.split.combined.equity_curve == direct.combined.equity_curve
        assert result.closes == 4

    def test_fills_are_the_breakouts_trades_and_the_tranches(self, tmp_path):
        config = shadow_config(tmp_path)
        bars = {s: FakeCoinbase()(s, interval_minutes=1440, days=300) for s in ("BTC-USD", "ETH-USD")}
        result = sh.replay(bars, config, through=START + timedelta(days=3))
        assert [(f.day, f.sleeve, f.symbol, f.side) for f in result.fills] == [
            (START, "long-term", "BTC-USD", "buy"),
            (START, "long-term", "ETH-USD", "buy"),
            (START, "short-term", "BTC-USD", "buy"),
            (START + timedelta(days=3), "short-term", "BTC-USD", "sell"),
        ]
        buy = result.fills[2]
        assert buy.close == D(110) and buy.dollars == result.split.short.trades[0].cost
        assert result.fills_on(START + timedelta(days=1)) == []

    def test_the_fetch_covers_the_warmup_before_the_start(self, tmp_path):
        config = shadow_config(tmp_path)
        assert sh.history_days(config, now=at(10)) == 10 + 200 + 3
        assert sh.latest_close(at(10)) == START + timedelta(days=9)


class TestTracker:
    def test_nothing_happens_before_the_first_close(self, tmp_path):
        config = shadow_config(tmp_path)
        fetch = FakeCoinbase()
        assert tracker(config, now=at(0), fetch=fetch).update() is None
        assert fetch.calls == [] and shadow_days(config) == []

    def test_each_close_is_recorded_once_even_across_a_restart(self, tmp_path):
        config = shadow_config(tmp_path)
        fetch = FakeCoinbase(through=START)
        first = tracker(config, now=at(1), fetch=fetch)
        result = first.update()
        assert result is not None and result.through == START
        assert first.update() is None
        assert tracker(config, now=at(1, hour=20), fetch=fetch).update() is None  # a restart
        assert len(fetch.calls) == 2  # one fetch per coin, for the one day
        [record] = shadow_days(config)
        assert record["day"] == "2026-10-01" and record["mode"] == "paper"
        assert "proposal_id" not in record
        assert list(AuditLog(config.audit_path).events(kind=KIND_PROPOSAL)) == []

    def test_a_day_records_what_was_traded_and_robinhoods_spread(self, tmp_path):
        config = shadow_config(tmp_path)
        robinhood = FakeRobinhood()
        tracker(config, now=at(1), fetch=FakeCoinbase(through=START), robinhood=robinhood).update()
        [record] = shadow_days(config)
        assert robinhood.asked == [["BTC-USD", "ETH-USD"]]
        fills = {(f["sleeve"], f["symbol"], f["side"]): f for f in record["fills"]}
        assert set(fills) == {
            ("long-term", "BTC-USD", "buy"),
            ("long-term", "ETH-USD", "buy"),
            ("short-term", "BTC-USD", "buy"),
        }
        assert fills[("long-term", "ETH-USD", "buy")]["dollars"] == "12.50"
        assert D(fills[("short-term", "BTC-USD", "buy")]["spread_pct"]) == D("1.8")
        assert record["accounts"]["split"]["capital"] == "500.00"

    def test_a_changed_setup_is_a_new_test(self, tmp_path):
        config = shadow_config(tmp_path)
        fetch = FakeCoinbase(through=START)
        tracker(config, now=at(1), fetch=fetch).update()
        changed = shadow_config(tmp_path, mode="lump")
        assert logged(changed) == {}
        assert tracker(changed, now=at(1), fetch=fetch).update() is not None
        assert [r["setup"]["long_mode"] for r in shadow_days(config)] == ["dca", "lump"]
        snapshot = {"setup": sh.setup(config), "closes": 1}
        assert "nothing recorded yet" in sh.describe_status(changed, snapshot)[0]

    def test_a_quiet_close_is_still_recorded(self, tmp_path):
        config = shadow_config(tmp_path, mode="dip")  # never under its average: no buys
        robinhood = FakeRobinhood()
        fetch = FakeCoinbase(through=START + timedelta(days=1))
        tracker(config, now=at(2), fetch=fetch, robinhood=robinhood).update()
        [record] = shadow_days(config)
        assert record["day"] == "2026-10-02" and record["fills"] == []
        assert robinhood.asked == []  # nothing traded, so no quote needed

    def test_a_quote_failure_costs_the_spread_not_the_day(self, tmp_path):
        config = shadow_config(tmp_path)
        robinhood = FakeRobinhood(fail=True)
        tracker(config, now=at(1), fetch=FakeCoinbase(through=START), robinhood=robinhood).update()
        [record] = shadow_days(config)
        assert record["fills"] and all(f["spread_pct"] is None for f in record["fills"])

    def test_a_late_coinbase_bar_is_waited_for_then_recorded_without(self, tmp_path):
        config = shadow_config(tmp_path)
        fetch = FakeCoinbase(through=START, lag={"ETH-USD": 1})  # no ETH close yet
        with pytest.raises(AgentError, match="has not published"):
            tracker(config, now=at(1, hour=3), fetch=fetch).update()  # inside the grace
        assert shadow_days(config) == []
        tracker(config, now=at(1, hour=7), fetch=fetch).update()
        [record] = shadow_days(config)
        assert record["missing"] == ["ETH-USD"]


def logged(config):
    return sh.logged_days(AuditLog(config.audit_path), config)


class TestReport:
    def test_the_bar_reads_provisional_until_ninety_closes(self, tmp_path):
        config = shadow_config(tmp_path)
        tracker(config, now=at(1), fetch=FakeCoinbase(through=START)).update()
        text = sh.report(config, AuditLog(config.audit_path), fetch=FakeCoinbase(), now=at(4))
        assert "FORWARD TEST: THE SPLIT ON PAPER" in text
        assert "4 close(s) so far, through 2026-10-04" in text
        assert "1 of 1 logged days match  [pass so far]" in text
        assert "1.80% over 3 fill(s)  [pass so far]" in text
        assert "not logged live" in text  # the sell's day was never recorded
        assert "All three pass" not in text

    def test_a_logged_day_that_differs_from_the_replay_fails_faithful(self, tmp_path):
        config = shadow_config(tmp_path)
        audit = AuditLog(config.audit_path)
        audit.append(KIND_SHADOW_DAY, {"day": "2026-10-02", "setup": sh.setup(config), "fills": [
            {"sleeve": "short-term", "symbol": "ETH-USD", "side": "buy", "spread_pct": "2.5"},
        ]})
        bars = {s: FakeCoinbase()(s, interval_minutes=1440, days=300) for s in ("BTC-USD", "ETH-USD")}
        result = sh.replay(bars, config, through=START + timedelta(days=3))
        faithful, costs, in_range = sh.check_bar(result, result, logged(config))
        assert faithful.passed is False and "differs on 2026-10-02" in faithful.detail
        assert costs.passed is False
        assert in_range.passed is True
        assert sh.conclusion([faithful, costs, in_range]).startswith("It fails Faithful, Costs")

    def test_nothing_to_report_before_the_first_close(self, tmp_path):
        config = shadow_config(tmp_path)
        text = sh.report(config, AuditLog(config.audit_path), fetch=FakeCoinbase(), now=at(0))
        assert "nothing to report until 2026-10-02" in text

    def test_the_verdict_is_given_after_ninety_closes(self, tmp_path):
        config = shadow_config(tmp_path, mode="dip")
        last = START + timedelta(days=99)
        paths = {"BTC-USD": [100] * 330, "ETH-USD": [50] * 330}

        def fetch(symbol, *, interval_minutes, days):
            return daily(paths[symbol], symbol, last=last)

        text = sh.report(config, AuditLog(config.audit_path), fetch=fetch, now=last + timedelta(days=1))
        assert "100 close(s) so far" in text and "so far, so every verdict" not in text
        assert "Not yet judged" in text  # no day was ever logged live


class TestRunner:
    def config(self, tmp_path):
        start = sh.latest_close(utcnow())
        return shadow_config(tmp_path, start=start), start

    def test_rhca_run_records_the_close_and_the_status_snapshot(self, tmp_path):
        config, start = self.config(tmp_path)

        def fetch(symbol, *, interval_minutes, days):
            return daily(BTC[:231] if symbol == "BTC-USD" else ETH, symbol, last=start)

        runner = Runner(config, Services(robinhood=FakeRobinhood(), daily_history=fetch))
        runner.cycle(force=True)
        runner.cycle(force=True)
        [record] = shadow_days(config)
        assert record["day"] == start.date().isoformat()
        heartbeat = json.loads(config.heartbeat_path.read_text())
        assert heartbeat["services"]["forward_test"] is True
        assert heartbeat["counts"]["forward_test_days"] == 1
        snapshot = json.loads(config.shadow_path.read_text())
        assert snapshot["closes"] == 1 and snapshot["open"] == ["BTC-USD"]
        assert any("1 forward-test close(s) recorded" in line
                   for line in describe_heartbeat(heartbeat, stale_after_seconds=600))
        lines = sh.describe_status(config, snapshot)
        assert "1 close(s) since" in lines[0] and "breakout holds BTC-USD" in lines[1]

    def test_without_a_forward_test_there_is_no_task(self, tmp_path):
        config = AgentConfig(watchlist=("BTC-USD",), data_dir=tmp_path / "data")
        runner = Runner(config, Services(robinhood=FakeRobinhood()))
        assert runner.shadow is None
        assert "forward test" not in [name for name, _, _ in runner._tasks]
        assert sh.describe_status(config, None) == ["forward test   : off (no config/shadow.yaml)"]


class TestCli:
    def test_rhca_shadow_prints_the_report(self, tmp_path, monkeypatch, capsys):
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        (config_dir / "shadow.yaml").write_text(
            "shadow:\n  start: 2026-10-01\n  symbols: [BTC-USD, ETH-USD]\n  long_mode: dca\n"
        )
        monkeypatch.setattr("robinhood_crypto_agent.shadow.fetch_coinbase_history", FakeCoinbase())
        monkeypatch.setattr("robinhood_crypto_agent.shadow.utcnow", lambda: at(4))
        args = ["--config-dir", str(config_dir), "--data-dir", str(tmp_path / "data"), "shadow"]
        assert main(args) == EXIT_OK
        out = capsys.readouterr().out
        assert "FORWARD TEST" in out and "short-term  BTC-USD" not in out
        assert "2026-10-04 short-term  sell BTC-USD" in out

    def test_rhca_shadow_needs_a_forward_test(self, tmp_path, capsys):
        args = ["--config-dir", str(tmp_path), "--data-dir", str(tmp_path / "data"), "shadow"]
        assert main(args) == EXIT_ERROR
        assert "config/shadow.yaml is missing" in capsys.readouterr().err

    def test_status_says_when_nothing_is_recorded_yet(self, tmp_path, capsys):
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        (config_dir / "shadow.yaml").write_text("shadow:\n  start: 2026-10-01\n")
        args = ["--config-dir", str(config_dir), "--data-dir", str(tmp_path / "data"), "status"]
        assert main(args) == EXIT_OK
        assert "counting from the 2026-10-01 close; nothing recorded yet" in capsys.readouterr().out
