"""Accept/decline decisions made outside the terminal.

The dashboard lets a human accept or decline a proposal from a web page. This
module is how that decision gets back to the agent **without weakening the
approval gate**.

The safety argument, in full
----------------------------
``ApprovalGate`` requires an explicit human instruction naming a proposal id.
A recorded decision satisfies exactly that: it names one proposal id, it was
made by a human, and it is stored verbatim. So a web accept is fed through the
*same* gate as a typed approval — it still faces the kill switch, the live
price-drift re-check, the remaining-quantity accounting and the order-contract
validation.

What a decision is **not**:

* It is not an order. Nothing here submits anything; the dashboard has no
  Robinhood credentials and no way to reach Robinhood.
* It is not a risk override. A decision on a risk-blocked proposal is refused
  by the gate exactly as a typed approval would be — ``OVERRIDE RISK CHECK``
  stays a deliberate, typed act and is never something a button can do.
* It is not permanent consent. It names one proposal, and the gate still
  refuses it once the proposal is filled or the price has drifted.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any

from .errors import AgentError
from .models import parse_timestamp, utcnow

#: Decision text is echoed into the approval gate, so it is length-bounded and
#: stripped of control characters before it is ever stored or replayed.
MAX_NOTE_LENGTH = 500
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

#: The risk-override phrase, matched loosely (any whitespace, any case) so it
#: cannot be smuggled through a decision note. ``ApprovalGate`` looks for this
#: phrase in the approval text, and a decision's note is interpolated into that
#: text -- so without this, a free-text field on a web form would be able to
#: execute a risk-BLOCKED proposal. Overriding the risk engine stays a typed,
#: deliberate human act; it is never something a button can do.
_OVERRIDE_PATTERN = re.compile(r"override\s+risk\s+check", re.IGNORECASE)


class DecisionKind(str, Enum):
    ACCEPT = "accept"
    DECLINE = "decline"


@dataclass(frozen=True)
class Decision:
    """One human accept/decline against one proposal."""

    proposal_id: str
    kind: DecisionKind
    decided_at: datetime
    source: str = "dashboard"
    actor: str = "owner"
    note: str = ""

    @property
    def approval_text(self) -> str:
        """The text handed to :class:`ApprovalGate`.

        It names the proposal id, which is what the gate requires. It
        deliberately never contains the risk-override phrase — see the module
        docstring.
        """
        base = f"{self.kind.value} proposal {self.proposal_id} via {self.source} by {self.actor}"
        return f"{base} -- {self.note}" if self.note else base

    def to_dict(self) -> dict[str, Any]:
        return {
            "proposal_id": self.proposal_id,
            "decision": self.kind.value,
            "decided_at": self.decided_at.isoformat(),
            "source": self.source,
            "actor": self.actor,
            "note": self.note,
        }


def sanitize_note(note: Any) -> str:
    """Strip control characters and bound the length of free text.

    The note travels from a web form into the approval text and then into the
    audit log, so it is treated as untrusted input at the boundary. The
    risk-override phrase is redacted here specifically: see
    :data:`_OVERRIDE_PATTERN`.
    """
    if note is None:
        return ""
    text = _CONTROL_CHARS.sub("", str(note))
    text = _OVERRIDE_PATTERN.sub("[redacted]", text).strip()
    return text[:MAX_NOTE_LENGTH]


def decision_from_dict(data: dict[str, Any]) -> Decision:
    """Parse a decision as it arrives from the dashboard."""
    if not isinstance(data, dict):
        raise AgentError("a decision must be an object")

    proposal_id = str(data.get("proposal_id", "")).strip()
    if not proposal_id:
        raise AgentError("a decision must name a proposal_id")
    if not re.fullmatch(r"[0-9a-f]{6,64}", proposal_id):
        # Proposal ids are hex digests. Rejecting anything else stops arbitrary
        # text reaching the approval gate's id matcher.
        raise AgentError(f"{proposal_id!r} is not a valid proposal id")

    raw_kind = str(data.get("decision", data.get("kind", ""))).lower().strip()
    try:
        kind = DecisionKind(raw_kind)
    except ValueError:
        raise AgentError(
            f"decision must be 'accept' or 'decline', got {raw_kind!r}"
        ) from None

    raw_time = data.get("decided_at")
    try:
        decided_at = parse_timestamp(str(raw_time)) if raw_time else utcnow()
    except ValueError:
        decided_at = utcnow()

    return Decision(
        proposal_id=proposal_id,
        kind=kind,
        decided_at=decided_at,
        source=sanitize_note(data.get("source") or "dashboard") or "dashboard",
        actor=sanitize_note(data.get("actor") or "owner") or "owner",
        note=sanitize_note(data.get("note")),
    )
