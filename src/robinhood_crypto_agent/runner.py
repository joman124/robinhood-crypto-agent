"""``rhca run``: the real-time loop, in shadow mode.

    Coinbase daily closes + Robinhood quotes -> the split + 14 risk rules -> proposal
                                                                               |
                                            logged, and synced to the dashboard

    Coinbase daily closes -> the split account, on paper -> one shadow_day record
                             (the forward test, config/shadow.yaml; ``shadow.py``)

Each minute the loop re-runs the split on the last closed UTC day. What it
wants -- a breakout entry or stop, a long-term tranche -- is logged as a
proposal once an hour, priced off the quote then, for as long as the rule
still wants it: a fresh one each hour keeps its price inside the approval
gate's drift tolerance.

"Shadow" means exactly one thing: there is no code path from here to an order.
The Robinhood client is read-only, and a proposal is a row in the audit log
that a human can take through the approval gate -- or not.

The forward test is paper all the way down: it writes ``shadow_day`` records,
never proposals, so nothing it does can reach the approval gate.

Each task runs on its own cadence and fails on its own. A Robinhood 5xx is
logged and retried next time; it never stops the loop. The heartbeat file
records every cycle, so ``rhca status`` can tell a quiet market from a dead
process.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

from . import dashboard as dashboard_mod
from . import shadow as shadow_mod
from .agent import Agent, MarketState, split_annotations
from .audit import KIND_PROPOSAL, AuditLog
from .config import AgentConfig
from .daily import DailyBars
from .errors import AgentError
from .files import write_json_atomic
from .ledger import STRATEGY_SPLIT
from .models import Proposal, parse_timestamp, utcnow
from .numeric import format_decimal, round_money
from .robinhood import RobinhoodClient
from .store import PriceStore, StateCache
from .store.prices import floor_to_interval
from .store.state import SECTION_POSITIONS

log = logging.getLogger("rhca.run")

MODE_SHADOW = "shadow"


@dataclass
class Services:
    """The outside world, injected so a test can replace every piece of it."""

    robinhood: RobinhoodClient
    dashboard: tuple[str, str] | None = None
    #: Daily bars for the split and the forward test; ``None`` reads
    #: Coinbase's public candles.
    daily_history: shadow_mod.Fetch | None = None


class Runner:
    """Polls, evaluates and records, each on its own cadence."""

    def __init__(
        self,
        config: AgentConfig,
        services: Services,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        robinhood_account: str | None = None,
    ) -> None:
        self.config = config
        self.services = services
        #: The crypto account the API key reads, already masked to its last 4.
        self.robinhood_account = robinhood_account
        self.store = PriceStore(config.price_store_path)
        self.audit = AuditLog(config.audit_path)
        self.cache = StateCache(config.data_dir / "market_state.json")
        self.agent = Agent(
            config,
            store=self.store,
            audit=self.audit,
            daily=DailyBars(config.data_dir / "daily", fetch=services.daily_history),
        )
        self._clock = clock
        self._sleep = sleep

        pipeline = config.pipeline
        self._tasks: list[tuple[str, int, Callable[[], None]]] = [
            ("quotes", pipeline.quote_interval_seconds, self._poll_quotes),
            ("pairs", pipeline.account_interval_seconds, self._poll_pairs),
            ("evaluate", pipeline.quote_interval_seconds, self._evaluate),
            ("sync", pipeline.sync_interval_seconds, self._sync),
        ]
        self.shadow = (
            shadow_mod.ShadowTracker(
                config, self.audit, services.robinhood, fetch=services.daily_history
            )
            if config.shadow is not None
            else None
        )
        if self.shadow is not None:
            self._tasks.append(
                ("forward test", shadow_mod.CHECK_INTERVAL_SECONDS, self._forward_test)
            )
        self._due = {name: 0.0 for name, _, _ in self._tasks}
        self._started_at = utcnow()
        self._cycles = 0
        self._counts = dict.fromkeys(
            ("quotes", "candidates", "proposed", "forward_test_days", "errors"), 0
        )
        self._last_error: dict[str, str] | None = None
        self._resume_from_audit()

    # -- the loop ------------------------------------------------------------

    def run(self, *, once: bool = False, max_seconds: float | None = None) -> None:
        started = self._clock()
        while True:
            self.cycle(force=once)
            if once:
                return
            if max_seconds is not None and self._clock() - started >= max_seconds:
                return
            wait = min(self._due.values()) - self._clock()
            self._sleep(max(1.0, min(wait, 30.0)))

    def cycle(self, *, force: bool = False) -> None:
        """Run every task that is due (all of them when ``force``)."""
        for name, interval, task in self._tasks:
            now = self._clock()
            if force or now >= self._due[name]:
                self._attempt(name, task)
                self._due[name] = now + interval
        self._cycles += 1
        self._write_heartbeat()

    def _attempt(self, name: str, task: Callable[[], None]) -> None:
        try:
            task()
        except AgentError as exc:
            self._fail(name, exc)
        except Exception as exc:  # a bug in one task must not stop the others
            log.exception("%s failed unexpectedly", name)
            self._fail(name, exc, logged=True)

    def _fail(self, name: str, exc: Exception, *, logged: bool = False) -> None:
        self._counts["errors"] += 1
        self._last_error = {"task": name, "message": str(exc)[:500], "at": utcnow().isoformat()}
        if not logged:
            log.warning("%s: %s", name, exc)

    # -- tasks ---------------------------------------------------------------

    def _poll_quotes(self) -> None:
        quotes = self.services.robinhood.best_bid_ask(self.config.watchlist)
        if not quotes:
            raise AgentError("Robinhood returned no usable quotes")
        self.store.record_quotes(quotes)
        self.cache.put_quotes(quotes)
        self._counts["quotes"] += len(quotes)

    def _poll_pairs(self) -> None:
        # Only market data comes from the API key. Its account is the owner's
        # main crypto account, not the Agentic one orders go to, so its
        # holdings and buying power stay out of the cache: `rhca ingest
        # positions` and `rhca ingest portfolio` supply the Agentic account's,
        # and a cycle here must not overwrite them.
        pairs = self.services.robinhood.trading_pairs(self.config.watchlist)
        for pair in pairs:
            if not pair.tradable or pair.halted:
                # Every proposal for it will fail pair_tradable; say why up front.
                log.warning("%s is reported untradable or halted by Robinhood", pair.symbol)
        self.cache.put_pairs(pairs)

    def _evaluate(self) -> None:
        quotes = self.cache.quotes()
        if not quotes:
            return
        market = MarketState(
            quotes=quotes,
            constraints=self.cache.pairs(),
            positions=self.cache.positions(),
            portfolio_value=self.cache.portfolio_value(),
            positions_as_of=positions_as_of(self.cache),
        )
        result = self.agent.analyze(market, record=False)
        hour = floor_to_interval(utcnow(), 60)
        for proposal in result.proposals:
            self._consider(proposal, hour)

    def _consider(self, proposal: Proposal, hour: datetime) -> None:
        """Log what the split wants once an hour, until it is acted on.

        Re-evaluating every minute must not log the same order every minute.
        A new hour logs it afresh, priced off the quote then.
        """
        annotations = split_annotations(proposal)
        slot = proposal_slot(proposal.symbol, annotations)
        key = f"{slot}|{hour.isoformat()}"
        if self._last_key.get(slot) == key:
            return
        self._last_key[slot] = key
        self._counts["candidates"] += 1
        self.audit.record_proposal(
            proposal,
            {"source": "rhca run", "mode": MODE_SHADOW, "candidate_key": key, **annotations},
        )
        if proposal.risk.passed:
            self._counts["proposed"] += 1
        log.info(
            "%s %s %s proposed (%s) -- %s: %s",
            proposal.symbol,
            proposal.side.value,
            format_decimal(proposal.quantity),
            proposal.proposal_id,
            "ready to approve" if proposal.risk.passed else "blocked by risk",
            proposal.reason,
        )

    def _forward_test(self) -> None:
        """Record the latest UTC daily close of the paper split account, once."""
        result = self.shadow.update() if self.shadow is not None else None
        if result is None:
            return
        self._counts["forward_test_days"] += 1
        write_json_atomic(self.config.shadow_path, shadow_mod.snapshot(result, self.config))
        day = f"{result.through:%Y-%m-%d}"
        fills = result.fills_on(result.through)
        for fill in fills:
            log.info(
                "forward test (paper, not a proposal) %s close: %s %s %s $%s at %s",
                day, fill.sleeve, fill.side, fill.symbol,
                round_money(fill.dollars), format_decimal(fill.close),
            )
        log.info(
            "forward test (paper) %s close: %s; split account $%s after %s close(s)",
            day,
            f"{len(fills)} paper fill(s)" if fills else "no paper fills",
            round_money(result.split.combined.final_equity),
            result.closes,
        )

    def _sync(self) -> None:
        if self.services.dashboard is None:
            return
        base_url, token = self.services.dashboard
        result = dashboard_mod.sync(
            self.config, audit=self.audit, store=self.store, base_url=base_url, token=token
        )
        for decision in result.new_decisions:
            log.info("dashboard decision: %s %s", decision.kind.value, decision.proposal_id)

    # -- state ---------------------------------------------------------------

    def _resume_from_audit(self) -> None:
        """Rebuild the once-an-hour keys from the log, so a restart does not
        re-log this hour's proposals."""
        self._last_key: dict[str, str] = {}
        for record in self.audit.events(kind=KIND_PROPOSAL):
            key = record.get("candidate_key")
            if key and record.get("strategy") == STRATEGY_SPLIT:
                self._last_key[str(key).rpartition("|")[0]] = str(key)

    def _write_heartbeat(self) -> None:
        services = self.services
        write_json_atomic(
            self.config.heartbeat_path,
            {
                "pid": os.getpid(),
                "mode": MODE_SHADOW,
                "started_at": self._started_at.isoformat(),
                "last_cycle_at": utcnow().isoformat(),
                "cycles": self._cycles,
                "counts": dict(self._counts),
                "last_error": self._last_error,
                "robinhood_account": self.robinhood_account,
                "services": {
                    "robinhood": True,
                    "dashboard": services.dashboard is not None,
                    "forward_test": self.shadow is not None,
                },
            },
        )


