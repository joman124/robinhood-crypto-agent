"""The read-only Robinhood Crypto API client: signing, key handling, REST shapes.

The REST payloads below have the field names and types of live responses,
captured on 2026-09-22 (every field is a string; quote timestamps carry
nanoseconds). The values are illustrative, not anyone's account.
"""

import base64
from decimal import Decimal

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from robinhood_crypto_agent.cli import main, read_dotenv
from robinhood_crypto_agent.errors import ConfigError
from robinhood_crypto_agent.robinhood import RobinhoodClient, generate_key_pair, load_private_key

SEED = bytes(range(32))
SEED_B64 = base64.b64encode(SEED).decode()
PUBLIC = Ed25519PrivateKey.from_private_bytes(SEED).public_key()
BASE = "https://trading.robinhood.com"


class FakeHttp:
    def __init__(self, responses=()):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, method, url, *, headers=None, body=None, timeout=None):
        self.calls.append((method, url, dict(headers or {})))
        return self.responses.pop(0)


def client(*responses):
    http = FakeHttp(responses)
    return RobinhoodClient("rh-key", SEED_B64, http=http, clock=lambda: 1_700_000_000), http


def verify(headers, path, method="GET"):
    message = f"rh-key{headers['x-timestamp']}{path}{method}".encode()
    PUBLIC.verify(base64.b64decode(headers["x-signature"]), message)  # raises if wrong


class TestSigning:
    def test_signature_covers_key_timestamp_path_and_method(self):
        rh, _ = client()
        path = "/api/v1/crypto/marketdata/best_bid_ask/?symbol=BTC-USD"
        headers = rh.signed_headers("GET", path)
        assert headers["x-api-key"] == "rh-key"
        assert headers["x-timestamp"] == "1700000000"
        verify(headers, path)

    def test_each_request_is_signed_over_the_exact_path_it_requests(self):
        rh, http = client({"results": []})
        rh.best_bid_ask(["BTC-USD", "ETHUSD"])
        method, url, headers = http.calls[0]
        path = "/api/v1/crypto/marketdata/best_bid_ask/?symbol=BTC-USD&symbol=ETH-USD"
        assert (method, url) == ("GET", BASE + path)
        verify(headers, path)

    def test_every_page_is_signed_over_its_own_path(self):
        rh, http = client(
            {"next": f"{BASE}/api/v1/crypto/trading/holdings/?cursor=abc", "results": []},
            {"next": None, "results": [{"asset_code": "BTC", "total_quantity": "0.5"}]},
        )
        assert [p.symbol for p in rh.holdings()] == ["BTC-USD"]
        _, url, headers = http.calls[1]
        assert url.endswith("/holdings/?cursor=abc")
        verify(headers, "/api/v1/crypto/trading/holdings/?cursor=abc")


class TestPrivateKey:
    def test_a_seed_plus_public_key_export_is_accepted(self):
        public = PUBLIC.public_bytes(Encoding.Raw, PublicFormat.Raw)
        key = load_private_key(base64.b64encode(SEED + public).decode())
        assert key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw) == public

    def test_mismatched_halves_are_refused(self):
        with pytest.raises(ConfigError, match="halves"):
            load_private_key(base64.b64encode(SEED + bytes(32)).decode())

    def test_the_wrong_length_is_refused(self):
        with pytest.raises(ConfigError, match="32-byte"):
            load_private_key(base64.b64encode(bytes(16)).decode())

    def test_non_base64_is_refused(self):
        with pytest.raises(ConfigError, match="base64"):
            load_private_key("definitely not base64!")

    def test_credentials_never_go_over_plain_http(self):
        with pytest.raises(ConfigError):
            RobinhoodClient("rh-key", SEED_B64, base_url="http://trading.robinhood.com")

    def test_from_env_is_none_until_both_keys_are_set(self):
        assert RobinhoodClient.from_env({"ROBINHOOD_API_KEY": "k"}) is None
        env = {"ROBINHOOD_API_KEY": "k", "ROBINHOOD_PRIVATE_KEY": SEED_B64}
        assert RobinhoodClient.from_env(env) is not None


def test_the_client_has_no_way_to_place_or_cancel_an_order():
    """Shadow mode's guarantee is structural: there is nothing here to call."""
    public = [name for name in dir(RobinhoodClient) if not name.startswith("_")]
    assert not [n for n in public if any(w in n for w in ("order", "cancel", "place", "trade"))]


