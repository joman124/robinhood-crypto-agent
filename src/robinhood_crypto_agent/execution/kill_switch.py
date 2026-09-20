"""A file-based kill switch.

The switch is the *presence of a file*, not a flag in a config object or a
variable in a process. That choice is deliberate:

* It can be engaged from outside the agent -- ``touch data/KILL_SWITCH`` from
  any shell stops the next proposal from becoming an order, with no session to
  find and no process to signal.
* It survives a crash and a restart. An in-memory flag does not, and the moment
  you most want a kill switch is right after something went wrong.
* It fails safe: if the file cannot be read for any reason, the switch reads as
  *engaged*. An unreadable switch means an unknown state, and unknown must
  block rather than permit.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ..models import parse_timestamp, utcnow

REASON_MANUAL = "manual"
REASON_DAILY_LOSS = "daily_loss_cap"


@dataclass(frozen=True)
class KillSwitchState:
    engaged: bool
    reason: str | None = None
    engaged_at: datetime | None = None

    def describe(self) -> str:
        if not self.engaged:
            return "kill switch: released"
        when = self.engaged_at.isoformat() if self.engaged_at else "unknown time"
        return f"kill switch: ENGAGED ({self.reason or 'no reason recorded'}) since {when}"


class KillSwitch:
    """Reads and writes the kill-switch file."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    def state(self) -> KillSwitchState:
        if not self.path.exists():
            return KillSwitchState(engaged=False)
        try:
            payload = json.loads(self.path.read_text() or "{}")
        except (OSError, json.JSONDecodeError):
            # Present but unreadable: treat as engaged. A switch whose state
            # cannot be determined must not read as "safe to trade".
            return KillSwitchState(
                engaged=True,
                reason="kill switch file is present but unreadable",
            )
        engaged_at = None
        raw_time = payload.get("engaged_at")
        if raw_time:
            try:
                engaged_at = parse_timestamp(str(raw_time))
            except ValueError:
                engaged_at = None
        return KillSwitchState(
            engaged=True,
            reason=payload.get("reason") or REASON_MANUAL,
            engaged_at=engaged_at,
        )

    @property
    def engaged(self) -> bool:
        return self.state().engaged

    def engage(self, reason: str = REASON_MANUAL) -> KillSwitchState:
        """Engage the switch. Engaging an already-engaged switch is a no-op.

        Keeping the original reason and timestamp matters: the first thing that
        tripped the switch is the interesting one, and overwriting it with a
        later manual engage would erase why trading stopped.
        """
        existing = self.state()
        if existing.engaged:
            return existing
        self.path.parent.mkdir(parents=True, exist_ok=True)
        engaged_at = utcnow()
        self.path.write_text(
            json.dumps({"reason": reason, "engaged_at": engaged_at.isoformat()}, indent=2)
            + "\n"
        )
        return KillSwitchState(engaged=True, reason=reason, engaged_at=engaged_at)

    def release(self) -> KillSwitchState:
        """Release the switch. Releasing a released switch is a no-op."""
        self.path.unlink(missing_ok=True)
        return KillSwitchState(engaged=False)
