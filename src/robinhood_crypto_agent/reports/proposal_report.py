from __future__ import annotations

from robinhood_crypto_agent.models import TradeProposal


def format_proposal_report(proposals: list[TradeProposal]) -> str:
    if not proposals:
        return (
            "No proposals generated: no signal cleared the composite "
            "threshold, or all candidates were risk-rejected."
        )

    lines = ["Trade proposal report", "=" * 60]
    for p in proposals:
        lines.append(f"\n[{p.status.value.upper()}] {p.id}")
        lines.append(
            f"  {p.side.value.upper()} {p.quantity:.6f} {p.symbol} "
            f"(~${p.notional_usd:.2f} @ ${p.reference_price:.2f})"
        )
        lines.append(
            f"  Regime: {p.signal.regime.value}  "
            f"Composite: {p.signal.composite_value:+.2f} "
            f"(confidence {p.signal.composite_confidence:.2f})"
        )
        for score in p.signal.component_scores:
            lines.append(
                f"    - {score.source}: {score.value:+.2f} "
                f"(conf {score.confidence:.2f}) - {score.rationale}"
            )
        lines.append(f"  Rationale: {p.rationale}")
        if p.execution_plan is not None:
            plan = p.execution_plan
            lines.append(f"  Execution plan: {plan.style.value.upper()} - {plan.rationale}")
            for tranche in plan.tranches:
                lines.append(
                    f"    - tranche {tranche.sequence}: {tranche.quantity:.6f} "
                    f"@ ${tranche.target_price:.2f} (~${tranche.notional_usd:.2f})"
                )
        if p.risk_check.passed:
            lines.append("  Risk check: PASSED")
        else:
            lines.append("  Risk check: FAILED")
            for violation in p.risk_check.violations:
                lines.append(f"    - {violation}")
    return "\n".join(lines)
