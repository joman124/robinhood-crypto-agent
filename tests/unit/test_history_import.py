"""``import_recent_history``: a symbol Coinbase will not serve is reported.

``rhca run`` now bootstraps at startup, so an unreachable Coinbase -- or one
watchlist symbol it has no product for -- must not stop the loop from starting.
The failure is returned so each caller can decide: ``bootstrap-history`` was
asked for the import and says so loudly, ``run`` notes it and carries on.
"""

from robinhood_crypto_agent import cli
from robinhood_crypto_agent.config import AgentConfig
from robinhood_crypto_agent.errors import AgentError
from robinhood_crypto_agent.store import PriceStore


def config_for(tmp_path, watchlist):
    return AgentConfig(watchlist=watchlist, data_dir=tmp_path)


def test_one_unservable_symbol_does_not_stop_the_others(tmp_path, monkeypatch):
    config = config_for(tmp_path, ("BTC-USD", "NOPE-USD", "ETH-USD"))

    def fake_fetch(symbol, *, interval_minutes):
        if symbol == "NOPE-USD":
            raise AgentError("Coinbase has no product NOPE-USD")
        return []

    monkeypatch.setattr(cli, "fetch_coinbase_candles", fake_fetch)
    imported, failures = cli.import_recent_history(config, PriceStore(config.price_store_path))

    assert [symbol for symbol, _, _ in imported] == ["BTC-USD", "ETH-USD"]
    assert len(failures) == 1
    assert "NOPE-USD" in failures[0]


def test_every_symbol_failing_is_still_not_an_exception(tmp_path, monkeypatch):
    """The caller decides whether a total failure is fatal, not this function."""
    config = config_for(tmp_path, ("BTC-USD", "ETH-USD"))

    def fake_fetch(symbol, *, interval_minutes):
        raise AgentError("Coinbase is unreachable")

    monkeypatch.setattr(cli, "fetch_coinbase_candles", fake_fetch)
    imported, failures = cli.import_recent_history(config, PriceStore(config.price_store_path))

    assert imported == []
    assert len(failures) == 2
