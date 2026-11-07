"""The Time Machine: a split trade's odds, replayed on past days.

What these pin down: a replay is made only of real days, scaled; the same
seed repeats a run; every coin draws the same days; a replayed trade goes
through the live rule's own code and ends where the backtest would end it on
the same bars; the forward test counts the paper account's own fills, and
only logged quotes make a past fill priced; and nothing here writes to the
audit log, let alone a proposal.
"""

import json
import random
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from robinhood_crypto_agent import shadow as sh
from robinhood_crypto_agent import timemachine as tm
from robinhood_crypto_agent.audit import KIND_PROPOSAL, KIND_SHADOW_DAY, AuditLog
from robinhood_crypto_agent.cli import EXIT_ERROR, EXIT_OK, main
from robinhood_crypto_agent.config import AgentConfig, RiskLimits, ShadowConfig
from robinhood_crypto_agent.daily import latest_close
from robinhood_crypto_agent.errors import AgentError
from robinhood_crypto_agent.models import Candle
from robinhood_crypto_agent.portfolio_backtest import Prepared, run_breakout
from robinhood_crypto_agent.strategy.breakout import Breakout, day_views

D = Decimal
DAY = timedelta(days=1)
LAST = datetime(2026, 11, 6, tzinfo=timezone.utc)
NOW = LAST + DAY + timedelta(hours=2)
RULE = Breakout()


def candles(symbol, closes, *, last=LAST):
    """Daily bars ending on ``last``: each opens at the close before it, and
    reaches 1% past the higher and lower of its open and close."""
    first = last - DAY * (len(closes) - 1)
    out = []
    for index, close in enumerate(closes):
        day = first + DAY * index
        opened = closes[index - 1] if index else close
        out.append(
            Candle(symbol, day, day + DAY, opened, max(opened, close) * D("1.01"),
                   min(opened, close) * D("0.99"), close, 4)
        )
    return out


def walk(symbol, days, *, seed, last=LAST, drift=0.0005, vol=0.03):
    rng = random.Random(seed)
    closes = [D(100)]
    for _ in range(days - 1):
        step = D(str(round(1 + rng.gauss(drift, vol), 6)))
        closes.append((closes[-1] * step).quantize(D("0.000001")))
    return candles(symbol, closes, last=last)


def steady(symbol, days, rate, *, last=LAST):
    closes = [(D(100) * D(rate) ** i).quantize(D("0.000001")) for i in range(days)]
    return candles(symbol, closes, last=last)


def breaking_out(symbol, days, *, seed):
    """A walk whose last close clears its prior 20-day high and its 100-day
    average: a breakout on the last day."""
    closes = [b.close for b in walk(symbol, days, seed=seed)]
    closes[-1] = max(closes[-121:-1]) * D("1.05")
    return candles(symbol, closes)


class Fetch:
    """Stands in for the daily-bar cache: the last ``days`` bars of each coin."""

    def __init__(self, series):
        self.series = series

    def __call__(self, symbol, days):
        return self.series.get(symbol, [])[-days:]


def run_lengths(days):
    runs, length = [], 1
    for before, after in zip(days, days[1:], strict=False):
        if after - before == DAY:
            length += 1
        else:
            runs.append(length)
            length = 1
    return runs + [length]


