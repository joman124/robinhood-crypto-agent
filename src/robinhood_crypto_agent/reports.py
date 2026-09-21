"""Human-readable rendering of an analysis run.

A report's job is to make a proposal *refusable*. That means showing the risk
verdict before the trade details, naming every blocking rule rather than the
first, and explaining why each skipped symbol was skipped -- a report that only
lists tradable ideas quietly hides the fact that three of five symbols had no
usable history.
"""

from __future__ import annotations

from typing import Iterable, Sequence

from .agent import AnalysisResult, SymbolOutcome
from .models import Proposal, RiskDecision
from .numeric import format_decimal, round_money
from .risk import summarize
from .store import Coverage

RULE = "=" * 72
THIN = "-" * 72


def render_analysis(result: AnalysisResult, *, verbose: bool = False) -> str:
    """Render a full analysis run."""
    lines: list[str] = [RULE, "ANALYSIS", RULE, result.activity.describe(), ""]

    executable = result.executable
    blocked = result.blocked
    skipped = [o for o in result.outcomes if o.proposal is None]

    lines.append(
        f"{len(executable)} executable proposal(s), {len(blocked)} blocked by risk, "
        f"{len(skipped)} symbol(s) with no proposal"
    )
    lines.append("")

    for proposal in executable:
        lines.extend(render_proposal(proposal, verbose=verbose))
        lines.append("")

    if blocked:
        lines.append(THIN)
        lines.append("BLOCKED BY RISK (recorded, not executable)")
        lines.append(THIN)
        for proposal in blocked:
            lines.extend(render_proposal(proposal, verbose=verbose))
            lines.append("")

    if skipped:
        lines.append(THIN)
        lines.append("NO PROPOSAL")
        lines.append(THIN)
        for outcome in skipped:
            lines.append(render_skip(outcome))
        lines.append("")

    if executable:
        lines.append(THIN)
        lines.append(
            "To execute, approve a proposal BY ID. Claude re-checks the live price, "
            "previews the order, and only then places it."
        )
    return "\n".join(lines)


def render_skip(outcome: SymbolOutcome) -> str:
    reason = outcome.skipped_reason or "no reason recorded"
    return f"  {outcome.symbol:12} ({outcome.bars} bars): {reason}"


def render_proposal(proposal: Proposal, *, verbose: bool = False) -> list[str]:
    """Render one proposal, risk verdict first."""
    view = proposal.view
    lines = [
        THIN,
        f"[{proposal.proposal_id}] {proposal.side.value.upper()} "
        f"{format_decimal(proposal.quantity)} {proposal.symbol}",
        f"  risk          : {summarize(proposal.risk)}",
        f"  notional      : ${round_money(proposal.notional)} "
        f"@ reference {format_decimal(proposal.reference_price)}",
        f"  regime        : {view.regime.value}",
        f"  composite     : score {view.score:+.3f}, confidence {view.confidence:.3f}, "
        f"direction {view.direction.value}",
        f"  plan          : {proposal.plan.style} -- {proposal.plan.rationale}",
    ]

    for tranche in proposal.plan.tranches:
        lines.append(
            f"    tranche {tranche.index}: {format_decimal(tranche.quantity)} "
            f"@ {format_decimal(tranche.target_price)} ({tranche.order_type.value})"
        )

    lines.append("  signals       :")
    for signal in view.signals:
        weight = view.weights.get(signal.name, 0.0)
        lines.append(
            f"    {signal.name:15} score {signal.score:+.3f}  conf {signal.confidence:.2f}  "
            f"weight {weight:.2f}  | {signal.rationale}"
        )

    lines.extend(render_risk(proposal.risk, indent="  "))

    if verbose:
        lines.append("  sizing        :")
        for key, value in proposal.sizing_detail.items():
            lines.append(f"    {key:24} {value}")
        if view.notes:
            lines.append("  notes         :")
            for note in view.notes:
                lines.append(f"    - {note}")
    return lines


def render_risk(decision: RiskDecision, *, indent: str = "") -> list[str]:
    """Render every rule's verdict -- passes included."""
    lines = [f"{indent}risk checks   :"]
    for finding in decision.findings:
        if finding.passed:
            mark = "PASS"
        elif finding.blocking:
            mark = "FAIL"
        else:
            mark = "WARN"
        lines.append(f"{indent}  [{mark}] {finding.rule:22} {finding.message}")
    return lines


def render_coverage(coverages: Iterable[Coverage]) -> str:
    """Render price-history coverage for the watchlist."""
    lines = ["price history coverage:"]
    for coverage in coverages:
        lines.append(f"  {coverage.describe()}")
        age = coverage.age_seconds
        if age is not None:
            lines.append(f"    last observation {age / 60:.1f} minutes ago")
    return "\n".join(lines)


def render_requests(requests: Sequence[object], *, tool_label: str) -> str:
    """Render the MCP tool calls a plan implies, as a copyable checklist.

    The arguments are printed as JSON exactly as they must be passed, so the
    payload can be copied rather than retyped -- retyping an order payload is a
    chance to change a digit.
    """
    import json
    import textwrap

    lines = [f"{tool_label} -- call these tools in order:"]
    for index, request in enumerate(requests, start=1):
        arguments = getattr(request, "arguments", {})
        tool = getattr(request, "tool", "?")
        lines.append(f"  {index}. {tool}")
        lines.append(textwrap.indent(json.dumps(arguments, indent=2), "     "))
    return "\n".join(lines)
