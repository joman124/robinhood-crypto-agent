"""The backtest: costs charged honestly, and the losses a win rate hides shown.

The series here are synthetic, so they pin down mechanics -- fills, the ladder
rules, the caps, the paging -- and say nothing about how either strategy does
on real prices. That is what ``rhca backtest`` on real history is for.
"""

import json
import math
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from robinhood_crypto_agent import backtest
from robinhood_crypto_agent.bootstrap import fetch_coinbase_history
from robinhood_crypto_agent.cli import EXIT_ERROR, EXIT_OK, backtest_history, main
from robinhood_crypto_agent.config import AgentConfig
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

    def test_the_time_exit_sells_only_lots_old_enough(self):
        book = backtest.Book(Decimal("0"))
        book.buy(T0, 0, Decimal("10"), Decimal("10"))
        book.buy(T0, 5, Decimal("10"), Decimal("10"))
        book.sell_lots_opened_by(T0, Decimal("10"), 3)
        assert book.held == Decimal("1") and len(book.closed) == 1


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


@pytest.fixture(scope="module")
def signal_run():
    """One signal run shared by the tests below; the views are the slow part."""
    config = AgentConfig(watchlist=("BTC-USD",))
    candles = make_candles(oscillating(300))
    views = backtest.signal_views(candles, config)
    return config, candles, views, backtest.run_signal(candles, config, views=views)


class TestSignal:
    def test_a_position_never_exceeds_the_concentration_cap(self, signal_run):
        config, _, _, result = signal_run
        cap = backtest.DEFAULT_PORTFOLIO * config.risk.max_position_pct_of_portfolio / 100
        assert result.buys > 0
        assert result.max_capital <= cap * Decimal("1.05")  # valued at cost, capped at the mark

    def test_every_lot_is_closed_by_the_time_exit(self, signal_run):
        config, _, _, result = signal_run
        held_bars = [
            (t.closed_at - t.opened_at) / timedelta(hours=1) for t in result.closed
        ]
        assert held_bars and max(held_bars) <= config.strategy.exit_after_bars

    def test_shared_views_change_nothing(self, signal_run):
        config, candles, views, result = signal_run
        costlier = backtest.run_signal(
            candles, config, views=views, round_trip_pct=Decimal("2.85")
        )
        assert costlier.buys == result.buys  # same decisions: views ignore the spread
        assert costlier.total < result.total  # only the cost changed


def test_hold_is_the_baseline(config):
    result = backtest.run_hold(make_candles(declining()))
    assert result.buys == 1 and result.max_capital == backtest.HOLD_DOLLARS
    assert result.total < 0


def test_the_report_leads_with_what_a_win_rate_hides(config):
    candles = make_candles(declining(120))
    base = {"BTC-USD": backtest.run_all(candles, config)}
    stressed = {"BTC-USD": backtest.run_all(candles, config, round_trip_pct=Decimal("2.85"))}
    report = backtest.render_report(
        base, stressed, steps=backtest.DEFAULT_LADDER, round_trip_pct=Decimal("1.9"), config=config
    )
    assert "ladder (lot)" in report and "ladder (anchor)" in report and "hold" in report
    assert "Read total P&L, worst drawdown and 'win +open' first" in report
    assert "authorizes nothing" in report


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
        "signal",
        "hold",
    }