class TestReplays:
    def test_a_replay_is_made_of_real_days_scaled_to_the_close_before(self):
        bars = walk("BTC-USD", 200, seed=1)
        sampler = tm.Sampler({"BTC-USD": tm.moves(bars)})
        drawn = [b.start for b in bars[101:106]]
        replayed = sampler.bars(bars[100], drawn)
        assert [b.start for b in replayed] == [bars[100].start + DAY * i for i in range(1, 6)]
        for made, real in zip(replayed, bars[101:106], strict=True):
            for name in ("open", "high", "low", "close"):
                assert abs(getattr(made, name) - getattr(real, name)) < D("1e-18")

    def test_the_same_seed_repeats_a_run_and_another_seed_does_not(self):
        history = {"BTC-USD": tm.moves(walk("BTC-USD", 400, seed=2))}
        first = tm.Sampler(history, seed=7).draw(300)
        assert tm.Sampler(history, seed=7).draw(300) == first
        assert tm.Sampler(history, seed=8).draw(300) != first

    def test_blocks_run_for_block_days_on_average(self):
        history = {"BTC-USD": tm.moves(walk("BTC-USD", 1500, seed=3))}
        runs = run_lengths(tm.Sampler(history, block_days=10, seed=1).draw(20000))
        assert 8 < sum(runs) / len(runs) < 12
        runs = run_lengths(tm.Sampler(history, block_days=1, seed=1).draw(5000))
        assert sum(runs) / len(runs) < 1.05

    def test_every_coin_draws_the_same_days_and_only_days_they_all_have(self):
        btc = walk("BTC-USD", 300, seed=4)
        eth = [b for i, b in enumerate(walk("ETH-USD", 300, seed=5)) if i % 50 != 7]
        sampler = tm.Sampler({"BTC-USD": tm.moves(btc), "ETH-USD": tm.moves(eth)}, seed=2)
        eth_days = set(tm.moves(eth))
        assert set(sampler.days) == eth_days and len(eth_days) < len(tm.moves(btc))
        drawn = sampler.draw(120)
        assert set(drawn) <= eth_days
        # One draw makes both coins' replays: each takes that day's own move.
        made_btc = sampler.bars(btc[-1], drawn)
        made_eth = sampler.bars(eth[-1], drawn)
        moves_btc, moves_eth = tm.moves(btc), tm.moves(eth)
        assert made_btc[0].close == btc[-1].close * moves_btc[drawn[0]].close
        assert made_eth[0].close == eth[-1].close * moves_eth[drawn[0]].close

    def test_there_must_be_a_day_to_draw(self):
        with pytest.raises(AgentError, match="no past day"):
            tm.Sampler({"BTC-USD": {}})
        with pytest.raises(AgentError, match="block-days"):
            tm.Sampler({"BTC-USD": tm.moves(walk("BTC-USD", 30, seed=1))}, block_days=0)

    def test_future_views_are_the_rules_own_day_views(self):
        real = walk("BTC-USD", 300, seed=6)
        sampler = tm.Sampler({"BTC-USD": tm.moves(real)}, seed=4)
        future = sampler.bars(real[-1], sampler.draw(60))
        whole = day_views(real + future, RULE)
        assert tm.future_views(real, future, RULE) == {b.start: whole[b.start] for b in future}


