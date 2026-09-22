"""A read-only client for Robinhood's Crypto Trading API.

This is the first code in the package that holds Robinhood credentials, so its
shape is the safety argument: it signs GET requests for quotes, pairs, holdings
and the account, and it has **no method that creates or cancels an order**.
Orders still go only through Claude Code's MCP tools behind a human approval
(CLAUDE.md). When unattended execution is built (docs/autonomy.md, Phase 2),
the order call belongs behind the approval gate, not bolted on here.

Authentication, per Robinhood's docs: every request carries ``x-api-key``,
``x-timestamp`` (Unix seconds; rejected after 30s) and ``x-signature`` -- a
base64 Ed25519 signature over ``api_key + timestamp + path + method + body``,
where ``path`` includes the query string. The private key is the base64 32-byte
seed produced during Robinhood's API key setup.
"""

from __future__ import annotations

import base64
import binascii
import os
import re
import time
from typing import Any, Callable, Iterable, Mapping
from urllib.parse import urlsplit

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)

from . import net
from .errors import ConfigError
from .mcp.parse import parse_accounts, parse_currency_pairs, parse_positions, parse_quotes
from .models import Account, PairConstraints, Position, Quote
from .symbols import canonical

BASE_URL = "https://trading.robinhood.com"
API_KEY_ENV = "ROBINHOOD_API_KEY"
PRIVATE_KEY_ENV = "ROBINHOOD_PRIVATE_KEY"

#: Pages of holdings to follow before stopping. A spot account with more than
#: a few hundred assets is not this agent's use case.
MAX_PAGES = 10

_SYMBOL = re.compile(r"^[A-Z0-9]+-[A-Z]+$")

Http = Callable[..., Any]


def generate_key_pair() -> tuple[str, str]:
    """A new Ed25519 key pair as ``(private seed, public key)``, both base64.

    The private half is what ``ROBINHOOD_PRIVATE_KEY`` holds and only ever
    signs requests locally; the public half is what Robinhood's "Add key" form
    asks for.
    """
    key = Ed25519PrivateKey.generate()
    seed = key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    public = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return base64.b64encode(seed).decode("ascii"), base64.b64encode(public).decode("ascii")


def load_private_key(encoded: str) -> Ed25519PrivateKey:
    """Decode the base64 Ed25519 seed Robinhood's key setup produces.

    Some tools export ``seed || public_key`` (64 bytes) instead of the bare
    seed. That is accepted only when its second half really is the seed's
    public key, so a truncated or mangled key fails here, not at Robinhood.
    """
    try:
        raw = base64.b64decode(encoded.strip(), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ConfigError(f"{PRIVATE_KEY_ENV} is not valid base64") from exc

    if len(raw) == 64:
        key = Ed25519PrivateKey.from_private_bytes(raw[:32])
        public = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        if public != raw[32:]:
            raise ConfigError(
                f"{PRIVATE_KEY_ENV} is 64 bytes but its halves do not match; "
                "paste the 32-byte private seed"
            )
        return key
    if len(raw) != 32:
        raise ConfigError(
            f"{PRIVATE_KEY_ENV} must decode to a 32-byte Ed25519 seed, got {len(raw)} bytes"
        )
    return Ed25519PrivateKey.from_private_bytes(raw)


class RobinhoodClient:
    """Signed, read-only access to Robinhood's Crypto Trading API."""

    def __init__(
        self,
        api_key: str,
        private_key: str,
        *,
        base_url: str = BASE_URL,
        http: Http = net.request_json,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not api_key.strip():
            raise ConfigError(f"{API_KEY_ENV} is empty")
        if not base_url.startswith("https://"):
            raise ConfigError(f"refusing to send Robinhood credentials to {base_url}")
        self._api_key = api_key.strip()
        self._key = load_private_key(private_key)
        self._base_url = base_url.rstrip("/")
        self._http = http
        self._clock = clock

    @classmethod
    def from_env(cls, environ: Mapping[str, str] = os.environ) -> "RobinhoodClient | None":
        """A client from the environment, or ``None`` when either key is unset."""
        api_key = environ.get(API_KEY_ENV, "")
        private_key = environ.get(PRIVATE_KEY_ENV, "")
        if not api_key or not private_key:
            return None
        return cls(api_key, private_key)

    def signed_headers(self, method: str, path: str, body: str = "") -> dict[str, str]:
        timestamp = str(int(self._clock()))
        message = f"{self._api_key}{timestamp}{path}{method}{body}"
        signature = self._key.sign(message.encode("utf-8"))
        return {
            "x-api-key": self._api_key,
            "x-timestamp": timestamp,
            "x-signature": base64.b64encode(signature).decode("ascii"),
        }

    def _get(self, path: str) -> Any:
        return self._http(
            "GET", self._base_url + path, headers=self.signed_headers("GET", path)
        )

    def _get_all(self, path: str) -> list[dict[str, Any]]:
        """Follow ``next`` links; each page is signed over its own path."""
        rows: list[dict[str, Any]] = []
        for _ in range(MAX_PAGES):
            payload = self._get(path)
            if not isinstance(payload, dict):
                break
            rows.extend(r for r in payload.get("results") or [] if isinstance(r, dict))
            nxt = payload.get("next")
            if not nxt or not isinstance(nxt, str):
                break
            parts = urlsplit(nxt)
            path = parts.path + (f"?{parts.query}" if parts.query else "")
        return rows

    # -- reads -------------------------------------------------------------

    def best_bid_ask(self, symbols: Iterable[str]) -> list[Quote]:
        """Spread-inclusive bid and ask: the prices a sell gets and a buy pays."""
        path = "/api/v1/crypto/marketdata/best_bid_ask/" + _symbol_query(symbols)
        return parse_quotes(self._get(path))

    def trading_pairs(self, symbols: Iterable[str]) -> list[PairConstraints]:
        path = "/api/v1/crypto/trading/trading_pairs/" + _symbol_query(symbols)
        return parse_currency_pairs(self._get(path))

    def holdings(self) -> list[Position]:
        return parse_positions(self._get_all("/api/v1/crypto/trading/holdings/"))

    def account(self) -> Account | None:
        accounts = parse_accounts(self._get("/api/v1/crypto/trading/accounts/"))
        return accounts[0] if accounts else None


def _symbol_query(symbols: Iterable[str]) -> str:
    """``?symbol=BTC-USD&symbol=ETH-USD``, built by hand so the signed path is exact."""
    pairs = [canonical(s) for s in symbols]
    for pair in pairs:
        # Canonical pairs need no URL escaping; anything else would make the
        # signed path and the requested path differ.
        if not _SYMBOL.match(pair):
            raise ConfigError(f"not a valid pair symbol: {pair!r}")
    return ("?" + "&".join(f"symbol={p}" for p in pairs)) if pairs else ""
