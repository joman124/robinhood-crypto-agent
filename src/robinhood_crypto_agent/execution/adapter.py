from __future__ import annotations

from typing import Protocol

from robinhood_crypto_agent.config import AppConfig, ExecutionMode
from robinhood_crypto_agent.models import ExecutionRecord, TradeProposal


class ExecutionNotAutomatedError(RuntimeError):
    """Raised whenever code tries to auto-submit while execution_mode is
    PROPOSE_ONLY. This is the actual safety boundary: in Phase 1, nothing in
    this codebase can place an order - only a human-approved, Claude-driven
    MCP tool call can (see CLAUDE.md and docs/architecture.md)."""


class ExecutionAdapter(Protocol):
    def submit_order(self, proposal: TradeProposal) -> ExecutionRecord: ...


class HumanApprovalRequiredAdapter:
    """The only adapter that exists in Phase 1. It cannot submit an order -
    it exists purely so every call site goes through one place and one
    exception type, and so graduating to automation later is a small,
    auditable change rather than a rewrite (see docs/architecture.md)."""

    def submit_order(self, proposal: TradeProposal) -> ExecutionRecord:
        raise ExecutionNotAutomatedError(
            "Automated execution is disabled (execution_mode=PROPOSE_ONLY). "
            "A human must approve this proposal by ID; Claude must then submit "
            "it via the MCP robinhood-trading order tool and record the result "
            "with `robinhood-crypto-agent log-execution`."
        )


def get_execution_adapter(config: AppConfig) -> ExecutionAdapter:
    if config.execution_mode is ExecutionMode.PROPOSE_ONLY:
        return HumanApprovalRequiredAdapter()
    raise NotImplementedError(
        "AUTO execution mode is not implemented in this repo yet. Graduating "
        "to it means implementing a new ExecutionAdapter (e.g. one backed by "
        "an MCP client invoked directly from Python) and wiring it in here - "
        "strategy, risk, and audit logging are unchanged. See "
        "docs/architecture.md."
    )
