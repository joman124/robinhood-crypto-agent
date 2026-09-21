"""The price store exists because there is no crypto historicals MCP tool."""

from datetime import timedelta
from decimal import Decimal

from robinhood_crypto_agent.models import Quote, utcnow
from robinhood_crypto_agent.store import PriceStore
from robinhood_crypto_agent.store.prices import floor_to_interval
from tests.conftest import make_candles


def observations(store, count, *, symbol="BTCUSD", start=None, step_minutes=15):
    base = start or (utcnow().replace(minute=0, second=0, microsecond=0) - timedelta(hours=8))
    quotes = [
        Quote(
            symbol=symbol,
            bid=Decimal(100 + i) - Decimal("0.5"),
            ask=Decimal(100 + i) + Decimal("0.5"),
            mark=Decimal(100 + i),
            observed_at=base + timedelta(minutes=step_minutes * i),
        )
        for i in range(count)
    ]
    store.record_quotes(quotes)
    return quotes


def test_bars_are_aggregated_from_quote_observations(tmp_path):
    store = PriceStore(tmp_path / "prices.jsonl")
    observations(store, 20)
    candles = store.candles("BTC-USD", interval_minutes=60)
    assert candles
    first = candles[0]
    assert first.observations == 4
    assert first.high >= first.low
    assert first.open <= first.close  # the fixture rises monotonically


def test_symbol_is_normalized_across_write_and_read(tmp_path):
    """Quotes arrive as BTCUSD; the watchlist says BTC-USD."""
    store = PriceStore(tmp_path / "prices.jsonl")
    observations(store, 8, symbol="BTCUSD")
    assert store.symbols() == ["BTC-USD"]
    assert store.observations("BTC-USD")


def test_partial_current_bar_is_excluded_by_default(tmp_path):
    """An indicator on a half-formed bar changes under its own feet."""
    store = PriceStore(tmp_path / "prices.jsonl")
    now = utcnow()
    store.record_quotes(
        [Quote("BTC-USD", Decimal("99"), Decimal("101"), Decimal("100"), now)]
    )
    assert store.candles("BTC-USD", interval_minutes=60) == []
    assert len(store.candles("BTC-USD", interval_minutes=60, include_partial=True)) == 1


def test_bars_are_anchored_to_the_day_not_to_first_observation(tmp_path):
    """Restarting the store must not shift every bar boundary."""
    moment = utcnow().replace(hour=13, minute=47, second=30, microsecond=0)
    assert floor_to_interval(moment, 60).minute == 0
    assert floor_to_interval(moment, 15).minute == 45
    assert floor_to_interval(moment, 30).minute == 30


def test_coverage_reports_insufficiency_honestly(tmp_path):
    store = PriceStore(tmp_path / "prices.jsonl")
    observations(store, 8)
    coverage = store.coverage("BTC-USD", interval_minutes=60, required_bars=30)
    assert not coverage.sufficient
    assert "INSUFFICIENT" in coverage.describe()
    assert coverage.observations == 8


def test_coverage_of_an_unknown_symbol_is_empty(tmp_path):
    store = PriceStore(tmp_path / "prices.jsonl")
    coverage = store.coverage("DOGE-USD")
    assert coverage.observations == 0
    assert coverage.first_seen is None
    assert "no observations" in coverage.describe()


def test_a_torn_line_does_not_destroy_the_history(tmp_path):
    """One interrupted write must not make the whole file unreadable."""
    path = tmp_path / "prices.jsonl"
    store = PriceStore(path)
    observations(store, 4)
    with path.open("a") as handle:
        handle.write('{"symbol": "BTC-USD", "observed_at": ')  # torn write
    assert len(store.observations("BTC-USD")) == 4


def test_imported_bars_round_trip_through_aggregation(tmp_path):
    store = PriceStore(tmp_path / "prices.jsonl")
    anchor = utcnow().replace(minute=0, second=0, microsecond=0) - timedelta(hours=40)
    candles = make_candles([100, 110, 105, 120], anchor=anchor)
    store.import_candles(candles)
    rebuilt = store.candles("BTC-USD", interval_minutes=60)
    assert len(rebuilt) == 4
    assert rebuilt[0].high == candles[0].high
    assert rebuilt[0].low == candles[0].low
    assert rebuilt[-1].close == candles[-1].close


def test_latest_quote_returns_the_newest(tmp_path):
    store = PriceStore(tmp_path / "prices.jsonl")
    quotes = observations(store, 5)
    assert store.latest_quote("BTC-USD").mark == quotes[-1].mark
