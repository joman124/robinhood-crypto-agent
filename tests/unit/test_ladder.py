"""The trend ladder's rule, on its own: the same function the backtest and the
live pipeline both call."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from robinhood_crypto_agent.models import Side
from robinhood_crypto_agent.strategy.ladder import (
    MODE_ANCHOR,
    MODE_LOT,
    REASON_DIP,
    REASON_TAKE_PROFIT,
    REASON_TREND_EXIT,
    HeldLot,
    Ladder,
    above_trend,
    flat_anchor,
    parse_steps,
    trend_average,
    trend_bars,
)
from tests.conftest import make_candles

D = Decimal
ANCHOR = D("100")


def decide(ladder=None, *, close, above=True, held="0", bought=(), sold=(), lots=()):
    ladder = ladder or Ladder()
    return ladder.decide(
        anchor=ANCHOR,
        close=D(str(close)),
        above=above,
        held=D(held),
        bought=bought,
        sold=sold,
        lots=lots,
    )


class TestBuys:
    def test_each_step_buys_its_dollars_at_its_depth(self):
        assert decide(close=96) == []
        [order] = decide(close=95)
        assert (order.side, order.reason, order.step, order.dollars) == (
            Side.BUY,
            REASON_DIP,
            0,
            D("5"),
        )
        assert [o.step for o in decide(close=80)] == [0, 1, 2]  # a gap takes every step

    def test_a_step_buys_once_per_cycle(self):
        assert [o.step for o in decide(close=89, bought={0})] == [1]

    def test_no_buy_unless_the_bar_closed_above_its_average(self):
        assert decide(close=95, above=False) == []
        assert decide(close=95, above=None) == []  # no average yet reads as "no"

    def test_without_the_filter_the_average_is_ignored(self):
        free = Ladder(trend_filter=False, trend_exit=False)
        assert [o.step for o in decide(free, close=95, above=None)] == [0]


class TestAnchorSells:
    def test_each_step_sells_its_dollars_over_the_anchor_and_the_last_sells_all(self):
        [order] = decide(close=105, held="1", bought={0})
        assert (order.side, order.reason, order.step, order.dollars) == (
            Side.SELL,
            REASON_TAKE_PROFIT,
            0,
            D("5"),
        )
        assert not order.everything
        orders = decide(close=121, held="1", bought={0}, sold={0})
        assert [o.step for o in orders] == [1, 2]
        assert orders[-1].everything and orders[-1].dollars is None

    def test_nothing_sells_when_nothing_is_held(self):
        assert decide(close=130, held="0") == []


class TestTrendExit:
    def test_a_close_at_or_under_the_average_sells_everything(self):
        [order] = decide(close=97, above=False, held="1", bought={0})
        assert (order.reason, order.everything) == (REASON_TREND_EXIT, True)

    def test_no_exit_before_the_average_exists(self):
        assert decide(close=97, above=None, held="1", bought={0}) == []

    def test_the_exit_is_off_when_switched_off(self):
        filter_only = Ladder(trend_exit=False)
        assert decide(filter_only, close=97, above=False, held="1", bought={0}) == []

    def test_the_exit_needs_the_filter(self):
        with pytest.raises(ValueError):
            Ladder(trend_filter=False, trend_exit=True)


class TestLotMode:
    def test_a_lot_sells_its_own_step_over_where_it_was_bought_and_re_arms(self):
        lot = HeldLot(step=0, mark=D("80"), key="the-lot")
        ladder = Ladder(mode=MODE_LOT, trend_filter=False, trend_exit=False)
        orders = decide(ladder, close=84, held="1", bought={0}, lots=[lot])
        # sold at +5% over 80, and 84 is still 16% under the anchor: step 0 buys again
        assert [(o.side, o.step, o.lot) for o in orders] == [
            (Side.SELL, 0, "the-lot"),
            (Side.BUY, 0, None),
            (Side.BUY, 1, None),
        ]


class TestSettings:
    def test_modes_and_steps_are_validated(self):
        with pytest.raises(ValueError):
            Ladder(mode="grid")
        with pytest.raises(ValueError):
            Ladder(steps=((D("10"), D("10")), (D("5"), D("5"))))
        assert Ladder().mode == MODE_ANCHOR
        assert Ladder().total_dollars == D("35")

    def test_steps_parse_and_validate(self):
        assert parse_steps("10:10, 5:5") == ((D("5"), D("5")), (D("10"), D("10")))
        with pytest.raises(ValueError):
            parse_steps("5:-5")

    def test_levels(self):
        assert Ladder().buy_level(ANCHOR, 1) == D("90")
        assert Ladder().sell_level(ANCHOR, 2) == D("120")


class TestTrendHelpers:
    def test_the_average_includes_the_bar_and_waits_until_it_exists(self):
        candles = make_candles([10, 12, 11, 9, 14])
        above = above_trend(candles, 3)
        assert list(above.values()) == [11 > 11, 9 > D("32") / 3, 14 > D("34") / 3]
        assert candles[0].start not in above and candles[1].start not in above
        assert trend_average(candles, 3) == D("34") / 3
        assert trend_average(candles[:2], 3) is None

    def test_bars_for_a_number_of_days(self):
        assert trend_bars(50, 60) == 1200
        assert trend_bars(1, 15) == 96

    def test_the_flat_anchor_is_the_high_since_a_moment(self):
        candles = make_candles([100, 120, 90, 95])
        assert flat_anchor(candles, since=None) == D("120")
        after_the_high = candles[1].end + timedelta(minutes=5)
        assert flat_anchor(candles, since=after_the_high) == D("95")
        assert flat_anchor(candles, since=datetime(2030, 1, 1, tzinfo=timezone.utc)) is None
