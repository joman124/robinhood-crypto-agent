"""Crypto pair symbol normalization.

The RobinHood MCP tools are not self-consistent about pair spelling:
``get_currency_pairs`` returns the hyphenated form (``BTC-USD``) while
``get_crypto_quotes`` returns it unhyphenated (``BTCUSD``). Cross-referencing a
quote against its pair constraints therefore needs a canonical form, or a
watchlist entry silently fails to match its own quote and the symbol looks
untradable for no visible reason.

Canonical form here is hyphenated and upper-case: ``BTC-USD``.
"""

from __future__ import annotations

#: Quote currencies a pair may be denominated in, longest first so that a
#: greedy suffix match prefers ``USDC`` over ``USD`` when splitting ``XUSDC``.
QUOTE_CURRENCIES: tuple[str, ...] = ("USDC", "USDT", "USD")


def canonical(symbol: str) -> str:
    """Return the canonical hyphenated, upper-case pair symbol.

    ``BTCUSD``, ``btc-usd`` and ``BTC/USD`` all become ``BTC-USD``. A bare asset
    symbol (``BTC``) is assumed to be USD-quoted, matching the order tools,
    which accept a bare asset symbol and resolve it to a pair.
    """
    text = symbol.strip().upper().replace("/", "-").replace("_", "-")
    if not text:
        return text
    if "-" in text:
        base, _, quote = text.partition("-")
        return f"{base}-{quote}" if quote else base

    for quote in QUOTE_CURRENCIES:
        if text.endswith(quote) and len(text) > len(quote):
            return f"{text[: -len(quote)]}-{quote}"
    return f"{text}-USD"


def unhyphenated(symbol: str) -> str:
    """The form ``get_crypto_quotes`` echoes back in its ``symbol`` field."""
    return canonical(symbol).replace("-", "")


def base_asset(symbol: str) -> str:
    """The asset code alone (``BTC`` from ``BTC-USD``)."""
    return canonical(symbol).partition("-")[0]


def quote_currency(symbol: str) -> str:
    """The quote currency code (``USD`` from ``BTC-USD``)."""
    return canonical(symbol).partition("-")[2] or "USD"


def same_pair(left: str, right: str) -> bool:
    """Whether two spellings refer to the same pair."""
    return canonical(left) == canonical(right)
