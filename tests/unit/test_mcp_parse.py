"""Parsers are tested against the real response shapes, captured live.

The payloads below are trimmed copies of actual RobinHood MCP responses, which
is what makes these tests worth anything -- a parser tested only against
payloads invented by the same person who wrote it proves nothing about the API.
"""

from decimal import Decimal

import pytest

from robinhood_crypto_agent.errors import AgentError
from robinhood_crypto_agent.mcp.parse import (
    next_cursor,
    parse_accounts,
    parse_currency_pairs,
    parse_order_response,
    parse_positions,
    parse_quotes,
    unwrap_results,
)

QUOTES = {
    "data": {
        "results": [
            {
                "symbol": "BTCUSD",
                "id": "3d961844-d360-45fc-989b-f6fca761d511",
                "bid_price": "80466.2691145",
                "bid_time": "2026-09-20T13:32:52.713-04:00",
                "ask_price": "81982.17105482",
                "ask_time": "2026-09-20T13:32:54.537-04:00",
                "mark_price": "81224.22008466",
                "open_price": "80469.17",
                "routing": "Market Maker Routing",
                "updated_at": "2026-09-20T13:32:55.452-04:00",
            }
        ]
    },
    "guide": "rendering advice that carries no data",
}

PAIRS = {
    "data": {
        "results": [
            {
                "id": "e92889b3-39ca-41bd-a802-9030ab7e9d26",
                "symbol": "BILL-USD",
                "display_symbol": "BILL-USD",
                "tradability": "tradable",
                "display_only": False,
                "min_order_size": "1",
                "max_order_size": "9900000",
                "min_order_quantity_increment": "0.1",
                "min_order_price_increment": "0.000001",
                "market_orders_only": False,
                "halted": True,
                "halted_regions": ["NY"],
            }
        ],
        "next": "http://edge/currency_pairs/?cursor=cD0yMDI2&limit=3",
    }
}


def test_quote_symbol_is_normalized_to_hyphenated():
    quote = parse_quotes(QUOTES)[0]
    assert quote.symbol == "BTC-USD"
    assert quote.mark == Decimal("81224.22008466")
    assert quote.previous_close == Decimal("80469.17")


def test_quote_timestamp_is_converted_to_utc():
    quote = parse_quotes(QUOTES)[0]
    assert quote.observed_at.isoformat() == "2026-09-20T17:32:55.452000+00:00"


def test_wide_market_maker_spread_is_measured():
    """The live BTC spread was ~1.87% -- the spread gate has to see that."""
    quote = parse_quotes(QUOTES)[0]
    assert Decimal("1.8") < quote.spread_pct < Decimal("1.9")


def test_zero_prices_mean_unavailable_not_free():
    payload = {"results": [{"symbol": "BTCUSD", "bid_price": "0", "ask_price": "0", "mark_price": "0"}]}
    assert parse_quotes(payload) == []


def test_mark_falls_back_to_mid_when_zero():
    payload = {
        "results": [
            {"symbol": "BTCUSD", "bid_price": "100", "ask_price": "102", "mark_price": "0"}
        ]
    }
    assert parse_quotes(payload)[0].mark == Decimal("101")


def test_one_sided_book_still_yields_a_quote():
    payload = {
        "results": [
            {"symbol": "BTCUSD", "bid_price": "0", "ask_price": "102", "mark_price": "101"}
        ]
    }
    quote = parse_quotes(payload)[0]
    assert quote.mark == Decimal("101")
    assert quote.bid == Decimal("101")  # filled with mark, so spread math is sane


def test_currency_pairs_carry_halts_and_increments():
    pair = parse_currency_pairs(PAIRS)[0]
    assert pair.symbol == "BILL-USD"
    assert pair.quantity_increment == Decimal("0.1")
    assert pair.halted and pair.halted_regions == ("NY",)
    assert not pair.globally_halted  # regional halt, not global


def test_global_halt_is_distinguished():
    payload = {"results": [{"symbol": "X-USD", "halted": True, "halted_regions": ["ALL"]}]}
    assert parse_currency_pairs(payload)[0].globally_halted


def test_market_orders_only_blocks_limit_orders():
    from robinhood_crypto_agent.models import OrderType

    payload = {"results": [{"symbol": "X-USD", "market_orders_only": True}]}
    pair = parse_currency_pairs(payload)[0]
    assert pair.supports(OrderType.MARKET)
    assert not pair.supports(OrderType.LIMIT)


def test_next_cursor_extracts_the_query_parameter():
    assert next_cursor(PAIRS) == "cD0yMDI2"
    assert next_cursor({"data": {"results": []}}) is None


def test_zero_quantity_positions_are_dropped():
    """A sold-out position must not consume the open-position cap."""
    payload = {
        "results": [
            {"currency": {"code": "BTC"}, "quantity": "0", "cost_basis": "0"},
            {"currency": {"code": "ETH"}, "quantity": "1.5", "cost_basis": "3000"},
        ]
    }
    positions = parse_positions(payload)
    assert [p.symbol for p in positions] == ["ETH-USD"]
    assert positions[0].quantity == Decimal("1.5")


def test_accounts_keep_both_number_spellings_distinct():
    payload = {"results": [{"account_number": "RH123", "rhs_account_number": "987654321"}]}
    account = parse_accounts(payload)[0]
    assert account.account_number == "RH123"
    assert account.rhs_account_number == "987654321"


def test_order_response_sums_execution_fills():
    payload = {
        "data": {
            "results": [
                {
                    "id": "ord-1",
                    "symbol": "BTC-USD",
                    "side": "buy",
                    "state": "partially_filled",
                    "quantity": "1.0",
                    "average_price": "100",
                    "executions": [{"quantity": "0.3"}, {"quantity": "0.2"}],
                }
            ]
        }
    }
    record = parse_order_response(payload, proposal_id="p1")
    assert record.filled_quantity == Decimal("0.5")
    assert record.requested_quantity == Decimal("1.0")
    assert record.notional == Decimal("50.00")


def test_order_response_without_executions_uses_cumulative_quantity():
    payload = {
        "results": [
            {
                "id": "o",
                "symbol": "BTC-USD",
                "side": "sell",
                "state": "filled",
                "quantity": "2",
                "cumulative_quantity": "2",
                "average_price": "10",
            }
        ]
    }
    assert parse_order_response(payload, proposal_id="p").filled_quantity == Decimal("2")


def test_order_response_requires_a_recognizable_side():
    with pytest.raises(AgentError, match="unrecognized side"):
        parse_order_response({"results": [{"symbol": "BTC-USD", "side": "hold"}]}, proposal_id="p")


def test_unwrap_handles_every_envelope_shape():
    assert unwrap_results({"data": {"results": [{"a": 1}]}}) == [{"a": 1}]
    assert unwrap_results({"results": [{"a": 1}]}) == [{"a": 1}]
    assert unwrap_results([{"a": 1}]) == [{"a": 1}]
    assert unwrap_results({"a": 1}) == [{"a": 1}]
    assert unwrap_results(None) == []
