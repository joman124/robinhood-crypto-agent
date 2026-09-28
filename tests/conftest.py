"""Shared fixtures.

Everything here is offline. No test in this suite may reach the network: the
whole point of the architecture is that the decision path is exercisable
without an account, so a test that needed one would be testing the wrong thing.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from robinhood_crypto_agent.config import AgentConfig, RiskLimits, StrategyConfig
from robinhood_crypto_agent.models import Candle, PairConstraints, Quote, utcnow


@pytest.fixture
def anchor() -> datetime:
    """A fixed hour boundary in the past, so bars are complete and stable."""
    return datetime(2026, 3, 1, 0, 0, 0, tzinfo=timezone.utc)


def make_candles(
    prices, *, symbol: str = "BTC-USD", anchor: datetime | None = None, observations: int = 4
) -> list[Candle]:
    """Build hourly bars from a list of closes."""
    start = anchor or datetime(2026, 3, 1, tzinfo=timezone.utc)
    candles = []
    for index, price in enumerate(prices):
        close = Decimal(str(price))
        bar_start = start + timedelta(hours=index)
        candles.append(
            Candle(
                symbol=symbol,
                start=bar_start,
                end=bar_start + timedelta(hours=1),
                open=close,
                high=close * Decimal("1.004"),
                low=close * Decimal("0.996"),
                close=close,
                observations=observations,
            )
        )
    return candles


def uptrend(count: int = 70, start: float = 100.0, rate: float = 1.012) -> list[float]:
    return [start * (rate**i) for i in range(count)]


def downtrend(count: int = 70, start: float = 100.0, rate: float = 0.988) -> list[float]:
    return [start * (rate**i) for i in range(count)]


def choppy(count: int = 70, start: float = 100.0, amplitude: float = 5.0) -> list[float]:
    return [start + amplitude * math.sin(i / 2.4) for i in range(count)]


@pytest.fixture
def limits() -> RiskLimits:
    return RiskLimits()


@pytest.fixture
def strategy() -> StrategyConfig:
    return StrategyConfig()


@pytest.fixture
def constraints() -> PairConstraints:
    return PairConstraints(
        symbol="BTC-USD",
        quantity_increment=Decimal("0.00000001"),
        min_order_size=Decimal("0.000001"),
        max_order_size=Decimal("100"),
        price_increment=Decimal("0.01"),
    )


@pytest.fixture
def config(tmp_path) -> AgentConfig:
    return AgentConfig(
        watchlist=("BTC-USD", "ETH-USD"),
        rhs_account_number="123456789",
        data_dir=tmp_path / "data",
    )


@pytest.fixture
def quote() -> Quote:
    return Quote(
        symbol="BTC-USD",
        bid=Decimal("79900"),
        ask=Decimal("80100"),
        mark=Decimal("80000"),
        observed_at=utcnow(),
        previous_close=Decimal("79000"),
    )


def daily_candles(closes, *, symbol: str = "BTC-USD", last: datetime) -> list[Candle]:
    """UTC daily bars ending with the day that starts at ``last``, each with a
    high and low 1% either side of its close."""
    first = last - timedelta(days=len(closes) - 1)
    out = []
    for index, price in enumerate(closes):
        close = Decimal(str(price))
        start = first + timedelta(days=index)
        out.append(
            Candle(symbol, start, start + timedelta(days=1), close,
                   close * Decimal("1.01"), close * Decimal("0.99"), close, 4)
        )
    return out


class FakeCoinbaseDaily:
    """Stands in for Coinbase's daily candles: fixed paths per coin, served up
    to whatever day ``clock`` says has closed."""

    def __init__(self, paths, *, last: datetime, clock=None, fail: bool = False):
        self.bars = {s: daily_candles(c, symbol=s, last=last) for s, c in paths.items()}
        self.clock = clock
        self.fail = fail
        self.calls: list[tuple[str, int]] = []

    def __call__(self, symbol, *, interval_minutes, days):
        from robinhood_crypto_agent.daily import latest_close
        from robinhood_crypto_agent.errors import AgentError

        assert interval_minutes == 24 * 60
        self.calls.append((symbol, days))
        if self.fail:
            raise AgentError("Coinbase is unreachable")
        bars = self.bars.get(symbol, [])
        if self.clock is not None:
            through = latest_close(self.clock())
            bars = [b for b in bars if b.start <= through]
        return bars
