"""The approval gate.

This is the narrowest point in the system: a proposal can only become an order
payload by passing through :meth:`ApprovalGate.authorize`, and that method
demands a human approval naming a specific proposal id.

The rules it enforces, and why each one exists:

* **Approval must name the proposal id.** "That looks good", "yes", and general
  enthusiasm are not approvals -- they are ambiguous about *which* proposal,
  and in a report listing four symbols that ambiguity is the whole problem.
* **A risk-blocked proposal needs an explicit override phrase** in the same
  message as the approval. Overriding is then recorded as an override, so the
  audit log distinguishes "the limits allowed this" from "a human overrode the
  limits".
* **The live price is re-checked against the proposal's reference price.** A
  proposal computed ten minutes ago describes a market that may no longer
  exist; past the drift tolerance the gate refuses and asks for a fresh
  proposal rather than filling against a stale quote.
* **Already-filled quantity is subtracted.** A staged plan fills over several
  tranches, and re-approving one must not silently double the position.
* **Only a proposal can be approved.** ``rhca run`` logs every candidate,
  including ones the trigger held back (``not_escalated``) and ones System 2
  passed on (``declined_by_system2``). Those are records, not proposals. To act
  on one anyway, run ``rhca analyze`` for a fresh proposal and approve that.

The gate returns a *payload*, not a filled order. Calling the MCP tool remains
a separate, deliberate act.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal

from ..audit import AuditLog
from ..config import AgentConfig
from ..errors import ApprovalError, KillSwitchEngaged
from ..mcp.contract import CRYPTO_TOOLS
from ..models import ExecutionMode, OrderRequest, Proposal, ProposalStatus, Quote
from ..numeric import ZERO, abs_pct_drift
from ..symbols import same_pair
from .kill_switch import KillSwitch
from .orders import build_order_request

#: The exact phrase required to execute a risk-blocked proposal.
OVERRIDE_PHRASE = "OVERRIDE RISK CHECK"


@dataclass(frozen=True)
class ExecutionAuthorization:
    """Permission to submit one specific order payload."""

    proposal_id: str
    tranche_index: int
    request: OrderRequest
    overridden: bool
    drift_pct: Decimal
    notes: list[str] = field(default_factory=list)

    def describe(self) -> str:
        lines = [
            f"authorized: proposal {self.proposal_id}, tranche {self.tranche_index}",
            f"price drift since proposal: {self.drift_pct:.3f}%",
        ]
        if self.overridden:
            lines.append("RISK OVERRIDE: the risk engine blocked this and a human overrode it")
        lines.extend(self.notes)
        return "\n".join(lines)


class ApprovalGate:
    """Validates a human approval and produces the order payload it authorizes."""

    def __init__(
        self,
        config: AgentConfig,
        *,
        kill_switch: KillSwitch | None = None,
        audit: AuditLog | None = None,
    ) -> None:
        self.config = config
        self.kill_switch = kill_switch or KillSwitch(config.kill_switch_path)
        self.audit = audit or AuditLog(config.audit_path)

    def authorize(
        self,
        proposal: Proposal,
        *,
        approval_text: str,
        live_quote: Quote,
        tranche_index: int = 0,
        ref_id: str | None = None,
        tool: str = CRYPTO_TOOLS["place"],
        allow_override: bool = True,
    ) -> ExecutionAuthorization:
        """Authorize one tranche, or raise explaining why not.

        ``allow_override`` is set to ``False`` for approvals that did not come
        from a human typing them -- a recorded dashboard decision, say. Such an
        approval can execute a proposal that *passed* the risk engine, but can
        never override one that was blocked, whatever its text says. This is
        belt-and-braces with the redaction in ``decisions.sanitize_note``: two
        independent things would have to fail for a web button to override a
        risk block.
        """
        notes: list[str] = []

        if self.config.execution_mode is not ExecutionMode.PROPOSE_ONLY:
            raise ApprovalError(
                f"execution_mode is {self.config.execution_mode.value!r}; this codebase "
                "only implements 'propose_only'."
            )

        state = self.kill_switch.state()
        if state.engaged:
            raise KillSwitchEngaged(
                f"{state.describe()} -- no proposal may be executed until it is released"
            )

        if proposal.status in (ProposalStatus.NOT_ESCALATED, ProposalStatus.DECLINED_BY_SYSTEM2):
            raise ApprovalError(
                f"{proposal.proposal_id} is {proposal.status.value}: rhca run logged it but "
                "did not propose it. To act on the idea anyway, run `rhca analyze` for a "
                "fresh proposal and approve that one by its id."
            )

        overridden = self._check_approval(
            proposal, approval_text, allow_override=allow_override
        )
        if overridden:
            notes.append(
                "risk check was overridden by explicit human instruction; "
                "this is logged as an override, not a normal execution"
            )

        drift = self._check_drift(proposal, live_quote)
        remaining = self._check_remaining(proposal, tranche_index)
        notes.append(f"{remaining} {proposal.symbol} remains unfilled on this proposal")

        request = build_order_request(
            proposal,
            config=self.config,
            tranche_index=tranche_index,
            tool=tool,
            ref_id=ref_id,
        )

        return ExecutionAuthorization(
            proposal_id=proposal.proposal_id,
            tranche_index=tranche_index,
            request=request,
            overridden=overridden,
            drift_pct=drift,
            notes=notes,
        )

    # -- checks -----------------------------------------------------------

    def _check_approval(
        self, proposal: Proposal, approval_text: str, *, allow_override: bool = True
    ) -> bool:
        text = (approval_text or "").strip()
        if not text:
            raise ApprovalError(
                "no approval text supplied. An execution requires an explicit human "
                f"instruction naming proposal {proposal.proposal_id}."
            )

        # Word-boundary match so an id is not accepted as a substring of a longer
        # token, and so a different proposal's id cannot satisfy this one.
        if not re.search(rf"\b{re.escape(proposal.proposal_id)}\b", text):
            raise ApprovalError(
                f"the approval does not name proposal {proposal.proposal_id}. "
                "Approval must identify the proposal by its id -- 'that looks good' "
                "is not approval. Ask which proposal is meant."
            )

        if proposal.risk.passed:
            if OVERRIDE_PHRASE in text:
                # Nothing to override; saying so prevents the phrase becoming a
                # habitual incantation attached to every approval.
                raise ApprovalError(
                    f"proposal {proposal.proposal_id} passed the risk check, so "
                    f"'{OVERRIDE_PHRASE}' does not apply. Re-approve without it."
                )
            return False

        failures = ", ".join(f.rule for f in proposal.risk.blocking_failures)
        if not allow_override:
            raise ApprovalError(
                f"proposal {proposal.proposal_id} was blocked by the risk engine "
                f"({failures}). This approval did not come from a typed human "
                "instruction, and a recorded decision can never override a risk "
                "block. Override it deliberately from the terminal, or let it stand."
            )
        if OVERRIDE_PHRASE not in text:
            raise ApprovalError(
                f"proposal {proposal.proposal_id} was blocked by the risk engine "
                f"({failures}). To execute it anyway, the approval must contain the "
                f"exact phrase '{OVERRIDE_PHRASE}'."
            )
        return True

    def _check_drift(self, proposal: Proposal, live_quote: Quote) -> Decimal:
        if not same_pair(live_quote.symbol, proposal.symbol):
            raise ApprovalError(
                f"the live quote is for {live_quote.symbol} but the proposal is for "
                f"{proposal.symbol}"
            )
        if live_quote.mark <= ZERO:
            raise ApprovalError("the live quote has no usable mark price")

        drift = abs_pct_drift(live_quote.mark, proposal.reference_price)
        tolerance = self.config.risk.price_drift_tolerance_pct
        if drift > tolerance:
            raise ApprovalError(
                f"the price has moved {drift:.3f}% since proposal "
                f"{proposal.proposal_id} was created (tolerance {tolerance}%). "
                f"Reference {proposal.reference_price}, live {live_quote.mark}. "
                "Generate a fresh proposal rather than filling against a stale quote."
            )
        return drift

    def _check_remaining(self, proposal: Proposal, tranche_index: int) -> Decimal:
        planned = proposal.plan.total_quantity
        already = self.audit.filled_quantity_for(proposal.proposal_id)
        remaining = planned - already
        if remaining <= ZERO:
            raise ApprovalError(
                f"proposal {proposal.proposal_id} is already filled "
                f"({already} of {planned}); re-approving it would add unplanned size."
            )

        tranche = next(
            (t for t in proposal.plan.tranches if t.index == tranche_index), None
        )
        if tranche is not None and tranche.quantity > remaining:
            raise ApprovalError(
                f"tranche {tranche_index} wants {tranche.quantity} but only {remaining} "
                f"remains unfilled on proposal {proposal.proposal_id}."
            )
        return remaining
