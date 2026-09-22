"""The package's one HTTP helper.

Every outbound call -- Robinhood market data, news feeds, Jev, X, the
dashboard -- goes through here, so timeouts, the response-size cap and error
wording are decided once. This module moves bytes to URLs its callers choose;
nothing here knows what an order is.
"""

from __future__ import annotations

import http.client
import json
import urllib.error
import urllib.request
from typing import Any, Mapping

from .errors import AgentError

USER_AGENT = "rhca/0.1 (+https://github.com/joman124/robinhood-crypto-agent)"
DEFAULT_TIMEOUT_SECONDS = 20

#: A response larger than this is refused rather than read into memory. The
#: largest legitimate payload here is a busy RSS feed, well under 1 MB.
MAX_RESPONSE_BYTES = 5_000_000


class HttpError(AgentError):
    """A request failed. ``status`` is the HTTP code, or ``None`` for a network error.

    Callers branch on it: a 4xx means the request itself is wrong and retrying
    will not help, while ``None`` or a 5xx is worth trying again next cycle.
    """

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status

    @property
    def retryable(self) -> bool:
        return self.status is None or self.status >= 500 or self.status == 429


def request(
    method: str,
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    body: bytes | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> bytes:
    """Perform one request and return the raw body."""
    req = urllib.request.Request(url, data=body, method=method)
    req.add_header("User-Agent", USER_AGENT)
    for name, value in (headers or {}).items():
        req.add_header(name, value)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        detail = exc.read(400).decode(errors="replace")
        raise HttpError(f"{method} {url} failed: HTTP {exc.code} {detail}", exc.code) from exc
    except (OSError, http.client.HTTPException) as exc:
        # URLError and socket timeouts are OSErrors; a dropped connection
        # mid-body surfaces as an HTTPException.
        reason = getattr(exc, "reason", None) or exc
        raise HttpError(f"{method} {url} failed: {reason}") from exc
    if len(raw) > MAX_RESPONSE_BYTES:
        raise HttpError(f"{method} {url} returned more than {MAX_RESPONSE_BYTES} bytes")
    return raw


def request_json(
    method: str,
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    body: Any = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> Any:
    """Send ``body`` as JSON (when given) and parse a JSON response."""
    merged = dict(headers or {})
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        merged.setdefault("Content-Type", "application/json")
    raw = request(method, url, headers=merged, body=data, timeout=timeout)
    text = raw.decode("utf-8", errors="replace")
    if not text.strip():
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise HttpError(f"{url} returned a non-JSON response: {exc}") from exc
