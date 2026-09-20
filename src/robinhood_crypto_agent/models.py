from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import Enum

from pydantic import BaseModel, Field


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Regime(str, Enum):
    TRENDING = "trending"
    RANGING = "ranging"


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"


class OrderType(str, Enum):
    MARKET = "market"
    LIMIT = "limit"


class ProposalStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXECUTED = "executed"
    EXPIRED = "expired"


class ExecutionStyle(str, Enum):
    """How an approved proposal should be filled - informational only, never
    a substitute for the human-approval safety gate in execution/adapter.py.

    PROMPT: one order, near the reference price, submitted without delay -
    for time-sensitive (TRENDING) signals where waiting for a better entry
    risks missing the move entirely.

    STAGED: several resting limit orders spread across favorable price
    levels, filled gradually - for signals with more runway (RANGING) where
    there's time to let the market come to a better average price.
    """

    PROMPT = "prompt"
    STAGED = "staged"


class Candle(BaseModel):
    symbol: str
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


class SignalScore(BaseModel):
    """A single signal source's opinion on a symbol at a point in time."""

    source: str
    value: float = Field(ge=-1.0, le=1.0)
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str = ""


class Signal(BaseModel):
    """The composite strategy's output for one symbol."""

    symbol: str
    regime: Regime
    composite_value: float = Field(ge=-1.0, le=1.0)
    composite_confidence: float = Field(ge=0.0, le=1.0)
    side: Side
    component_scores: list[SignalScore] = Field(default_factory=list)
    generated_at: datetime = Field(default_factory=_utcnow)


class RiskCheckResult(BaseModel):
    passed: bool
    violations: list[str] = Field(default_factory=list)


class ExecutionTranche(BaseModel):
    """One resting order within a STAGED ExecutionPlan (or the single order
    of a PROMPT plan)."""

    sequence: int
    target_price: float
    quantity: float
    notional_usd: float


class ExecutionPlan(BaseModel):
    """How to fill an approved TradeProposal - built by
    execution/plan.py:build_execution_plan(), attached before logging. Purely
    informational: it guides how Claude places MCP orders for an
    already-approved proposal, it does not itself authorize anything."""

    style: ExecutionStyle
    tranches: list[ExecutionTranche]
    rationale: str


class TradeProposal(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    symbol: str
    side: Side
    quantity: float
    order_type: OrderType = OrderType.MARKET
    limit_price: float | None = None
    reference_price: float
    notional_usd: float
    rationale: str
    signal: Signal
    risk_check: RiskCheckResult
    execution_plan: ExecutionPlan | None = None
    status: ProposalStatus = ProposalStatus.PENDING
    created_at: datetime = Field(default_factory=_utcnow)


class ExecutionRecord(BaseModel):
    """What actually happened, written by `log-execution` from the real MCP
    response - never inferred or fabricated. `symbol`/`side` are duplicated
    from the proposal (rather than requiring a join) so the audit log's
    daily-activity/open-position accounting can be computed by scanning
    execution entries alone."""

    proposal_id: str
    symbol: str
    side: Side
    mcp_order_id: str | None = None
    status: str  # e.g. "filled", "rejected", "error" - from the MCP response
    filled_price: float | None = None
    filled_qty: float | None = None
    realized_pnl_usd: float | None = None
    override: bool = False
    timestamp: datetime = Field(default_factory=_utcnow)
    raw_mcp_response: dict = Field(default_factory=dict)
