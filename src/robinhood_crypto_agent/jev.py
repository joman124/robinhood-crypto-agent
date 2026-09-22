"""Jev, TypeSafe AI's System One model, as the news labeler.

Jev answers typed questions about a piece of text with calibrated
probabilities, in roughly a tenth of a second. Here it reads each headline and
answers three questions -- which asset, which direction, how much impact --
and nothing else.

It never sees a price. Its reported weak spots include arithmetic, and the
indicators already do the numbers deterministically; a risk limit also has to
be a limit, not a probability. So the division is: Python owns every number,
Jev owns the text.

API: ``POST https://api.typesafe.ai/v1/systemone`` with a bearer token, a
``state`` (the text) and a map of ``questions``; each answer comes back typed,
with ``confidence`` summarising how concentrated its probabilities were.
"""

from __future__ import annotations

import math
import os
from typing import Any, Callable, Iterable, Mapping

from . import net
from .errors import AgentError
from .models import DIRECTION_SIGNS, MARKET_WIDE, MAX_IMPACT, NewsItem, NewsLabels
from .symbols import base_asset

JEV_URL = "https://api.typesafe.ai/v1/systemone"
JEV_MODEL = "jev-latest"
API_KEY_ENV = "TYPESAFE_API_KEY"
TIMEOUT_SECONDS = 10

#: The answer for a headline about none of the watchlist.
OTHER = "OTHER"

ASSET_NAMES = {
    "BTC": "Bitcoin",
    "ETH": "Ethereum",
    "SOL": "Solana",
    "DOGE": "Dogecoin",
    "LTC": "Litecoin",
    "BCH": "Bitcoin Cash",
    "AVAX": "Avalanche",
    "LINK": "Chainlink",
    "ADA": "Cardano",
    "SHIB": "Shiba Inu",
    "XRP": "XRP",
}

IMPACT_LEVELS = [
    "No effect on the price",
    "Minor: routine news",
    "Notable: could move the price a few percent",
    "Major: market-moving, such as a hack, an ETF ruling, an exchange failure or major regulation",
]


def news_questions(watchlist: Iterable[str]) -> dict[str, Any]:
    """The three questions, with the asset options built from the watchlist."""
    assets: dict[str, str] = {}
    for symbol in watchlist:
        code = base_asset(symbol)
        assets[code] = f"{ASSET_NAMES.get(code, code)} ({code})"
    assets[MARKET_WIDE] = "The crypto market as a whole: regulation, ETFs, exchanges, macro"
    assets[OTHER] = "Another coin, or not about crypto prices"
    return {
        "asset": {
            "type": "choice",
            "instructions": "Which asset is this crypto news mainly about?",
            "criteria": assets,
        },
        "direction": {
            "type": "choice",
            "instructions": (
                "For that asset, which way is this news likely to move the price "
                "over the next few hours?"
            ),
            "criteria": {
                "bullish": "Likely to push the price up",
                "bearish": "Likely to push the price down",
                "neutral": "No clear effect on the price",
            },
        },
        "impact": {
            "type": "score",
            "instructions": "How much could this news move the price?",
            "criteria": IMPACT_LEVELS,
        },
    }


def _unit(value: Any, what: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise AgentError(f"Jev {what} is not a number: {value!r}") from None
    if math.isnan(number) or not 0.0 <= number <= 1.0:
        raise AgentError(f"Jev {what} {number} is outside [0, 1]")
    return number


def _choice(answers: dict[str, Any], key: str, allowed: Iterable[str]) -> tuple[str, float]:
    answer = answers.get(key)
    if not isinstance(answer, dict):
        raise AgentError(f"Jev returned no '{key}' answer")
    choice = answer.get("choice")
    if choice not in set(allowed):
        raise AgentError(f"Jev '{key}' answer {choice!r} is not one of the options")
    return str(choice), _unit(answer.get("confidence"), f"'{key}' confidence")


def labels_from_answers(payload: Any, *, allowed_assets: Iterable[str]) -> NewsLabels:
    """Validate a Jev response into labels, or raise -- never guess a field."""
    answers = payload.get("answers") if isinstance(payload, dict) else None
    if not isinstance(answers, dict):
        raise AgentError("Jev response has no answers")
    asset, asset_confidence = _choice(answers, "asset", allowed_assets)
    direction, direction_confidence = _choice(answers, "direction", DIRECTION_SIGNS)

    impact_answer = answers.get("impact")
    if not isinstance(impact_answer, dict):
        raise AgentError("Jev returned no 'impact' answer")
    try:
        impact = float(impact_answer.get("score"))
    except (TypeError, ValueError):
        raise AgentError(f"Jev impact score is not a number: {impact_answer!r}") from None
    if math.isnan(impact) or not 0.0 <= impact <= MAX_IMPACT:
        raise AgentError(f"Jev impact score {impact} is outside [0, {MAX_IMPACT}]")

    return NewsLabels(
        asset=asset,
        asset_confidence=asset_confidence,
        direction=direction,
        direction_confidence=direction_confidence,
        impact=impact,
        model=str(payload.get("model", "")),
    )


class JevClient:
    """Labels one headline per call."""

    def __init__(
        self,
        api_key: str,
        *,
        url: str = JEV_URL,
        model: str = JEV_MODEL,
        http: Callable[..., Any] = net.request_json,
    ) -> None:
        if not api_key.strip():
            raise AgentError(f"{API_KEY_ENV} is empty")
        self._api_key = api_key.strip()
        self._url = url
        self._model = model
        self._http = http

    @classmethod
    def from_env(cls, environ: Mapping[str, str] = os.environ) -> "JevClient | None":
        key = environ.get(API_KEY_ENV)
        return cls(key) if key else None

    def label(self, item: NewsItem, watchlist: Iterable[str]) -> NewsLabels:
        questions = news_questions(watchlist)
        state = f"{item.title}\n\n{item.summary}" if item.summary else item.title
        payload = self._http(
            "POST",
            self._url,
            headers={"Authorization": f"Bearer {self._api_key}"},
            body={"state": state, "model": self._model, "questions": questions},
            timeout=TIMEOUT_SECONDS,
        )
        return labels_from_answers(payload, allowed_assets=questions["asset"]["criteria"])
