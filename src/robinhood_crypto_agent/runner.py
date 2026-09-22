"""``rhca run``: the real-time loop, in shadow mode.

    Robinhood quotes --+
    RSS -> Jev --------+--> System 1: indicators + news signal + 16 risk rules
                       |          |
                       |   trigger: is confidence high?
                       |     no  -> logged as not_escalated
                       |     yes -> System 2: Claude Sonnet 5, propose or pass
                       |             (+ Crypto.com market data via MCP connector)
                       |          |
                       +--------> logged, scored, synced to the dashboard

"Shadow" means exactly one thing: there is no code path from here to an order.
The Robinhood client is read-only, System 2's tools are read-only, and a
"propose" is a row in the audit log that a human can take through the existing
approval gate -- or not.

Each task runs on its own cadence and fails on its own. A dead feed, a Jev
timeout or a Robinhood 5xx is logged and retried next time; it never stops the
loop. The heartbeat file records every cycle, so ``rhca status`` can tell a
quiet market from a dead process.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from . import dashboard as dashboard_mod
from . import news as news_mod
from .agent import Agent, MarketState
from .audit import KIND_PROPOSAL, AuditLog
from .config import AgentConfig
from .errors import AgentError
from .jev import JevClient
from .models import NewsItem, Proposal, ProposalStatus, Quote, parse_timestamp, utcnow
from .net import HttpError
from .numeric import ZERO, format_decimal
from .robinhood import RobinhoodClient
from .store import PriceStore, StateCache
from .store.prices import floor_to_interval
from .system2 import System2, not_configured
from .trigger import EscalationTrigger

log = logging.getLogger("rhca.run")

MODE_SHADOW = "shadow"


@dataclass
class Services:
    """The outside world, injected so a test can replace every piece of it."""

    robinhood: RobinhoodClient
    jev: JevClient | None = None
    system2: System2 | None = None
    dashboard: tuple[str, str] | None = None
    fetch_feed: Callable[[str], list[NewsItem]] = news_mod.fetch_feed


def system2_tools(
    robinhood: RobinhoodClient,
) -> tuple[Callable[[str], Quote | None], Callable[[], dict[str, Any]]]:
    """System 2's two read-only tools, bound to the Robinhood client."""

    def fetch_quote(symbol: str) -> Quote | None:
        quotes = robinhood.best_bid_ask([symbol])
        return quotes[0] if quotes else None

    def fetch_holdings() -> dict[str, Any]:
        account = robinhood.account()
        buying_power = account.buying_power if account else None
        return {
            "positions": [
                {"symbol": p.symbol, "quantity": format_decimal(p.quantity)}
                for p in robinhood.holdings()
            ],
            "buying_power": format_decimal(buying_power) if buying_power is not None else None,
        }

    return fetch_quote, fetch_holdings


