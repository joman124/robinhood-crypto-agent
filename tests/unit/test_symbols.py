from robinhood_crypto_agent.symbols import (
    base_asset,
    canonical,
    quote_currency,
    same_pair,
    unhyphenated,
)


def test_quote_and_pair_spellings_reconcile():
    """get_crypto_quotes returns BTCUSD; get_currency_pairs returns BTC-USD."""
    assert canonical("BTCUSD") == "BTC-USD"
    assert canonical("BTC-USD") == "BTC-USD"
    assert same_pair("BTCUSD", "btc-usd")


def test_longest_quote_currency_wins():
    """SOLUSDC must split as SOL/USDC, not SOLU/SDC or SOLUSD/C."""
    assert canonical("SOLUSDC") == "SOL-USDC"
    assert canonical("SOLUSDT") == "SOL-USDT"
    assert canonical("SOLUSD") == "SOL-USD"


def test_bare_asset_defaults_to_usd():
    assert canonical("BTC") == "BTC-USD"
    assert canonical("eth") == "ETH-USD"


def test_separators_are_normalized():
    assert canonical("ETH/USD") == "ETH-USD"
    assert canonical("ETH_USD") == "ETH-USD"


def test_accessors():
    assert unhyphenated("BTC-USD") == "BTCUSD"
    assert base_asset("DOGEUSD") == "DOGE"
    assert quote_currency("SOLUSDC") == "USDC"
