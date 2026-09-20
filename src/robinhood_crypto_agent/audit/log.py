from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from datetime import date as date_type
from pathlib import Path

from robinhood_crypto_agent.execution.kill_switch import is_kill_switch_engaged
from robinhood_crypto_agent.models import ExecutionRecord, Side, TradeProposal

DEFAULT_AUDIT_LOG_PATH = Path(__file__).resolve().parents[3] / "data" / "audit_log.jsonl"

_OPEN_POSITION_EPSILON = 1e-9


@dataclass
class DailyActivity:
    """Today's trading activity, replayed from the audit log - this is both
    the record and the data source the risk engine's daily caps are
    evaluated against."""

    date: date_type
    exposure_usd: float = 0.0
    realized_loss_usd: float = 0.0
    open_position_symbols: set[str] = field(default_factory=set)
    kill_switch_engaged: bool = False


class AuditLog:
    """Append-only JSONL log. Nothing in this class ever rewrites or deletes
    a line - corrections are new entries, never edits (see CLAUDE.md)."""

    def __init__(self, path: Path = DEFAULT_AUDIT_LOG_PATH):
        self.path = path

    def _append(self, entry: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        entry = {**entry, "logged_at": datetime.now(UTC).isoformat()}
        with self.path.open("a") as f:
            f.write(json.dumps(entry, default=str) + "\n")

    def log_proposal(self, proposal: TradeProposal) -> None:
        self._append({"type": "proposal", "proposal": proposal.model_dump(mode="json")})

    def log_rejection(self, proposal: TradeProposal, reason: str) -> None:
        self._append(
            {
                "type": "rejection",
                "proposal": proposal.model_dump(mode="json"),
                "reason": reason,
            }
        )

    def log_execution(self, record: ExecutionRecord) -> None:
        self._append({"type": "execution", "execution": record.model_dump(mode="json")})

    def read_entries(self) -> list[dict]:
        if not self.path.exists():
            return []
        entries: list[dict] = []
        with self.path.open() as f:
            for line in f:
                line = line.strip()
                if line:
                    entries.append(json.loads(line))
        return entries

    def find_proposal(self, proposal_id: str) -> dict | None:
        for entry in reversed(self.read_entries()):
            if entry.get("type") in ("proposal", "rejection"):
                proposal = entry["proposal"]
                if proposal.get("id") == proposal_id:
                    return proposal
        return None

    def read_daily_activity(self, for_date: date_type | None = None) -> DailyActivity:
        """Replay execution entries to compute today's exposure/realized
        loss, and ALL executions to compute currently-open positions (a
        position opened yesterday is still open today)."""
        for_date = for_date or datetime.now(UTC).date()
        activity = DailyActivity(date=for_date, kill_switch_engaged=is_kill_switch_engaged())

        net_qty: dict[str, float] = {}

        for entry in self.read_entries():
            if entry.get("type") != "execution":
                continue
            execution = entry["execution"]
            if execution.get("status") != "filled":
                continue

            symbol = execution["symbol"]
            side = Side(execution["side"])
            filled_qty = execution.get("filled_qty") or 0.0
            filled_price = execution.get("filled_price") or 0.0

            signed_qty = filled_qty if side is Side.BUY else -filled_qty
            net_qty[symbol] = net_qty.get(symbol, 0.0) + signed_qty

            timestamp = datetime.fromisoformat(execution["timestamp"])
            if timestamp.date() == for_date:
                activity.exposure_usd += filled_price * filled_qty
                realized_pnl = execution.get("realized_pnl_usd")
                if realized_pnl is not None and realized_pnl < 0:
                    activity.realized_loss_usd += -realized_pnl

        activity.open_position_symbols = {
            symbol for symbol, qty in net_qty.items() if qty > _OPEN_POSITION_EPSILON
        }
        return activity