class TestTrade:
    def test_a_steady_rise_is_never_stopped(self):
        daily = steady("BTC-USD", 400, "1.01")
        entry = tm.entry_on("BTC-USD", daily, LAST, RULE)
        odds = tm.replay_trade(daily, entry, RULE, replays=50, horizon=60)
        assert entry.breakout
        assert odds.stopped_by(60) == 0 and odds.held == 1
        # Every drawn day is the same 1% rise, so every replay ends alike.
        rs = [o.r for o in odds.outcomes]
        assert max(rs) - min(rs) < D("1e-6") and odds.mean_r > 2
        assert odds.positive == 1

    def test_a_steady_fall_is_stopped_at_a_loss(self):
        daily = steady("BTC-USD", 400, "0.97")
        entry = tm.entry_on("BTC-USD", daily, LAST, RULE)
        odds = tm.replay_trade(daily, entry, RULE, replays=50, horizon=60)
        assert not entry.breakout and entry.stop < entry.view.close
        assert odds.stopped_by(60) == 1 and odds.stopped_by(60, gain=False) == 1
        assert all(o.r < 0 for o in odds.outcomes)

    def test_a_replay_ends_where_the_backtest_would_end_it(self):
        daily = breaking_out("SOL-USD", 600, seed=9)
        entry = tm.entry_on("SOL-USD", daily, LAST, RULE)
        assert entry.breakout
        odds = tm.replay_trade(daily, entry, RULE, replays=5, seed=11, sample_days=500)

        # The same seed over the same days draws the same first replay.
        sample = [b for b in daily if b.start > LAST - DAY * 500]
        sampler = tm.Sampler({"SOL-USD": tm.moves(sample)}, seed=11)
        future = sampler.bars(daily[-1], sampler.draw(odds.horizon))
        bars = daily + future
        backtest = run_breakout(
            {"SOL-USD": Prepared(bars, day_views(bars, RULE))}, RULE,
            start=LAST, end=future[-1].end,
        )
        trade = backtest.trades[0]
        first = odds.outcomes[0]
        assert trade.entry_day == LAST
        if first.stopped_on is None:
            assert trade.open
        else:
            assert trade.exit_day == LAST + DAY * first.stopped_on
        assert abs(trade.r - first.r) < D("1e-18")

    def test_the_history_is_the_rule_before_the_entry_day(self):
        daily = walk("XRP-USD", 900, seed=12, drift=0.002)
        entry = tm.entry_on("XRP-USD", daily, LAST, RULE)
        odds = tm.replay_trade(daily, entry, RULE, replays=10)
        assert odds.history and all(t.entry_day < LAST for t in odds.history)

    def test_a_proposal_replays_the_close_it_was_decided_on(self, tmp_path):
        audit = AuditLog(tmp_path / "audit.jsonl")
        decided = LAST - DAY * 3
        for proposal_id, rule in (("p-entry", "entry"), ("p-tranche", "tranche")):
            audit.append(KIND_PROPOSAL, {
                "strategy": "split", "proposal_id": proposal_id, "symbol": "ETH-USD",
                "side": "buy", "notional": "20", "reference_price": "100",
                "proposal": {"sizing_detail": {"split": {
                    "sleeve": "short-term" if rule == "entry" else "long-term",
                    "rule": rule, "day": decided.isoformat(),
                }}},
            })
        config = AgentConfig(watchlist=("ETH-USD",), data_dir=tmp_path)
        fetch = Fetch({"ETH-USD": walk("ETH-USD", 700, seed=13)})
        odds = tm.trade_odds(config, audit, fetch, now=NOW, proposal_id="p-entry", replays=10)
        assert odds.entry.day == decided and odds.entry.proposal_id == "p-entry"
        assert "proposal p-entry" in tm.render_trade(odds)
        with pytest.raises(AgentError, match="held for good"):
            tm.trade_odds(config, audit, fetch, now=NOW, proposal_id="p-tranche")
        with pytest.raises(AgentError, match="no proposal"):
            tm.trade_odds(config, audit, fetch, now=NOW, proposal_id="p-none")
        with pytest.raises(AgentError, match="name a coin"):
            tm.trade_odds(config, audit, fetch, now=NOW)

    def test_the_report_says_the_seed_the_drift_and_that_it_is_not_a_proposal(self, tmp_path):
        config = AgentConfig(watchlist=("BTC-USD",), data_dir=tmp_path)
        fetch = Fetch({"BTC-USD": walk("BTC-USD", 1700, seed=14)})
        audit = AuditLog(tmp_path / "audit.jsonl")
        odds = tm.trade_odds(config, audit, fetch, now=NOW, symbol="BTC-USD", replays=40, seed=5)
        text = tm.render_trade(odds)
        assert "40 replays" in text and "seed 5" in text and "carry that drift" in text
        assert "Nothing here is a proposal or an approval" in text
        assert tm.trade_summary(odds)["replays"] == 40
        assert not audit.path.exists() or not list(audit.events())


START = datetime(2026, 10, 1, tzinfo=timezone.utc)


def forward_config(tmp_path):
    return AgentConfig(
        watchlist=("BTC-USD",),
        data_dir=tmp_path / "data",
        risk=RiskLimits(min_notional_per_trade_usd=D(5)),
        shadow=ShadowConfig(start=START, symbols=("BTC-USD", "ETH-USD"), long_mode="dip"),
    )


def forward_series(last):
    return {
        "BTC-USD": walk("BTC-USD", 1700, seed=21, last=last),
        "ETH-USD": walk("ETH-USD", 1700, seed=22, last=last),
    }