class TestRestShapes:
    BEST_BID_ASK = {
        "results": [
            {
                "symbol": "BTC-USD",
                "timestamp": "2026-09-22T03:58:31.095125625Z",
                "price": "81224.22",
                "bid_inclusive_of_sell_spread": "80466.27",
                "sell_spread": "0.0093",
                "ask_inclusive_of_buy_spread": "81982.17",
                "buy_spread": "0.0093",
            }
        ]
    }

    def test_quotes_are_the_spread_inclusive_prices(self):
        rh, _ = client(self.BEST_BID_ASK)
        [quote] = rh.best_bid_ask(["BTC-USD"])
        assert quote.symbol == "BTC-USD"
        assert quote.bid == Decimal("80466.27")
        assert quote.ask == Decimal("81982.17")
        assert quote.mark == Decimal("81224.22")

    def test_a_nanosecond_timestamp_is_robinhoods_time_not_the_fetch_time(self):
        """Rejected nanoseconds used to fall back to now -- a stale quote read as fresh."""
        rh, _ = client(self.BEST_BID_ASK)
        [quote] = rh.best_bid_ask(["BTC-USD"])
        assert quote.observed_at.isoformat() == "2026-09-22T03:58:31.095125+00:00"

    def test_without_a_price_field_the_mark_is_the_mid(self):
        row = dict(self.BEST_BID_ASK["results"][0])
        del row["price"]
        rh, _ = client({"results": [row]})
        [quote] = rh.best_bid_ask(["BTC-USD"])
        assert quote.mark == (Decimal("80466.27") + Decimal("81982.17")) / 2

    def test_one_malformed_quote_does_not_cost_the_others(self):
        good = self.BEST_BID_ASK["results"][0]
        bad = {**good, "symbol": "ETH-USD", "bid_inclusive_of_sell_spread": {"amount": "1"}}
        bad.pop("price")
        bad["ask_inclusive_of_buy_spread"] = "not a number"
        rh, _ = client({"results": [bad, good]})
        assert [q.symbol for q in rh.best_bid_ask(["BTC-USD", "ETH-USD"])] == ["BTC-USD"]

    def test_holdings_drop_empty_positions(self):
        rh, _ = client(
            {
                "next": None,
                "results": [
                    {
                        "account_number": "RH123",
                        "asset_code": "BTC",
                        "total_quantity": "0.5",
                        "quantity_available_for_trading": "0.5",
                    },
                    {"account_number": "RH123", "asset_code": "ETH", "total_quantity": "0"},
                ],
            }
        )
        [position] = rh.holdings()
        assert (position.symbol, position.quantity) == ("BTC-USD", Decimal("0.5"))

    def test_pairs_carry_increments_and_status(self):
        rh, _ = client(
            {
                "results": [
                    {
                        "asset_code": "BTC",
                        "quote_code": "USD",
                        "quote_increment": "0.01",
                        "asset_increment": "0.00000001",
                        "max_order_size": "20",
                        "min_order_size": "0.000001",
                        "status": "tradable",
                        "symbol": "BTC-USD",
                    },
                    {"asset_code": "XYZ", "symbol": "XYZ-USD", "status": "untradable"},
                ]
            }
        )
        btc, xyz = rh.trading_pairs(["BTC-USD", "XYZ-USD"])
        assert btc.quantity_increment == Decimal("0.00000001")
        assert btc.price_increment == Decimal("0.01")
        assert btc.tradable and not xyz.tradable

    def test_account_buying_power(self):
        rh, _ = client(
            {
                "account_number": "RH123",
                "status": "active",
                "buying_power": "1234.56",
                "buying_power_currency": "USD",
            }
        )
        assert rh.account().buying_power == Decimal("1234.56")


class TestKeygen:
    """``rhca keygen``: the one step of key setup that is easy to get wrong by hand."""

    def test_a_generated_pair_round_trips(self):
        private, public = generate_key_pair()
        derived = load_private_key(private).public_key()
        assert base64.b64encode(derived.public_bytes(Encoding.Raw, PublicFormat.Raw)).decode() == public

    def test_the_private_key_goes_to_env_and_only_the_public_key_is_shown(
        self, tmp_path, monkeypatch, capsys
    ):
        monkeypatch.delenv("ROBINHOOD_PRIVATE_KEY", raising=False)
        (tmp_path / ".env.example").write_text("# keys\nROBINHOOD_API_KEY=\nROBINHOOD_PRIVATE_KEY=\n")
        env = tmp_path / ".env"

        assert main(["--env-file", str(env), "keygen"]) == 0

        private = read_dotenv(env)["ROBINHOOD_PRIVATE_KEY"]
        public_bytes = load_private_key(private).public_key().public_bytes(
            Encoding.Raw, PublicFormat.Raw
        )
        out = capsys.readouterr().out
        assert private not in out
        assert base64.b64encode(public_bytes).decode() in out
        assert env.read_text().startswith("# keys")  # the template's comments survive

    def test_an_existing_key_is_never_silently_replaced(self, tmp_path, monkeypatch):
        monkeypatch.delenv("ROBINHOOD_PRIVATE_KEY", raising=False)
        env = tmp_path / ".env"
        env.write_text("ROBINHOOD_PRIVATE_KEY=already-registered\n")
        assert main(["--env-file", str(env), "keygen"]) == 1
        assert env.read_text() == "ROBINHOOD_PRIVATE_KEY=already-registered\n"
        assert main(["--env-file", str(env), "keygen", "--force"]) == 0
        assert read_dotenv(env)["ROBINHOOD_PRIVATE_KEY"] != "already-registered"

    def test_empty_values_in_env_count_as_unset(self, tmp_path):
        env = tmp_path / ".env"
        env.write_text("# c\nROBINHOOD_API_KEY=\nexport TYPESAFE_API_KEY=\"abc\"\n")
        assert read_dotenv(env) == {"TYPESAFE_API_KEY": "abc"}
