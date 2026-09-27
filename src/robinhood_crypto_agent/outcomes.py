"""Scoring proposals against what the price actually did next.

This is the evidence layer for the retired System 1, whose proposals were
predictions over a fixed horizon: it answers *were they any good?*

The trend ladder's proposals are not scored here. A dip buy waits days or weeks
for its sell, so a six-hour hit rate would grade it on a question it never
asked. Its measure is P&L: ``rhca backtest`` before it trades, and the
ledger's realized P&L (``rhca status``) once it does.

What is measured, and what is not
---------------------------------
This scores the **signal**, not realized P&L. A proposal is resolved by looking
at where the price went over a fixed horizon after it was made, regardless of
whether it was accepted, filled, or skipped. That is deliberate:

* It scores every proposal, including declined ones — so the record is not
  biased toward the subset a human happened to like.
* It is unaffected by execution luck, so a good call that filled badly still
  reads as a good call.

Realized P&L lives in the audit log and is what the daily loss cap uses. The two
answer different questions and should not be conflated.

The round trip
--------------
A proposal is graded as the trade it would really have been: in at its
reference price (the ask for a buy, which already pays half the spread), and
out at the far side of the book when the horizon closes (the bid for a buy,
half a spread below the mark). Robinhood's market-maker crypto spreads measure
near 1.9%, so a buy has to lift the mark about 1.9% from where it started just
to break even. Grading against the mark alone, as this module used to, called
a trade that lost ~0.2% after costs a win.

So the verdict is on the move *after* the exit cost:

* **win** -- made more than the hurdle (``DEFAULT_HURDLE_PCT``) after the round trip;
* **loss** -- lost money after the round trip, by any amount;
* **flat** -- made money, but no more than the hurdle.

The exit cost is half the spread recorded with the proposal, or
``DEFAULT_EXIT_COST_PCT`` for a proposal logged before spreads were recorded.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from decimal import Decimal
from enum import Enum
from typing import Any, Iterable, Sequence

from .errors import AgentError
from .models import Candle, Side, parse_timestamp, utcnow
from .numeric import ZERO, format_decimal, round_money, to_decimal
from .symbols import canonical

#: How many bars after a proposal to measure. At the default 60-minute bar,
#: six bars is six hours -- long enough for a signal to express itself, short
#: enough that the result is still attributable to it.
DEFAULT_HORIZON_BARS = 6

#: Profit, in percent and *after* the round trip, a proposal must make to count
#: as a win. Anything that lost money after the round trip is a loss.
DEFAULT_HURDLE_PCT = Decimal("0.25")

#: The exit leg's cost, in percent of the mark, when a proposal did not record
#: its spread: half of the ~1.9% measured on Robinhood's market-maker quotes.
DEFAULT_EXIT_COST_PCT = Decimal("0.95")


class Verdict(str, Enum):
    """How a proposal turned out."""

    WIN = "win"
    LOSS = "loss"
    FLAT = "flat"
    PENDING = "pending"
    UNSCORABLE = "unscorable"


@dataclass(frozen=True)
class Outcome:
    """The scored result of one proposal."""

    proposal_id: str
    symbol: str
    side: Side
    verdict: Verdict
    reference_price: Decimal
    resolved_price: Decimal | None
    signed_move_pct: Decimal | None
    horizon_bars: int
    hurdle_pct: Decimal
    proposed_at: datetime
    resolved_at: datetime | None
    regime: str
    score: float
    confidence: float
    reason: str = ""
    #: The proposal's status when logged -- proposed, not_escalated,
    #: declined_by_system2, rejected_by_risk -- so hit rates can be compared
    #: across what each stage let through and what it held back.
    status: str = "proposed"
    #: What the exit leg cost, in percent of the mark: half the spread.
    exit_cost_pct: Decimal = DEFAULT_EXIT_COST_PCT

    def to_dict(self) -> dict[str, Any]:
        return {
            "proposal_id": self.proposal_id,
            "symbol": self.symbol,
            "side": self.side.value,
            "verdict": self.verdict.value,
            "reference_price": format_decimal(self.reference_price),
            "resolved_price": (
                format_decimal(self.resolved_price) if self.resolved_price is not None else None
            ),
            "signed_move_pct": (
                str(round_money(self.signed_move_pct, 3))
                if self.signed_move_pct is not None
                else None
            ),
            "horizon_bars": self.horizon_bars,
            "hurdle_pct": format_decimal(self.hurdle_pct),
            "exit_cost_pct": format_decimal(self.exit_cost_pct),
            "proposed_at": self.proposed_at.isoformat(),
            "resolved_at": self.resolved_at.isoformat() if self.resolved_at else None,
            "regime": self.regime,
            "score": self.score,
            "confidence": self.confidence,
            "reason": self.reason,
        }


def score_proposal(
    *,
    proposal_id: str,
    symbol: str,
    side: Side,
    reference_price: Decimal,
    proposed_at: datetime,
    candles: Sequence[Candle],
    regime: str = "unknown",
    score: float = 0.0,
    confidence: float = 0.0,
    horizon_bars: int = DEFAULT_HORIZON_BARS,
    hurdle_pct: Decimal = DEFAULT_HURDLE_PCT,
    exit_cost_pct: Decimal = DEFAULT_EXIT_COST_PCT,
) -> Outcome:
    """Resolve one proposal against the bars that followed it, after the round trip.

    Returns a ``PENDING`` outcome when the horizon has not elapsed yet, and
    ``UNSCORABLE`` when history is missing over the window -- neither is
    silently treated as a loss, which would make an agent that simply stopped
    trading look like a bad one.
    """
    symbol = canonical(symbol)

    def build(verdict: Verdict, *, resolved=None, move=None, at=None, reason="") -> Outcome:
        return Outcome(
            proposal_id=proposal_id,
            symbol=symbol,
            side=side,
            verdict=verdict,
            reference_price=reference_price,
            resolved_price=resolved,
            signed_move_pct=move,
            horizon_bars=horizon_bars,
            hurdle_pct=hurdle_pct,
            proposed_at=proposed_at,
            resolved_at=at,
            regime=regime,
            score=score,
            confidence=confidence,
            reason=reason,
            exit_cost_pct=exit_cost_pct,
        )

    if reference_price <= ZERO:
        return build(Verdict.UNSCORABLE, reason="reference price is not positive")

    after = [c for c in candles if canonical(c.symbol) == symbol and c.start >= proposed_at]
    if not after:
        return build(
            Verdict.PENDING,
            reason=f"no bars recorded for {symbol} after the proposal yet",
        )

    if len(after) < horizon_bars:
        return build(
            Verdict.PENDING,
            reason=f"{len(after)} of {horizon_bars} bars elapsed since the proposal",
        )

    target = after[horizon_bars - 1]
    resolved_price = target.close
    if resolved_price <= ZERO:
        return build(Verdict.UNSCORABLE, reason="resolved price is not positive")

    # Out at the far side of the book: a buy sells at the bid, half a spread
    # below the mark; a sell buys back at the ask, half a spread above it.
    # The reference price already paid the entry half.
    cost = exit_cost_pct / Decimal(100)
    if side is Side.BUY:
        exit_price = resolved_price * (Decimal(1) - cost)
        signed = (exit_price - reference_price) / reference_price * Decimal(100)
    else:
        exit_price = resolved_price * (Decimal(1) + cost)
        signed = (reference_price - exit_price) / reference_price * Decimal(100)

    if signed > hurdle_pct:
        verdict = Verdict.WIN
    elif signed < ZERO:
        verdict = Verdict.LOSS
    else:
        verdict = Verdict.FLAT

    return build(
        verdict,
        resolved=resolved_price,
        move=signed,
        at=target.end,
        reason=(
            f"{format_decimal(round_money(signed, 3))}% after the round trip over "
            f"{horizon_bars} bars (exit at the {'bid' if side is Side.BUY else 'ask'}, "
            f"{exit_cost_pct}% from the mark); a win needs more than {hurdle_pct}%"
        ),
    )


@dataclass
class AccuracyStats:
    """Aggregate accuracy over a set of outcomes."""

    total: int = 0
    wins: int = 0
    losses: int = 0
    flat: int = 0
    pending: int = 0
    unscorable: int = 0
    moves: list[Decimal] = field(default_factory=list)

    @property
    def resolved(self) -> int:
        """Proposals with a real verdict. Pending ones are not evidence yet."""
        return self.wins + self.losses + self.flat

    @property
    def decided(self) -> int:
        """Wins plus losses. Flat outcomes did not go either way."""
        return self.wins + self.losses

    @property
    def win_rate(self) -> float | None:
        """Wins as a share of decided outcomes, or ``None`` with no evidence.

        ``None`` rather than 0.0: an agent with no resolved proposals has an
        *unknown* hit rate, and showing 0% would read as "it is wrong every
        time".
        """
        if self.decided == 0:
            return None
        return self.wins / self.decided

    @property
    def average_move_pct(self) -> Decimal | None:
        if not self.moves:
            return None
        return sum(self.moves, ZERO) / Decimal(len(self.moves))

    @property
    def best_move_pct(self) -> Decimal | None:
        return max(self.moves) if self.moves else None

    @property
    def worst_move_pct(self) -> Decimal | None:
        return min(self.moves) if self.moves else None

    def add(self, outcome: Outcome) -> None:
        self.total += 1
        if outcome.verdict is Verdict.WIN:
            self.wins += 1
        elif outcome.verdict is Verdict.LOSS:
            self.losses += 1
        elif outcome.verdict is Verdict.FLAT:
            self.flat += 1
        elif outcome.verdict is Verdict.PENDING:
            self.pending += 1
        else:
            self.unscorable += 1
        if outcome.signed_move_pct is not None:
            self.moves.append(outcome.signed_move_pct)

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "wins": self.wins,
            "losses": self.losses,
            "flat": self.flat,
            "pending": self.pending,
            "unscorable": self.unscorable,
            "resolved": self.resolved,
            "decided": self.decided,
            "win_rate": self.win_rate,
            "average_move_pct": (
                str(round_money(self.average_move_pct, 3))
                if self.average_move_pct is not None
                else None
            ),
            "best_move_pct": (
                str(round_money(self.best_move_pct, 3)) if self.best_move_pct is not None else None
            ),
            "worst_move_pct": (
                str(round_money(self.worst_move_pct, 3))
                if self.worst_move_pct is not None
                else None
            ),
        }


def aggregate(outcomes: Iterable[Outcome]) -> AccuracyStats:
    stats = AccuracyStats()
    for outcome in outcomes:
        stats.add(outcome)
    return stats


def group_by(outcomes: Iterable[Outcome], key: str) -> dict[str, AccuracyStats]:
    """Accuracy broken down by ``symbol``, ``regime``, ``side`` or ``status``."""
    if key not in {"symbol", "regime", "side", "status"}:
        raise ValueError(f"cannot group outcomes by {key!r}")
    grouped: dict[str, AccuracyStats] = {}
    for outcome in outcomes:
        value = getattr(outcome, key)
        label = value.value if isinstance(value, Enum) else str(value)
        grouped.setdefault(label, AccuracyStats()).add(outcome)
    return grouped


def outcome_from_proposal_record(
    record: dict[str, Any],
    candles: Sequence[Candle],
    *,
    horizon_bars: int = DEFAULT_HORIZON_BARS,
    hurdle_pct: Decimal = DEFAULT_HURDLE_PCT,
) -> Outcome | None:
    """Score a proposal straight from its audit-log record.

    ``None`` for a record that cannot be scored; for a time exit, which closed
    a position rather than predicting; and for the ladder's proposals, whose
    measure is P&L rather than a hit rate.
    """
    if record.get("exit") or record.get("strategy") == "ladder":
        return None
    try:
        proposal_id = str(record["proposal_id"])
        symbol = canonical(str(record["symbol"]))
        side = Side(str(record["side"]))
        reference_price = to_decimal(record["reference_price"], field="reference_price")
        proposed_at = parse_timestamp(str(record["recorded_at"]))
    except (KeyError, ValueError, TypeError):
        return None

    outcome = score_proposal(
        proposal_id=proposal_id,
        symbol=symbol,
        side=side,
        reference_price=reference_price,
        proposed_at=proposed_at,
        candles=candles,
        regime=str(record.get("regime", "unknown")),
        score=float(record.get("score", 0.0) or 0.0),
        confidence=float(record.get("confidence", 0.0) or 0.0),
        horizon_bars=horizon_bars,
        hurdle_pct=hurdle_pct,
        exit_cost_pct=_exit_cost(record),
    )
    return replace(outcome, status=str(record.get("status") or "proposed"))


def _exit_cost(record: dict[str, Any]) -> Decimal:
    """Half the spread the proposal recorded, or the default when it has none."""
    raw = record.get("spread_pct")
    if raw is None:
        return DEFAULT_EXIT_COST_PCT
    try:
        spread = to_decimal(raw, field="spread_pct")
    except (AgentError, ValueError, ArithmeticError):
        return DEFAULT_EXIT_COST_PCT
    return spread / Decimal(2) if spread > ZERO else DEFAULT_EXIT_COST_PCT


def horizon_elapsed_at(
    proposed_at: datetime, *, horizon_bars: int, bar_interval_minutes: int
) -> datetime:
    """When a proposal's horizon closes, for "resolves in ~2h" in a UI."""
    return proposed_at + timedelta(minutes=horizon_bars * bar_interval_minutes)


def is_resolvable_now(
    proposed_at: datetime, *, horizon_bars: int, bar_interval_minutes: int
) -> bool:
    return utcnow() >= horizon_elapsed_at(
        proposed_at, horizon_bars=horizon_bars, bar_interval_minutes=bar_interval_minutes
    )