class Runner:
    """Polls, evaluates, escalates and records, each on its own cadence."""

    def __init__(
        self,
        config: AgentConfig,
        services: Services,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.config = config
        self.services = services
        self.store = PriceStore(config.price_store_path)
        self.audit = AuditLog(config.audit_path)
        self.cache = StateCache(config.data_dir / "market_state.json")
        self.news = news_mod.NewsStore(config.news_path)
        self.agent = Agent(config, store=self.store, audit=self.audit)
        self.trigger = EscalationTrigger(config.pipeline)
        self._clock = clock
        self._sleep = sleep
        self._window = timedelta(minutes=config.strategy.news_window_minutes)

        pipeline = config.pipeline
        self._tasks: list[tuple[str, int, Callable[[], None]]] = [
            ("quotes", pipeline.quote_interval_seconds, self._poll_quotes),
            ("account", pipeline.account_interval_seconds, self._poll_account),
            ("news", pipeline.news_interval_seconds, self._poll_news),
            ("evaluate", pipeline.quote_interval_seconds, self._evaluate),
            ("sync", pipeline.sync_interval_seconds, self._sync),
        ]
        self._due = {name: 0.0 for name, _, _ in self._tasks}
        self._seen = self.news.seen_ids()
        self._started_at = utcnow()
        self._cycles = 0
        self._counts = dict.fromkeys(
            ("quotes", "news", "labeled", "candidates", "escalations", "proposed", "errors"), 0
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

    def _poll_account(self) -> None:
        robinhood = self.services.robinhood
        pairs = robinhood.trading_pairs(self.config.watchlist)
        for pair in pairs:
            if not pair.tradable or pair.halted:
                # Every proposal for it will fail pair_tradable; say why up front.
                log.warning("%s is reported untradable or halted by Robinhood", pair.symbol)
        self.cache.put_pairs(pairs)
        positions = robinhood.holdings()
        self.cache.put_positions(positions)
        account = robinhood.account()
        if account is None:
            return
        self.cache.put_account(account)
        if account.buying_power is None:
            return
        marks = {symbol: quote.mark for symbol, quote in self.cache.quotes().items()}
        # Holdings off the watchlist have no quote here and are left out. That
        # understates the portfolio, which makes the concentration limit
        # stricter rather than looser.
        held = sum((p.quantity * marks[p.symbol] for p in positions if p.symbol in marks), ZERO)
        self.cache.put_portfolio_value(account.buying_power + held)

    def _poll_news(self) -> None:
        cutoff = utcnow() - self._window
        fresh: list[NewsItem] = []
        for url in self.config.pipeline.rss_feeds:
            try:
                items = self.services.fetch_feed(url)
            except AgentError as exc:
                self._fail("news", exc)  # one dead feed must not hide the others
                continue
            fresh.extend(item for item in items if item.published_at >= cutoff)
        self._ingest(fresh)

    def _ingest(self, items: list[NewsItem]) -> list[NewsItem]:
        """Label new items with Jev and store them, oldest first."""
        jev = self.services.jev
        stored: list[NewsItem] = []
        for item in sorted(items, key=lambda i: i.published_at):
            if item.item_id in self._seen:
                continue
            labels = None
            if jev is not None:
                try:
                    labels = jev.label(item, self.config.watchlist)
                except HttpError as exc:
                    self._fail("jev", exc)
                    if exc.retryable:
                        break  # Jev is down or slow; the rest wait for the next poll
                    # A 4xx will not fix itself: keep the headline, unlabeled.
                except AgentError as exc:
                    self._fail("jev", exc)  # a malformed answer: unlabeled beats a guess
            labeled = news_mod.with_labels(item, labels)
            self.news.append([labeled])
            self._seen.add(item.item_id)
            stored.append(labeled)
            self._counts["news"] += 1
            if labels is not None:
                self._counts["labeled"] += 1
                if labels.asset != "OTHER" and labels.direction != "neutral":
                    log.info(
                        "news [%s %s %.2f, impact %.1f] %s",
                        labels.asset,
                        labels.direction,
                        labels.direction_confidence,
                        labels.impact,
                        item.title[:100],
                    )
        return stored

    def _evaluate(self) -> None:
        quotes = self.cache.quotes()
        if not quotes:
            return
        news = [i for i in self.news.recent(utcnow() - self._window) if i.labels is not None]
        market = MarketState(
            quotes=quotes,
            constraints=self.cache.pairs(),
            positions=self.cache.positions(),
            portfolio_value=self.cache.portfolio_value(),
            news=news,
        )
        result = self.agent.analyze(market, record=False)
        interval = self.config.strategy.bar_interval_minutes
        closed_bar = floor_to_interval(utcnow(), interval) - timedelta(minutes=interval)
        for outcome in result.outcomes:
            if outcome.proposal is not None:
                self._consider(outcome.proposal, closed_bar, market)

    def _consider(self, proposal: Proposal, closed_bar: datetime, market: MarketState) -> None:
        """Log a candidate once per idea, and escalate it if the trigger says so.

        An idea is (side, last closed bar, strongest headline): the view only
        changes when a bar closes or the news does, so re-evaluating every
        minute must not log the same candidate every minute.
        """
        symbol = proposal.symbol
        news_signal = next((s for s in proposal.view.signals if s.name == "news"), None)
        news_id = str(news_signal.detail.get("item_id", "")) if news_signal else ""
        key = f"{proposal.side.value}|{closed_bar.isoformat()}|{news_id}"
        if self._last_key.get(symbol) == key:
            return
        self._last_key[symbol] = key
        self._counts["candidates"] += 1

        now = utcnow()
        if now.date() != self._escalation_day:
            self._escalation_day, self._escalations_today = now.date(), 0
        verdict = self.trigger.evaluate(
            proposal,
            escalations_today=self._escalations_today,
            last_escalated_at=self._last_escalated.get(symbol),
            now=now,
        )
        system2 = self.services.system2
        extra: dict[str, Any] = {
            "source": "rhca run",
            "mode": MODE_SHADOW,
            "candidate_key": key,
            "trigger_reason": verdict.reason,
            # Only a real Sonnet call counts toward the cooldown and daily cap.
            "escalated": verdict.escalate and system2 is not None,
        }

        if not verdict.escalate:
            status = proposal.status if not proposal.risk.passed else ProposalStatus.NOT_ESCALATED
            self.audit.record_proposal(replace(proposal, status=status), extra)
            log.info("%s %s held back: %s", symbol, proposal.side.value, verdict.reason)
            return

        if system2 is None:
            decision = not_configured()
        else:
            self._escalations_today += 1
            self._last_escalated[symbol] = now
            self._counts["escalations"] += 1
            decision = system2.decide(proposal, market.news_for(symbol))

        status = ProposalStatus.PROPOSED if decision.approved else ProposalStatus.DECLINED_BY_SYSTEM2
        extra.update(
            system2_decision=decision.decision,
            system2_confidence=decision.confidence,
            system2=decision.to_dict(),
        )
        self.audit.record_proposal(replace(proposal, status=status), extra)
        if decision.approved:
            self._counts["proposed"] += 1
        log.info(
            "%s %s escalated -> System 2 %s%s: %s",
            symbol,
            proposal.side.value,
            decision.decision.upper(),
            f" ({decision.confidence:.2f})" if decision.confidence is not None else "",
            decision.rationale[:200],
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
        """Rebuild dedupe keys, cooldowns and today's count from the log.

        Durable state, like the risk caps: a restart must not re-log the
        current bar's candidates or hand out a fresh day's escalation budget.
        """
        self._last_key: dict[str, str] = {}
        self._last_escalated: dict[str, datetime] = {}
        self._escalation_day: date = utcnow().date()
        self._escalations_today = 0
        for record in self.audit.events(kind=KIND_PROPOSAL):
            symbol = str(record.get("symbol", ""))
            if record.get("candidate_key"):
                self._last_key[symbol] = str(record["candidate_key"])
            if not record.get("escalated"):
                continue
            try:
                at = parse_timestamp(str(record["recorded_at"]))
            except (KeyError, ValueError):
                continue
            self._last_escalated[symbol] = at
            if at.date() == self._escalation_day:
                self._escalations_today += 1

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
                "escalations_today": self._escalations_today,
                "last_error": self._last_error,
                "services": {
                    "robinhood": True,
                    "jev": services.jev is not None,
                    "system2": services.system2 is not None,
                    "market_data": bool(services.system2 and services.system2.market_data_url),
                    "dashboard": services.dashboard is not None,
                },
            },
        )


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    """Write-then-replace, so a reader never sees half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def read_json(path: Path) -> dict[str, Any] | None:
    """A JSON object from ``path``, or ``None`` when it is missing or unreadable."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


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
        f"  this run     : {counts.get('quotes', 0)} quotes, {counts.get('news', 0)} headlines "
        f"({counts.get('labeled', 0)} labeled), {counts.get('candidates', 0)} candidates, "
        f"{counts.get('escalations', 0)} escalations, {counts.get('proposed', 0)} proposed, "
        f"{counts.get('errors', 0)} errors",
        f"  today        : {heartbeat.get('escalations_today', 0)} escalation(s) to System 2",
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
