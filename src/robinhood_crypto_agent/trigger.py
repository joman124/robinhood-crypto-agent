"""The escalation trigger: "is confidence high?" between System 1 and System 2.

System 1 produces a candidate whenever a bar closes or news changes its view.
Most candidates should not cost a Sonnet call. One is escalated only when all
of these hold:

1. **It passed every risk rule.** System 2 is never asked about a trade the risk
   engine would refuse, so it can never be talked into one.
2. **Composite confidence and |score| clear the trigger**, which sits above the
   risk engine's own minimums.
3. **The symbol is outside its cooldown**, so one persistent signal is one
   escalation, not one a minute.
4. **Today's escalation count is under the cap**, which is also the cap on
   Sonnet spend.

Every "no" carries a reason, and the candidate is logged with it anyway:
tuning these thresholds needs the outcomes of what they held back.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from .config import PipelineConfig
from .models import JsonMixin, Proposal


@dataclass(frozen=True)
class TriggerVerdict(JsonMixin):
    escalate: bool
    reason: str


class EscalationTrigger:
    def __init__(self, config: PipelineConfig) -> None:
        self.config = config

    def evaluate(
        self,
        proposal: Proposal,
        *,
        escalations_today: int,
        last_escalated_at: datetime | None,
        now: datetime,
    ) -> TriggerVerdict:
        view = proposal.view
        if not proposal.risk.passed:
            rules = ", ".join(f.rule for f in proposal.risk.blocking_failures)
            return TriggerVerdict(False, f"blocked by risk: {rules}")

        confidence = Decimal(str(round(view.confidence, 6)))
        if confidence < self.config.trigger_min_confidence:
            return TriggerVerdict(
                False,
                f"confidence {view.confidence:.3f} below the trigger's "
                f"{self.config.trigger_min_confidence}",
            )
        strength = Decimal(str(round(abs(view.score), 6)))
        if strength < self.config.trigger_min_abs_score:
            return TriggerVerdict(
                False,
                f"|score| {abs(view.score):.3f} below the trigger's "
                f"{self.config.trigger_min_abs_score}",
            )

        cooldown = timedelta(minutes=self.config.escalation_cooldown_minutes)
        if last_escalated_at is not None and now - last_escalated_at < cooldown:
            minutes = (now - last_escalated_at).total_seconds() / 60
            return TriggerVerdict(
                False,
                f"cooldown: {proposal.symbol} was escalated {minutes:.0f}m ago "
                f"({self.config.escalation_cooldown_minutes}m cooldown)",
            )
        if escalations_today >= self.config.max_escalations_per_day:
            return TriggerVerdict(
                False,
                f"daily escalation cap reached ({self.config.max_escalations_per_day})",
            )

        return TriggerVerdict(
            True,
            f"confidence {view.confidence:.3f} and |score| {abs(view.score):.3f} clear the trigger",
        )