def proposal_slot(symbol: str, annotations: dict[str, Any]) -> str:
    """What dedupes per hour: one coin, sleeve and rule."""
    return f"{symbol}|{annotations.get('sleeve')}|{annotations.get('rule')}"


def positions_as_of(cache: StateCache) -> datetime | None:
    """When the holdings snapshot was last ingested."""
    return next((a.updated_at for a in cache.ages() if a.name == SECTION_POSITIONS), None)


def stale_after_seconds(config: AgentConfig) -> int:
    """How old a heartbeat may be before the loop reads as not running."""
    return max(180, 3 * config.pipeline.quote_interval_seconds)


def describe_heartbeat(heartbeat: dict[str, Any] | None, *, stale_after_seconds: float) -> list[str]:
    """``rhca status`` lines for the pipeline: alive or not, and what it did."""
    if heartbeat is None:
        return ["pipeline       : never run (start it with `rhca run`)"]
    try:
        last = parse_timestamp(str(heartbeat["last_cycle_at"]))
    except (KeyError, ValueError):
        return ["pipeline       : heartbeat file is unreadable"]
    age = (utcnow() - last).total_seconds()
    state = "RUNNING" if age <= stale_after_seconds else "NOT RUNNING"
    counts = heartbeat.get("counts") or {}
    services = heartbeat.get("services") or {}
    enabled = ", ".join(name for name, on in services.items() if on) or "none"
    lines = [
        f"pipeline       : {state} ({heartbeat.get('mode', '?')} mode), last cycle "
        f"{_ago(age)}, {heartbeat.get('cycles', 0)} cycles since {heartbeat.get('started_at')}",
        f"  services     : {enabled}",
        f"  this run     : {counts.get('quotes', 0)} quotes, "
        f"{counts.get('candidates', 0)} proposals logged "
        f"({counts.get('proposed', 0)} passing risk), "
        + (
            f"{counts.get('forward_test_days', 0)} forward-test close(s) recorded, "
            if services.get("forward_test")
            else ""
        )
        + f"{counts.get('errors', 0)} errors",
    ]
    error = heartbeat.get("last_error")
    if isinstance(error, dict):
        lines.append(f"  last error   : [{error.get('task')}] {error.get('message')}")
    return lines


def _ago(seconds: float) -> str:
    if seconds < 90:
        return f"{seconds:.0f}s ago"
    if seconds < 5400:
        return f"{seconds / 60:.0f}m ago"
    return f"{seconds / 3600:.1f}h ago"