class TestForwardTest:
    def test_it_counts_the_paper_accounts_own_fills(self, tmp_path):
        config, now = forward_config(tmp_path), START + DAY * 30 + timedelta(hours=6)
        series = forward_series(latest_close(now))
        audit = AuditLog(config.audit_path)
        odds = tm.forward_test(config, audit, Fetch(series), now=now, replays=6, seed=3)
        assert odds.read_day == START + DAY * 89 and odds.extension_day == START + DAY * 179
        assert len(odds.at_extension) == 6

        # The first replay again, through rhca shadow's own replay.
        through = latest_close(now)
        keep = sh.history_days(config, now=now)
        sampler = tm.Sampler(
            {s: tm.moves([b for b in d if b.start > through - DAY * tm.DEFAULT_SAMPLE_DAYS])
             for s, d in series.items()},
            seed=3,
        )
        drawn = sampler.draw((odds.extension_day - through).days)
        daily = {s: d[-keep:] + sampler.bars(d[-1], drawn) for s, d in series.items()}
        replayed = sh.replay(daily, config, through=odds.extension_day)
        fills = [f for f in replayed.fills if f.day > through]
        assert odds.at_extension[0] == len(fills)
        assert odds.at_read[0] == sum(f.day <= odds.read_day for f in fills)
        assert odds.fills_so_far == len(sh.replay(
            {s: d[-keep:] for s, d in series.items()}, config, through=through
        ).fills)

    def test_only_fills_logged_with_a_robinhood_quote_are_priced(self, tmp_path):
        config, now = forward_config(tmp_path), START + DAY * 30 + timedelta(hours=6)
        audit = AuditLog(config.audit_path)
        fills = [
            {"sleeve": "long-term", "symbol": "BTC-USD", "side": "buy", "spread_pct": "1.2"},
            {"sleeve": "long-term", "symbol": "ETH-USD", "side": "buy", "spread_pct": None},
        ]
        audit.append(KIND_SHADOW_DAY, {"day": (START + DAY * 4).date().isoformat(),
                                       "setup": sh.setup(config), "fills": fills})
        audit.append(KIND_SHADOW_DAY, {"day": (START + DAY * 5).date().isoformat(),
                                       "setup": {"another": "test"}, "fills": fills})
        series = forward_series(latest_close(now))
        odds = tm.forward_test(config, audit, Fetch(series), now=now, replays=4)
        assert odds.priced_by_read == odds.priced_by_extension == 1
        assert all(count >= 1 for count in odds.at_read)

    def test_past_its_extension_nothing_is_replayed(self, tmp_path):
        config, now = forward_config(tmp_path), START + DAY * 200
        series = forward_series(latest_close(now))
        odds = tm.forward_test(config, AuditLog(config.audit_path), Fetch(series), now=now)
        assert not odds.simulated and odds.replays == 0
        assert "Past its extension" in tm.render_forward(odds, config)
        assert tm.forward_summary(odds)["deadlines"] == {}

    def test_it_writes_nothing_to_the_audit_log(self, tmp_path):
        config, now = forward_config(tmp_path), START + DAY * 30 + timedelta(hours=6)
        audit = AuditLog(config.audit_path)
        audit.record_note("before")
        before = config.audit_path.read_bytes()
        series = forward_series(latest_close(now))
        odds = tm.forward_test(config, audit, Fetch(series), now=now, replays=3)
        text = tm.render_forward(odds, config)
        assert config.audit_path.read_bytes() == before
        assert "not proposals" in text and "3 replays" in text
        assert set(tm.forward_summary(odds)["deadlines"]) == {"read", "extension"}


class TestCli:
    @pytest.fixture
    def coinbase(self, monkeypatch):
        series = forward_series(LAST)

        def fetch(symbol, *, interval_minutes, days):
            assert interval_minutes == 24 * 60
            return series[symbol][-days:]

        monkeypatch.setattr("robinhood_crypto_agent.daily.fetch_coinbase_history", fetch)
        monkeypatch.setattr("robinhood_crypto_agent.cli.utcnow", lambda: NOW)

    def args(self, tmp_path, *rest):
        return ["--config-dir", str(tmp_path / "config"), "--data-dir", str(tmp_path / "data"),
                "timemachine", *rest]

    def test_rhca_timemachine_replays_a_coin(self, tmp_path, coinbase, capsys):
        assert main(self.args(tmp_path, "btc-usd", "--replays", "30", "--json")) == EXIT_OK
        summary = json.loads(capsys.readouterr().out)
        assert summary["symbol"] == "BTC-USD" and summary["replays"] == 30
        assert summary["day"] == LAST.date().isoformat()

    def test_rhca_timemachine_replays_the_forward_test(self, tmp_path, coinbase, capsys):
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        (config_dir / "shadow.yaml").write_text(
            "shadow:\n  start: 2026-10-01\n  symbols: [BTC-USD, ETH-USD]\n"
        )
        assert main(self.args(tmp_path, "--forward-test", "--replays", "5")) == EXIT_OK
        assert "the forward test's odds of 3 priced paper fills" in capsys.readouterr().out

    @pytest.mark.parametrize(
        "rest, message",
        [
            ((), "name a coin"),
            (("BTC-USD", "--forward-test"), "takes no coin"),
            (("BTC-USD", "--proposal", "p-1"), "not both"),
            (("--forward-test",), "config/shadow.yaml is missing"),
        ],
    )
    def test_rhca_timemachine_refuses_what_it_cannot_replay(
        self, tmp_path, coinbase, capsys, rest, message
    ):
        assert main(self.args(tmp_path, *rest)) == EXIT_ERROR
        assert message in capsys.readouterr().err
