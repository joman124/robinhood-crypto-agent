from __future__ import annotations

from robinhood_crypto_agent.audit.log import DailyActivity
from robinhood_crypto_agent.models import RiskCheckResult, Side, TradeProposal
from robinhood_crypto_agent.risk.limits import RiskLimits


def evaluate(
    proposal: TradeProposal, risk_limits: RiskLimits, activity: DailyActivity
) -> RiskCheckResult:
    """Pure function, no I/O: every applicable violation is collected (not
    just the first) so a rejected proposal's logged reason is fully
    informative."""
    violations: list[str] = []

    if proposal.symbol not in risk_limits.allowed_symbols:
        violations.append(
            f"{proposal.symbol} is not in the watchlist allowlist "
            f"{risk_limits.allowed_symbols}"
        )

    if proposal.notional_usd > risk_limits.max_position_usd:
        violations.append(
            f"proposal notional ${proposal.notional_usd:.2f} exceeds "
            f"max_position_usd ${risk_limits.max_position_usd:.2f}"
        )

    projected_exposure = activity.exposure_usd + proposal.notional_usd
    if projected_exposure > risk_limits.max_daily_exposure_usd:
        violations.append(
            f"today's exposure ${activity.exposure_usd:.2f} + this proposal "
            f"${proposal.notional_usd:.2f} = ${projected_exposure:.2f} would exceed "
            f"max_daily_exposure_usd ${risk_limits.max_daily_exposure_usd:.2f}"
        )

    if activity.realized_loss_usd >= risk_limits.max_daily_loss_usd:
        violations.append(
            f"today's realized loss ${activity.realized_loss_usd:.2f} has already "
            f"reached max_daily_loss_usd ${risk_limits.max_daily_loss_usd:.2f}"
        )

    # Opening a new position (a BUY into a symbol with no existing net long)
    # counts against max_open_positions; a SELL that reduces/closes a
    # position never does.
    if proposal.side is Side.BUY and proposal.symbol not in activity.open_position_symbols:
        projected_open = len(activity.open_position_symbols) + 1
        if projected_open > risk_limits.max_open_positions:
            violations.append(
                f"opening {proposal.symbol} would bring open positions to "
                f"{projected_open}, exceeding max_open_positions "
                f"{risk_limits.max_open_positions}"
            )

    if activity.kill_switch_engaged:
        violations.append("kill switch is engaged - no executions permitted")

    return RiskCheckResult(passed=len(violations) == 0, violations=violations)


class RiskEngine:
    def __init__(self, risk_limits: RiskLimits):
        self.risk_limits = risk_limits

    def evaluate(self, proposal: TradeProposal, activity: DailyActivity) -> RiskCheckResult:
        return evaluate(proposal, self.risk_limits, activity)
