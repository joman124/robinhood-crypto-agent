"""Building the dashboard payload, and (optionally) pushing it.

A note on the "no network" claim
--------------------------------
Elsewhere this package states that it has no network access. That claim is
about **Robinhood**, and it still holds exactly: nothing here has Robinhood
credentials, knows a Robinhood URL, or can place an order. Claude remains the
only thing that can reach the broker.

:func:`build_payload` is pure — it reads local files and returns a dict, and is
what the tests exercise. :func:`push` is the one function in the package that
opens a socket, and it talks only to a dashboard URL the operator configures,
carrying proposals and their outcomes. If you would rather the package never
opened a socket at all, use ``rhca dashboard-export`` to write the JSON and
push it with curl or a cron job; nothing depends on :func:`push`.

What is deliberately **not** in the payload
-------------------------------------------
Account numbers, buying power, portfolio value, position sizes and order ids.
The dashboard's job is to show what the agent *suggested* and whether those
suggestions were any good. It does not need to know how much money is behind
them, and a dashboard that never receives that data cannot leak it.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from decimal import Decimal
from typing import Any, Sequence

from .audit import KIND_PROPOSAL, AuditLog
from .config import AgentConfig
from .decisions import Decision, decision_from_dict
from .errors import AgentError
from .models import utcnow
from .numeric import round_money
from .outcomes import (
    DEFAULT_HORIZON_BARS,
    DEFAULT_HURDLE_PCT,
    Outcome,
    aggregate,
    group_by,
    outcome_from_proposal_record,
)
from .store import PriceStore

PAYLOAD_VERSION = 1
DEFAULT_TIMEOUT_SECONDS = 20

#: Proposal fields the dashboard is allowed to see. Everything else in the
#: audit record -- including anything an account snapshot might carry -- is
#: dropped rather than filtered later.
PROPOSAL_FIELDS = (
    "proposal_id",
    "symbol",
    "side",
    "quantity",
    "reference_price",
    "notional",
    "status",
    "risk_passed",
    "risk_failures",
    "score",
    "confidence",
    "regime",
)


def build_payload(
    config: AgentConfig,
    *,
    audit: AuditLog | None = None,
    store: PriceStore | None = None,
    horizon_bars: int = DEFAULT_HORIZON_BARS,
    hurdle_pct: Decimal = DEFAULT_HURDLE_PCT,
    limit: int | None = None,
    repo_url: str | None = None,
) -> dict[str, Any]:
    """Assemble everything the dashboard renders, from local state only."""
    audit = audit or AuditLog(config.audit_path)
    store = store or PriceStore(config.price_store_path)

    candles_by_symbol = {
        symbol: store.candles(
            symbol, interval_minutes=config.strategy.bar_interval_minutes, limit=500
        )
        for symbol in store.symbols()
    }

    proposals: list[dict[str, Any]] = []
    outcomes: list[Outcome] = []

    records = list(audit.events(kind=KIND_PROPOSAL))
    if limit is not None and limit > 0:
        records = records[-limit:]

    for record in records:
        row = {field: record.get(field) for field in PROPOSAL_FIELDS}
        row["proposed_at"] = record.get("recorded_at")

        symbol = str(record.get("symbol", ""))
        outcome = outcome_from_proposal_record(
            record,
            candles_by_symbol.get(symbol, []),
            horizon_bars=horizon_bars,
            hurdle_pct=hurdle_pct,
        )
        if outcome is not None:
            outcomes.append(outcome)
            row["outcome"] = outcome.to_dict()
        else:
            row["outcome"] = None

        # A proposal the risk engine blocked is shown, and is not actionable.
        row["actionable"] = bool(record.get("risk_passed"))
        proposals.append(row)

    proposals.sort(key=lambda r: str(r.get("proposed_at") or ""), reverse=True)

    activity = audit.daily_activity()

    return {
        "version": PAYLOAD_VERSION,
        "generated_at": utcnow().isoformat(),
        "repo_url": repo_url,
        "execution_mode": config.execution_mode.value,
        "watchlist": list(config.watchlist),
        "scoring": {
            "horizon_bars": horizon_bars,
            "hurdle_pct": str(hurdle_pct),
            "bar_interval_minutes": config.strategy.bar_interval_minutes,
        },
        "proposals": proposals,
        "stats": {
            "overall": aggregate(outcomes).to_dict(),
            "by_regime": {k: v.to_dict() for k, v in group_by(outcomes, "regime").items()},
            "by_symbol": {k: v.to_dict() for k, v in group_by(outcomes, "symbol").items()},
            "by_side": {k: v.to_dict() for k, v in group_by(outcomes, "side").items()},
        },
        "today": {
            "executions": activity.execution_count,
            "executed_notional": str(round_money(activity.executed_notional)),
            "realized_pnl": str(round_money(activity.realized_pnl)),
            "proposals": activity.proposal_count,
        },
    }


def _request(
    url: str, *, token: str, method: str, body: dict[str, Any] | None = None, timeout: int
) -> Any:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Authorization", f"Bearer {token}")
    request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:400]
        raise AgentError(f"{method} {url} failed: HTTP {exc.code} {detail}") from exc
    except urllib.error.URLError as exc:
        raise AgentError(f"{method} {url} failed: {exc.reason}") from exc
    if not raw.strip():
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AgentError(f"{url} returned a non-JSON response: {exc}") from exc


def push(
    payload: dict[str, Any],
    *,
    base_url: str,
    token: str,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """POST the payload to the dashboard. The only socket this package opens."""
    if not base_url.startswith("https://") and "localhost" not in base_url:
        # A bearer token over plaintext would be readable in transit.
        raise AgentError(
            f"refusing to send a bearer token to a non-HTTPS URL: {base_url}"
        )
    url = base_url.rstrip("/") + "/api/proposals"
    return _request(url, token=token, method="POST", body=payload, timeout=timeout) or {}


def fetch_decisions(
    *, base_url: str, token: str, timeout: int = DEFAULT_TIMEOUT_SECONDS
) -> list[Decision]:
    """GET accept/decline decisions recorded on the dashboard.

    Every row is parsed through :func:`decisions.decision_from_dict`, which
    validates the proposal id and redacts the risk-override phrase -- the
    dashboard is treated as untrusted input even though the operator owns it.
    """
    if not base_url.startswith("https://") and "localhost" not in base_url:
        raise AgentError(
            f"refusing to send a bearer token to a non-HTTPS URL: {base_url}"
        )
    url = base_url.rstrip("/") + "/api/decisions"
    response = _request(url, token=token, method="GET", timeout=timeout) or {}
    rows = response.get("decisions") if isinstance(response, dict) else response
    if not isinstance(rows, list):
        return []

    decisions: list[Decision] = []
    for row in rows:
        try:
            decisions.append(decision_from_dict(row))
        except AgentError:
            # One malformed row must not discard the rest.
            continue
    return decisions


def latest_decision_for(decisions: Sequence[Decision], proposal_id: str) -> Decision | None:
    """The most recent decision for a proposal, since a mind can be changed."""
    matching = [d for d in decisions if d.proposal_id == proposal_id]
    if not matching:
        return None
    return max(matching, key=lambda d: d.decided_at)
