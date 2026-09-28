"""The command-line interface.

There are two ways to drive the agent, and one way to place an order.

**Claude Code, by hand.** Claude holds the MCP connection and is the only thing
that can place a Robinhood order; this CLI holds the decision logic, the risk
limits and the audit trail:

    Claude calls get_crypto_quotes  ->  rhca ingest quotes   (JSON in)
    Claude calls get_currency_pairs ->  rhca ingest pairs
    ...                                 rhca analyze          (proposals out)
    human approves a proposal id   ->  rhca approve          (payload out)
    Claude calls place_crypto_order ->  rhca record-execution (JSON in)

Every command that accepts MCP output takes it as JSON on a path or on stdin,
so nothing has to be retyped or paraphrased -- paraphrasing a tool response is
how a fabricated fill ends up in an audit log.

**``rhca run``, in shadow mode.** The real-time loop reads quotes and trading
pairs from Robinhood's Crypto API with a read-only client and runs the split
on each closed UTC day (see ``runner``). Balance and holdings are the
Agentic account's, as ``rhca ingest`` last cached them. It proposes; it cannot
order. Its proposals go through the same ``rhca approve`` gate. It also keeps
the forward test's paper account (``shadow``), which ``rhca shadow`` reports.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Sequence

from . import backtest as backtest_mod
from . import dashboard as dashboard_mod
from . import files as files_mod
from . import portfolio_backtest as portfolio_mod
from . import reports
from . import runner as runner_mod
from . import shadow as shadow_mod
from .agent import Agent, MarketState, split_rules
from .audit import AuditLog, day_from
from .bootstrap import fetch_coinbase_history
from .config import AgentConfig, load_config
from .daily import latest_close
from .errors import AgentError
from .execution.gate import ApprovalGate
from .execution.kill_switch import REASON_DAILY_LOSS, REASON_MANUAL, KillSwitch
from .execution.orders import build_plan_requests
from .ledger import Holding, split_book
from .mcp.contract import CRYPTO_TOOLS, TOOL_CONTRACTS, validate_crypto_order_args
from .mcp.parse import (
    parse_accounts,
    parse_currency_pairs,
    parse_order_response,
    parse_positions,
    parse_quotes,
    unwrap_results,
)
from .models import Candle, PairConstraints, parse_timestamp, utcnow
from .numeric import format_decimal, round_money, to_decimal
from .outcomes import DEFAULT_HORIZON_BARS, DEFAULT_HURDLE_PCT
from .robinhood import API_KEY_ENV as ROBINHOOD_KEY_ENV
from .robinhood import PRIVATE_KEY_ENV as ROBINHOOD_PRIVATE_KEY_ENV
from .robinhood import RobinhoodClient, generate_key_pair
from .serde import proposal_from_dict
from .store import PriceStore, StateCache
from .store.state import SECTION_CRYPTO_BUYING_POWER
from .strategy.breakout import Breakout
from .strategy.hodl import MODES as LONG_MODES
from .strategy.hodl import Accumulate
from .symbols import canonical

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_BLOCKED = 2

#: The crypto account the Robinhood API key reads -- the full number or its
#: last 4 digits. A Crypto API key only ever reads the main crypto account of
#: the login it was made under (the key page has no account picker), so this
#: pins the main account, and a key from another login is refused at startup.
#: The Agentic account is reachable only through the MCP server.
CRYPTO_ACCOUNT_ENV = "RHCA_CRYPTO_ACCOUNT"


def mask(number: str | None) -> str:
    """An account number as its last 4 digits, the way Robinhood shows it."""
    return f"****{number[-4:]}" if number else "unknown"


def check_account(account: Any, expected: str) -> None:
    """Refuse to run against any crypto account but the pinned one."""
    if account is None:
        raise AgentError("Robinhood returned no account for this API key")
    if not expected:
        return
    if len(expected) < 4 or not account.account_number.endswith(expected):
        raise AgentError(
            f"this Robinhood API key reads crypto account {mask(account.account_number)}, "
            f"but {CRYPTO_ACCOUNT_ENV} pins {mask(expected)}. A Crypto API key only reads "
            f"your main crypto account, so pin that one; the Agentic account is reachable "
            f"only through the MCP server. If {mask(account.account_number)} is not your "
            f"main account, the key belongs to another Robinhood login "
            f"(docs/runbook.md, 'Which account')."
        )


#: Every credential ``rhca run`` reads. Only names are ever printed.
CREDENTIAL_ENVS = (
    ROBINHOOD_KEY_ENV,
    ROBINHOOD_PRIVATE_KEY_ENV,
    "RHCA_DASHBOARD_URL",
    "RHCA_DASHBOARD_TOKEN",
)


def _dotenv_key(line: str) -> str:
    """The key a ``KEY=VALUE`` line sets, or "" for a comment or blank line."""
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        return ""
    return line.partition("=")[0].strip().removeprefix("export ").strip()


def read_dotenv(path: Path) -> dict[str, str]:
    """The non-empty ``KEY=VALUE`` pairs in a .env file. An empty value is unset."""
    if not path.is_file():
        return {}
    values = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        key = _dotenv_key(raw)
        if not key:
            continue
        value = raw.partition("=")[2].strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if value:
            values[key] = value
    return values


def load_dotenv(path: Path) -> list[str]:
    """Load a .env file into the environment, never overriding what is set.

    Returns the names it loaded. Values are not echoed anywhere.
    """
    loaded = []
    for key, value in read_dotenv(path).items():
        if key not in os.environ:
            os.environ[key] = value
            loaded.append(key)
    return loaded


def set_dotenv_value(path: Path, key: str, value: str) -> None:
    """Set ``key`` in a .env file, replacing its line or appending one.

    A missing .env starts as a copy of the ``.env.example`` beside it, so the
    other keys keep their comments.
    """
    if path.is_file():
        lines = path.read_text(encoding="utf-8").splitlines()
    else:
        example = path.with_name(".env.example")
        lines = example.read_text(encoding="utf-8").splitlines() if example.is_file() else []
    for index, line in enumerate(lines):
        if _dotenv_key(line) == key:
            lines[index] = f"{key}={value}"
            break
    else:
        lines.append(f"{key}={value}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _read_json(source: str | None) -> Any:
    """Read JSON from a path, or from stdin when the source is ``-``/absent."""
    if source in (None, "-"):
        text = sys.stdin.read()
    else:
        path = Path(str(source))
        if not path.exists():
            raise AgentError(f"{path} does not exist")
        text = path.read_text()
    text = text.strip()
    if not text:
        raise AgentError("no JSON input provided")
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise AgentError(f"input is not valid JSON: {exc}") from exc


def _env_file(args: argparse.Namespace) -> Path:
    """``--env-file``, else ``.env`` beside the config directory (the repo root)."""
    if args.env_file:
        return Path(args.env_file)
    return Path(args.config_dir).parent / ".env"


def _context(args: argparse.Namespace) -> tuple[AgentConfig, StateCache, PriceStore, AuditLog]:
    config = load_config(args.config_dir, data_dir=args.data_dir)
    return (
        config,
        StateCache(config.data_dir / "market_state.json"),
        PriceStore(config.price_store_path),
        AuditLog(config.audit_path),
    )


# -- commands -------------------------------------------------------------


def cmd_status(args: argparse.Namespace) -> int:
    config, state, store, audit = _context(args)
    kill_switch = KillSwitch(config.kill_switch_path)
    activity = audit.daily_activity()

    print(f"execution mode : {config.execution_mode.value}")
    print(f"watchlist      : {', '.join(config.watchlist)}")
    print(
        "account        : "
        + (
            f"rhs_account_number {config.rhs_account_number}"
            if config.rhs_account_number
            else "NOT CONFIGURED -- set RHCA_RHS_ACCOUNT_NUMBER or config/agent.yaml"
        )
    )
    print(kill_switch.state().describe())
    print()
    print(activity.describe())
    limits = config.risk
    print(
        f"  daily notional remaining: "
        f"${round_money(activity.remaining_notional(limits.max_daily_notional_usd))} "
        f"of ${limits.max_daily_notional_usd}"
    )
    print(
        f"  daily loss              : ${round_money(activity.realized_loss)} "
        f"of ${limits.max_daily_loss_usd} cap"
    )
    if activity.realized_loss >= limits.max_daily_loss_usd:
        print("  DAILY LOSS CAP REACHED -- engage the kill switch and stop for the day")
    print()

    for age in state.ages():
        print(f"  {age.describe()}")
    print()

    heartbeat = files_mod.read_json(config.heartbeat_path)
    for line in runner_mod.describe_heartbeat(
        heartbeat,
        stale_after_seconds=runner_mod.stale_after_seconds(config),
    ):
        print(line)
    present = [name for name in CREDENTIAL_ENVS if os.environ.get(name)]
    missing = [name for name in CREDENTIAL_ENVS if not os.environ.get(name)]
    print(f"  keys set     : {', '.join(present) or 'none'}")
    if missing:
        print(f"  keys missing : {', '.join(missing)}")
    key_account = str((heartbeat or {}).get("robinhood_account") or "")
    if key_account:
        expected = os.environ.get(CRYPTO_ACCOUNT_ENV, "").strip()
        if not expected:
            verdict = f"not pinned -- set {CRYPTO_ACCOUNT_ENV}"
        elif key_account[-4:] == expected[-4:]:
            verdict = "matches the pin"
        else:
            verdict = f"DOES NOT MATCH the pinned {mask(expected)}"
        print(
            f"  robinhood    : API key reads crypto account {key_account} ({verdict}), "
            "for quotes and pairs only"
        )
    print()

    for line in describe_split(config, audit, state):
        print(line)
    print()

    for line in shadow_mod.describe_status(config, files_mod.read_json(config.shadow_path)):
        print(line)
    print()

    coverages = [
        store.coverage(
            symbol,
            interval_minutes=config.strategy.bar_interval_minutes,
            required_bars=config.strategy.required_bars,
        )
        for symbol in config.watchlist
    ]
    print(reports.render_coverage(coverages))
    return EXIT_OK


def describe_split(config: AgentConfig, audit: AuditLog, state: StateCache) -> list[str]:
    """``rhca status`` lines for the split: the rule, then each sleeve's cash,
    holdings and P&L as the recorded fills have them."""
    settings = config.strategy
    _, plan = split_rules(config)
    book = split_book(
        audit,
        long_capital=settings.split_long_capital,
        short_capital=settings.split_short_capital,
    )
    quotes = state.quotes()
    lines = [
        f"split          : ${round_money(settings.split_long_capital)} long-term ({plan.label}) + "
        f"${round_money(settings.split_short_capital)} short-term (breakout) on "
        f"{', '.join(config.watchlist)}; no coin over "
        f"{config.risk.max_position_pct_of_portfolio}% of the account"
    ]
    for sleeve in (book.long, book.short):
        lines.append(
            f"  {sleeve.name:13}: cash ${round_money(sleeve.cash)} of "
            f"${round_money(sleeve.capital)}; realized {money_sign(sleeve.realized_pnl)}"
        )
        for symbol in config.watchlist:
            holding = sleeve.holdings.get(symbol)
            if holding is None:
                continue
            described = _describe_holding(holding, quotes.get(symbol))
            if described:
                lines.append(f"    {symbol:11}: {described}")
    return lines


def _describe_holding(holding: Holding, quote: Any) -> str:
    parts = []
    if holding.held:
        parts.append(f"holding {format_decimal(holding.quantity)}")
        if holding.entry_day is not None:
            parts.append(f"since the {holding.entry_day:%Y-%m-%d} close")
        parts.append(f"cost ${round_money(holding.cost)}")
        if quote is not None:
            value = holding.quantity * quote.bid
            parts.append(
                f"worth ${round_money(value)} at the bid ({money_sign(value - holding.cost)})"
            )
    if holding.tranches:
        parts.append(f"{holding.tranches} tranche(s) bought")
    if holding.open_buy:
        parts.append("a buy is open")
    if holding.open_sell:
        parts.append("a sell is open")
    if holding.realized_pnl:
        parts.append(f"realized {money_sign(holding.realized_pnl)}")
    return "; ".join(parts)


def money_sign(value: Decimal) -> str:
    rounded = round_money(value)
    return f"-${-rounded}" if rounded < 0 else f"+${rounded}"


def cmd_ingest(args: argparse.Namespace) -> int:
    config, state, store, audit = _context(args)
    payload = _read_json(args.file)
    kind = args.kind

    if kind == "quotes":
        quotes = parse_quotes(payload)
        if not quotes:
            raise AgentError("no usable quotes in the payload")
        written = store.record_quotes(quotes)
        state.put_quotes(quotes)
        print(f"recorded {written} quote observation(s):")
        for quote in quotes:
            print(
                f"  {quote.symbol:12} mark {format_decimal(quote.mark)} "
                f"spread {quote.spread_pct:.3f}%"
            )
        return EXIT_OK

    if kind == "pairs":
        pairs = parse_currency_pairs(payload)
        if not pairs:
            raise AgentError("no currency pairs in the payload")
        state.put_pairs(pairs)
        print(f"cached constraints for {len(pairs)} pair(s):")
        for pair in pairs:
            flags = []
            if pair.market_orders_only:
                flags.append("market-only")
            if pair.halted:
                flags.append(f"HALTED {','.join(pair.halted_regions) or 'ALL'}")
            if not pair.tradable:
                flags.append("untradable")
            print(
                f"  {pair.symbol:12} increment {format_decimal(pair.quantity_increment)}"
                + (f"  [{'; '.join(flags)}]" if flags else "")
            )
        return EXIT_OK

    if kind == "positions":
        positions = parse_positions(payload)
        state.put_positions(positions)
        print(f"cached {len(positions)} open position(s):")
        for position in positions:
            print(f"  {position.symbol:12} {format_decimal(position.quantity)}")
        return EXIT_OK

    if kind == "accounts":
        accounts = parse_accounts(payload)
        if not accounts:
            raise AgentError("no accounts in the payload")
        # The one to cache is the one orders go to: the account named by
        # --rhs-account-number, else the one this agent may trade, else the
        # first. The first is usually the owner's default account, which the
        # agent cannot trade.
        tradable = [a for a in accounts if a.agentic_allowed]
        account = tradable[0] if tradable else accounts[0]
        if len(accounts) > 1 and args.rhs_account_number:
            matching = [
                a for a in accounts if a.rhs_account_number == args.rhs_account_number
            ]
            if matching:
                account = matching[0]
        state.put_account(account)
        print(f"cached account {account.account_number}")
        print(f"  rhs_account_number (for order tools): {account.rhs_account_number}")
        if account.agentic_allowed is False:
            print(
                "  warning: this agent cannot trade this account; orders to it will be "
                "refused. Pass the Agentic account's --rhs-account-number.",
                file=sys.stderr,
            )
        if len(accounts) > 1:
            if args.rhs_account_number and account.rhs_account_number == args.rhs_account_number:
                why = "the one named"
            elif account.agentic_allowed:
                why = "the one this agent can trade"
            else:
                why = "the first"
            print(
                f"  note: {len(accounts)} accounts returned; cached {why}. "
                "Pass --rhs-account-number to pick a specific one."
            )
        return EXIT_OK

    if kind == "portfolio":
        # get_portfolio for the Agentic account, the one orders go to. rhca run
        # reads no balance: its API key can only read the main crypto account.
        rows = unwrap_results(payload)
        if not rows:
            raise AgentError("no portfolio data in the payload")
        row = rows[0]
        value = None
        for key in (
            "total_value",
            "total_market_value",
            "market_value",
            "equity",
            "total_equity",
            "portfolio_value",
            "crypto_market_value",
        ):
            if row.get(key) is not None:
                value = to_decimal(row[key], field=key)
                break
        if value is None:
            raise AgentError(
                "could not find a portfolio value in the payload. Expected one of "
                "total_value, total_market_value, market_value, equity, total_equity, "
                "portfolio_value."
            )
        state.put_portfolio_value(value)
        print(f"cached portfolio value ${round_money(value)}")
        buying_power = _crypto_buying_power(row)
        if buying_power is None:
            # Robinhood omits it when unavailable. The top-level buying_power is
            # no substitute: crypto is cash-only, and that figure can include margin.
            print(
                "  no crypto_buying_power in the payload; the cached one is unchanged",
                file=sys.stderr,
            )
        else:
            state.put_crypto_buying_power(buying_power)
            print(f"cached crypto buying power ${round_money(buying_power)}")
        return EXIT_OK

    raise AgentError(f"unknown ingest kind: {kind}")


def _crypto_buying_power(row: dict[str, Any]) -> Decimal | None:
    """``crypto_buying_power`` from a ``get_portfolio`` row: ``{"buying_power": ...}``,
    or a bare amount."""
    raw = row.get("crypto_buying_power")
    if isinstance(raw, dict):
        raw = raw.get("buying_power")
    return to_decimal(raw, field="crypto_buying_power") if raw is not None else None


#: Quotes per bar that stand for a fully sampled exchange candle.
FULL_BAR_OBSERVATIONS = 4


def candles_from_rows(
    payload: Any, symbol: str, interval_minutes: int, *, observations: int = 1
) -> list[Candle]:
    """Bars from start/open/high/low/close objects (or the *_price spellings).

    ``observations`` is how many quotes each bar stands for. import-history
    keeps 1, so its bars never read as better sampled than they were proven
    to be; a backtest reading complete exchange candles passes
    ``FULL_BAR_OBSERVATIONS``, as the bootstrap does.
    """
    rows = payload if isinstance(payload, list) else unwrap_results(payload)
    candles: list[Candle] = []
    for row in rows:
        try:
            start = parse_timestamp(str(row.get("start") or row.get("begins_at") or row["timestamp"]))
            open_price = to_decimal(row.get("open") or row["open_price"], field="open")
            high = to_decimal(row.get("high") or row["high_price"], field="high")
            low = to_decimal(row.get("low") or row["low_price"], field="low")
            close = to_decimal(row.get("close") or row["close_price"], field="close")
        except (KeyError, ValueError, AgentError):
            continue
        end_raw = row.get("end")
        end = (
            parse_timestamp(str(end_raw))
            if end_raw
            else start + timedelta(minutes=interval_minutes)
        )
        candles.append(
            Candle(
                symbol=symbol,
                start=start,
                end=end,
                open=open_price,
                high=high,
                low=low,
                close=close,
                observations=observations,
            )
        )
    if not candles:
        raise AgentError(
            "no usable bars found. Expected objects with start/open/high/low/close "
            "(or begins_at/open_price/high_price/low_price/close_price)."
        )
    return sorted(candles, key=lambda c: c.start)


def cmd_import_history(args: argparse.Namespace) -> int:
    config, state, store, audit = _context(args)
    symbol = canonical(args.symbol)
    candles = candles_from_rows(_read_json(args.file), symbol, args.interval)
    written = store.import_candles(candles)
    print(
        f"imported {len(candles)} bar(s) for {symbol} as {written} synthetic "
        "observation(s), marked source=import"
    )
    coverage = store.coverage(
        symbol,
        interval_minutes=config.strategy.bar_interval_minutes,
        required_bars=config.strategy.required_bars,
    )
    print(f"  {coverage.describe()}")
    return EXIT_OK


def cmd_analyze(args: argparse.Namespace) -> int:
    config, state, store, audit = _context(args)
    agent = Agent(config, store=store, audit=audit)

    market = MarketState(
        quotes=state.quotes(),
        constraints=state.pairs(),
        positions=state.positions(),
        portfolio_value=state.portfolio_value(),
        positions_as_of=runner_mod.positions_as_of(state),
    )
    if not market.quotes:
        raise AgentError(
            "no cached quotes. Call get_crypto_quotes and pipe the response into "
            "`rhca ingest quotes` first."
        )

    symbols = [canonical(s) for s in args.symbols.split(",")] if args.symbols else None
    result = agent.analyze(market, symbols=symbols, record=not args.no_record)

    if args.json:
        print(
            json.dumps(
                {
                    "generated_at": str(result.generated_at),
                    "proposals": [p.to_dict() for p in result.proposals],
                    "skipped": [
                        {"symbol": o.symbol, "reason": o.skipped_reason, "bars": o.bars}
                        for o in result.outcomes
                        if not o.proposals
                    ],
                },
                indent=2,
            )
        )
    else:
        print(reports.render_analysis(result, verbose=args.verbose))

    return EXIT_OK if result.executable else EXIT_BLOCKED


def _load_proposal(audit: AuditLog, proposal_id: str):
    record = audit.find_proposal(proposal_id)
    if record is None:
        raise AgentError(
            f"no proposal {proposal_id} in the audit log. Run `rhca analyze` first, "
            "and approve a proposal by the id it printed."
        )
    payload = record.get("proposal")
    if not isinstance(payload, dict):
        raise AgentError(f"audit record for {proposal_id} has no proposal body")
    return proposal_from_dict(payload)


def cmd_plan_order(args: argparse.Namespace) -> int:
    config, state, store, audit = _context(args)
    proposal = _load_proposal(audit, args.proposal_id)

    requests = build_plan_requests(
        proposal,
        config=config,
        tool=CRYPTO_TOOLS["preview"],
        constraints=_pair_constraints(state, proposal.symbol),
    )
    print(
        reports.render_requests(
            requests, tool_label=f"PREVIEW plan for {proposal.proposal_id}"
        )
    )
    print()
    print(
        "These are previews. They do not place anything. Previewing before placing is "
        "the documented default for the order tools."
    )
    return EXIT_OK


def _pair_constraints(state: StateCache, symbol: str) -> PairConstraints | None:
    """The cached pair, whose tick the limit price is snapped to.

    Without it the price goes out as proposed, and Robinhood may reject it as
    off the tick -- so say how to fix that before it gets that far.
    """
    pair = state.pairs().get(symbol)
    if pair is None or not pair.price_increment:
        print(
            f"warning: no price tick cached for {symbol}, so the limit price is not "
            "rounded to it. Pipe get_currency_pairs into `rhca ingest pairs` first.",
            file=sys.stderr,
        )
    return pair


def cmd_approve(args: argparse.Namespace) -> int:
    config, state, store, audit = _context(args)
    proposal = _load_proposal(audit, args.proposal_id)

    quotes = parse_quotes(_read_json(args.quote)) if args.quote else []
    live = next(
        (q for q in quotes if canonical(q.symbol) == proposal.symbol),
        state.quotes().get(proposal.symbol),
    )
    if live is None:
        raise AgentError(
            f"no live quote for {proposal.symbol}. Re-fetch get_crypto_quotes and pass "
            "it with --quote so the price can be re-checked before submitting."
        )

    gate = ApprovalGate(config, kill_switch=KillSwitch(config.kill_switch_path), audit=audit)
    authorization = gate.authorize(
        proposal,
        approval_text=args.approval,
        live_quote=live,
        tranche_index=args.tranche,
        ref_id=args.ref_id,
        tool=CRYPTO_TOOLS["place"],
        constraints=_pair_constraints(state, proposal.symbol),
    )

    print(authorization.describe())
    print()
    print(reports.render_requests([authorization.request], tool_label="PLACE THIS ORDER"))
    print()
    print(
        "After the tool returns, record the real response:\n"
        f"  rhca record-execution {proposal.proposal_id} "
        f"--tranche {authorization.tranche_index} --file response.json"
        + (" --override" if authorization.overridden else "")
    )
    return EXIT_OK


def cmd_record_execution(args: argparse.Namespace) -> int:
    config, state, store, audit = _context(args)
    payload = _read_json(args.file)
    record = parse_order_response(
        payload,
        proposal_id=args.proposal_id,
        tranche_index=args.tranche,
        overridden=args.override,
    )
    audit.record_execution(record)

    print(
        f"recorded execution for {record.proposal_id}: {record.state} "
        f"{format_decimal(record.filled_quantity)} {record.symbol} "
        f"(${round_money(record.notional)})"
    )
    if record.overridden:
        print("  logged as a RISK OVERRIDE")

    activity = audit.daily_activity()
    limits = config.risk
    print(f"  {activity.describe()}")
    remaining = activity.remaining_notional(limits.max_daily_notional_usd)
    print(f"  daily notional remaining: ${round_money(remaining)}")

    if activity.realized_loss >= limits.max_daily_loss_usd:
        KillSwitch(config.kill_switch_path).engage(REASON_DAILY_LOSS)
        audit.record_kill_switch(True, REASON_DAILY_LOSS)
        print("  DAILY LOSS CAP REACHED -- kill switch engaged automatically")
    return EXIT_OK


def cmd_record_pnl(args: argparse.Namespace) -> int:
    config, state, store, audit = _context(args)
    day = day_from(args.date)

    if args.amount is not None:
        amount = to_decimal(args.amount, field="amount")
        source = "manual"
    else:
        payload = _read_json(args.file)
        rows = unwrap_results(payload)
        if not rows:
            raise AgentError("no realized P&L rows in the payload")
        amount = Decimal(0)
        for row in rows:
            for key in ("realized_pnl", "total_realized_pnl", "net_realized_pnl", "pnl"):
                if row.get(key) is not None:
                    amount += to_decimal(row[key], field=key)
                    break
        source = "get_realized_pnl"

    audit.record_pnl(day, amount, source)
    activity = audit.daily_activity(day)
    print(f"recorded realized P&L ${format_decimal(amount)} for {day} (source: {source})")
    print(f"  {activity.describe()}")

    if activity.realized_loss >= config.risk.max_daily_loss_usd:
        KillSwitch(config.kill_switch_path).engage(REASON_DAILY_LOSS)
        audit.record_kill_switch(True, REASON_DAILY_LOSS)
        print("  DAILY LOSS CAP REACHED -- kill switch engaged automatically")
    return EXIT_OK


def cmd_audit(args: argparse.Namespace) -> int:
    config, state, store, audit = _context(args)

    if args.proposal_id:
        record = audit.find_proposal(args.proposal_id)
        if record is None:
            raise AgentError(f"no proposal {args.proposal_id} in the audit log")
        print(json.dumps(record, indent=2))
        executions = audit.executions_for(args.proposal_id)
        print(f"\n{len(executions)} execution(s) recorded:")
        for execution in executions:
            print(
                f"  tranche {execution.get('tranche_index')}: {execution.get('state')} "
                f"{execution.get('filled_quantity')} (${execution.get('notional')})"
            )
        print(f"total filled: {format_decimal(audit.filled_quantity_for(args.proposal_id))}")
        return EXIT_OK

    day = day_from(args.date) if args.date else None
    activity = audit.daily_activity(day)
    print(activity.describe())
    if activity.symbols_traded:
        print(f"  symbols traded: {', '.join(activity.symbols_traded)}")
    print()
    for record in audit.recent(args.limit, kind=args.kind):
        summary = {
            k: v for k, v in record.items() if k not in {"proposal", "raw_response"}
        }
        print(json.dumps(summary))
    return EXIT_OK


def cmd_kill_switch(args: argparse.Namespace) -> int:
    config, state, store, audit = _context(args)
    kill_switch = KillSwitch(config.kill_switch_path)

    if args.action == "status":
        print(kill_switch.state().describe())
        return EXIT_OK
    if args.action == "on":
        result = kill_switch.engage(args.reason or REASON_MANUAL)
        audit.record_kill_switch(True, result.reason or REASON_MANUAL)
        print(result.describe())
        return EXIT_OK

    result = kill_switch.release()
    audit.record_kill_switch(False, args.reason or REASON_MANUAL)
    print(result.describe())
    print("Trading proposals can be executed again once they pass the risk engine.")
    return EXIT_OK


def cmd_describe_tools(args: argparse.Namespace) -> int:
    print("RobinHood MCP crypto tools this agent uses:\n")
    for contract in TOOL_CONTRACTS:
        marker = "!! MUTATING" if contract.mutating else "   read-only"
        print(f"  {marker}  {contract.name:24} {contract.summary}")
    print(
        "\nOrder payload rules enforced by `rhca validate-order`:\n"
        "  - rhs_account_number must be the NUMERIC account number\n"
        "  - exactly one of quantity or dollar_amount\n"
        "  - limit_price required for limit/stop_limit; stop_price for stop_loss/stop_limit\n"
        "  - market and limit accept only time_in_force 'gtc'; 'ioc' is never valid\n"
        "  - ref_id must be a UUID and is re-sent verbatim when retrying\n"
        "  - tax_lots: sell only, quantity only, <= 50 lots, summing to the quantity"
    )
    return EXIT_OK


def cmd_validate_order(args: argparse.Namespace) -> int:
    payload = _read_json(args.file)
    if not isinstance(payload, dict):
        raise AgentError("expected a JSON object of order arguments")
    validate_crypto_order_args(payload)
    print("valid: this payload satisfies the RobinHood crypto order contract")
    print(json.dumps(payload, indent=2))
    return EXIT_OK


def _scoring_args(args: argparse.Namespace) -> tuple[int, Decimal]:
    horizon = int(getattr(args, "horizon", None) or DEFAULT_HORIZON_BARS)
    hurdle = (
        to_decimal(args.hurdle, field="hurdle")
        if getattr(args, "hurdle", None) is not None
        else DEFAULT_HURDLE_PCT
    )
    return horizon, hurdle


def cmd_accuracy(args: argparse.Namespace) -> int:
    config, state, store, audit = _context(args)
    horizon, hurdle = _scoring_args(args)
    payload = dashboard_mod.build_payload(
        config, audit=audit, store=store, horizon_bars=horizon, hurdle_pct=hurdle
    )

    if args.json:
        print(json.dumps(payload["stats"], indent=2))
        return EXIT_OK

    overall = payload["stats"]["overall"]
    print(
        f"scored over {horizon} bars against a {hurdle}% hurdle "
        f"({config.strategy.bar_interval_minutes}-minute bars)"
    )
    print()
    rate = overall["win_rate"]
    print(f"  proposals      : {overall['total']}")
    print(f"  resolved       : {overall['resolved']} ({overall['pending']} still pending)")
    print(
        "  hit rate       : "
        + (
            f"{rate:.1%} ({overall['wins']}W / {overall['losses']}L, "
            f"{overall['flat']} flat)"
            if rate is not None
            else "unknown -- nothing has resolved yet"
        )
    )
    if overall["average_move_pct"] is not None:
        print(f"  average move   : {overall['average_move_pct']}%")
        print(f"  best / worst   : {overall['best_move_pct']}% / {overall['worst_move_pct']}%")

    for label, key in (
        ("by regime", "by_regime"),
        ("by symbol", "by_symbol"),
        ("by status", "by_status"),
    ):
        grouped = payload["stats"][key]
        if not grouped:
            continue
        print(f"\n  {label}:")
        for name, stats in sorted(grouped.items()):
            group_rate = stats["win_rate"]
            rendered = f"{group_rate:.1%}" if group_rate is not None else "unknown"
            print(
                f"    {name:20} {rendered:>8}  "
                f"({stats['wins']}W/{stats['losses']}L/{stats['flat']}F, "
                f"{stats['pending']} pending)"
            )

    if overall["resolved"] == 0:
        print(
            "\nNo proposal has resolved yet. Hit rate is unknown, not zero -- "
            "ingest quotes over the horizon so outcomes can be scored."
        )
    return EXIT_OK


def cmd_dashboard_export(args: argparse.Namespace) -> int:
    config, state, store, audit = _context(args)
    horizon, hurdle = _scoring_args(args)
    payload = dashboard_mod.build_payload(
        config,
        audit=audit,
        store=store,
        horizon_bars=horizon,
        hurdle_pct=hurdle,
        limit=args.limit,
        repo_url=args.repo_url,
    )
    rendered = json.dumps(payload, indent=2)
    if args.out:
        path = Path(args.out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rendered + "\n")
        print(f"wrote {len(payload['proposals'])} proposal(s) to {path}")
    else:
        print(rendered)
    return EXIT_OK


def cmd_dashboard_sync(args: argparse.Namespace) -> int:
    config, state, store, audit = _context(args)
    token = args.token or os.environ.get("RHCA_DASHBOARD_TOKEN")
    base_url = args.url or os.environ.get("RHCA_DASHBOARD_URL")
    if not base_url:
        raise AgentError(
            "no dashboard URL. Pass --url or set RHCA_DASHBOARD_URL."
        )
    if not token:
        raise AgentError(
            "no dashboard token. Pass --token or set RHCA_DASHBOARD_TOKEN."
        )

    horizon, hurdle = _scoring_args(args)
    result = dashboard_mod.sync(
        config,
        audit=audit,
        store=store,
        base_url=base_url,
        token=token,
        horizon_bars=horizon,
        hurdle_pct=hurdle,
        limit=args.limit,
        repo_url=args.repo_url,
    )
    print(f"pushed {result.pushed} proposal(s) to {base_url}")

    decisions = result.decisions
    if not decisions:
        print("no pending decisions on the dashboard")
        return EXIT_OK

    print(
        f"\n{len(decisions)} decision(s) recorded on the dashboard "
        f"({len(result.new_decisions)} new since the last sync):"
    )
    for decision in decisions:
        marker = "ACCEPT" if decision.kind.value == "accept" else "decline"
        print(f"  [{marker}] {decision.proposal_id}  ({decision.decided_at.isoformat()})")
        if decision.kind.value == "accept":
            print(
                f"      to act on it: rhca approve {decision.proposal_id} "
                f'--approval "{decision.approval_text}" --quote fresh.json'
            )
    print(
        "\nA recorded decision goes through the same approval gate as a typed one: "
        "kill switch, live price-drift re-check, remaining quantity and contract "
        "validation all still apply. It can never override a risk block."
    )
    return EXIT_OK


def import_recent_history(
    config: AgentConfig, store: PriceStore
) -> tuple[list[tuple[str, int, str]], list[str]]:
    """Fill in whichever recent bars this store is missing, symbol by symbol.

    Returns ``(imported, failures)``: one ``(symbol, bars, coverage)`` row per
    symbol that worked, and a message per symbol that did not. A symbol
    Coinbase will not serve is returned rather than raised, because the two
    callers want opposite things from it -- ``bootstrap-history`` is asking for
    the import and should fail loudly, while ``run`` must still start when
    Coinbase is unreachable.
    """
    imported: list[tuple[str, int, str]] = []
    failures: list[str] = []
    interval = config.strategy.bar_interval_minutes
    for symbol in config.watchlist:
        try:
            # Enough for the trend average: its window plus two days' slack.
            candles = fetch_coinbase_history(
                symbol, interval_minutes=interval, days=config.strategy.history_days
            )
        except AgentError as exc:
            failures.append(f"{symbol}: {exc}")
            continue
        existing = {
            c.start
            for c in store.candles(symbol, interval_minutes=interval, include_partial=True)
        }
        missing = [c for c in candles if c.start not in existing]
        store.import_candles(missing)
        coverage = store.coverage(
            symbol, interval_minutes=interval, required_bars=config.strategy.required_bars
        )
        imported.append((symbol, len(missing), coverage.describe()))
    return imported, failures


def warm_daily_bars(config: AgentConfig) -> tuple[str, list[str]]:
    """Fetch the daily closes the split decides on, so the first pass has them.
    A coin Coinbase will not serve is reported, not raised: ``run`` must start
    anyway, and says why that coin gets no proposal."""
    agent = Agent(config)
    book = agent.book()
    days = agent.history_days(book, latest_close(utcnow()))
    last: list[str] = []
    failures: list[str] = []
    for symbol in config.watchlist:
        try:
            bars = agent.daily.get(symbol, days=days)
        except AgentError as exc:
            failures.append(f"{symbol}: {exc}")
            continue
        last.append(f"{symbol} through {bars[-1].start:%Y-%m-%d}")
    return (", ".join(last) or "none"), failures


def cmd_bootstrap_history(args: argparse.Namespace) -> int:
    config, state, store, audit = _context(args)
    imported, failures = import_recent_history(config, store)
    for symbol, bars, coverage in imported:
        print(f"{symbol}: imported {bars} bar(s) from Coinbase. {coverage}")
    for failure in failures:
        print(f"WARNING {failure}", file=sys.stderr)
    if failures and not imported:
        raise AgentError("no symbol could be bootstrapped from Coinbase")
    print(
        "\nImported bars are marked source=import. They set the anchor and the trend "
        "average; proposals are always priced off a live Robinhood quote."
    )
    return EXIT_OK


def cmd_keygen(args: argparse.Namespace) -> int:
    """Make the Ed25519 key pair Robinhood's API key setup asks for.

    The private half goes straight into .env and is never printed; only the
    public half is shown, for pasting into Robinhood's "Add key" form.
    """
    env_path = _env_file(args)
    in_file = read_dotenv(env_path).get(ROBINHOOD_PRIVATE_KEY_ENV)
    in_environment = os.environ.get(ROBINHOOD_PRIVATE_KEY_ENV)
    if (in_file or in_environment) and not args.force:
        raise AgentError(
            f"{ROBINHOOD_PRIVATE_KEY_ENV} is already set. A new private key would not match "
            "the public key already registered with Robinhood. Pass --force only if you are "
            "replacing the key at Robinhood too."
        )

    private_key, public_key = generate_key_pair()
    set_dotenv_value(env_path, ROBINHOOD_PRIVATE_KEY_ENV, private_key)
    print(f"Wrote a new {ROBINHOOD_PRIVATE_KEY_ENV} to {env_path.resolve()}.")
    print("It is not shown here, and it never leaves this machine: it only signs requests.")
    if in_environment:
        print(
            f"Note: {ROBINHOOD_PRIVATE_KEY_ENV} is also set in your environment, which wins "
            "over .env. Remove it there, or this new key will not be used."
        )
    print()
    print("Paste this PUBLIC key into Robinhood's \"Add key\" form:")
    print()
    print(f"    {public_key}")
    print()
    print(
        f"Then copy the API key Robinhood shows you into {env_path.name} as "
        f"{ROBINHOOD_KEY_ENV}=... (docs/runbook.md, 'Keys', has the full steps)."
    )
    return EXIT_OK


def describe_ingested_balance(state: StateCache) -> str:
    """The Agentic account's balance and holdings, as ``rhca ingest`` last cached them."""
    buying_power = state.crypto_buying_power()
    if buying_power is None:
        return (
            "balance NEVER INGESTED -- pipe its get_portfolio and get_crypto_positions "
            "into `rhca ingest` (docs/runbook.md, 'Balance and holdings')"
        )
    age = next(a for a in state.ages() if a.name == SECTION_CRYPTO_BUYING_POWER)
    return (
        f"crypto buying power ${round_money(buying_power)}, "
        f"{len(state.positions())} position(s), balance ingested "
        f"{(age.age_seconds or 0.0) / 60:.0f} minutes ago"
    )


BREAKOUT = "breakout"
SPLIT = "split"
#: What --strategies accepts: the per-coin strategies, and the breakout and the
#: split, which run as one account across every symbol.
BACKTEST_STRATEGIES = (*backtest_mod.STRATEGY_NAMES, BREAKOUT, SPLIT)

#: How long fetched backtest history is reused before it is fetched again.
BACKTEST_CACHE_HOURS = 6


def backtest_history(
    config: AgentConfig, symbol: str, *, days: int, refresh: bool = False
) -> list[Candle]:
    """``days`` of Coinbase bars, cached under data/backtest/.

    Kept out of the price store on purpose: months of bars there would slow
    every read the live loop makes, and the loop needs only the recent ones.
    """
    interval = config.strategy.bar_interval_minutes
    path = config.data_dir / "backtest" / f"{canonical(symbol)}-{interval}m.json"
    cached = files_mod.read_json(path)
    if cached is not None and not refresh:
        try:
            fetched_at = parse_timestamp(str(cached["fetched_at"]))
            fresh = (
                int(cached.get("days", 0)) >= days
                and (utcnow() - fetched_at).total_seconds()
                < BACKTEST_CACHE_HOURS * 3600
            )
        except (KeyError, ValueError, TypeError):
            fresh = False
        if fresh:
            bars = candles_from_rows(
                cached.get("bars") or [], symbol, interval, observations=FULL_BAR_OBSERVATIONS
            )
            oldest = utcnow() - timedelta(days=days)
            return [c for c in bars if c.start >= oldest]
    candles = fetch_coinbase_history(symbol, interval_minutes=interval, days=days)
    if not candles:
        raise AgentError(f"Coinbase returned no {interval}-minute bars for {symbol}")
    files_mod.write_json_atomic(
        path,
        {
            "fetched_at": utcnow().isoformat(),
            "days": days,
            "bars": [
                {
                    "start": c.start.isoformat(),
                    "open": format_decimal(c.open),
                    "high": format_decimal(c.high),
                    "low": format_decimal(c.low),
                    "close": format_decimal(c.close),
                }
                for c in candles
            ],
        },
    )
    return candles


def cmd_backtest(args: argparse.Namespace) -> int:
    config = load_config(args.config_dir, data_dir=args.data_dir)
    symbols = (
        [canonical(s) for s in args.symbols.split(",") if s.strip()]
        if args.symbols
        else list(config.watchlist)
    )
    try:
        steps = (
            backtest_mod.parse_ladder(args.ladder) if args.ladder else config.strategy.steps
        )
    except (ValueError, ArithmeticError) as exc:
        raise AgentError(f"--ladder: {exc}") from exc
    round_trip = to_decimal(args.spread_pct, field="spread-pct")
    strategies = [s.strip() for s in args.strategies.split(",") if s.strip()]
    unknown = sorted(set(strategies) - set(BACKTEST_STRATEGIES))
    if unknown:
        raise AgentError(
            f"unknown strategies {unknown}; choose from {list(BACKTEST_STRATEGIES)} "
            "(System 1's 'signal' was retired with it)"
        )
    per_coin = [s for s in strategies if s in backtest_mod.STRATEGY_NAMES]
    rule = None
    if BREAKOUT in strategies or SPLIT in strategies:
        try:
            # The rule's own 10% per coin unless asked otherwise: it was fixed
            # with the rule, and the account-wide limit in risk_limits.yaml is
            # a separate cap (the split's --coin-cap-pct).
            rule = Breakout(
                risk_pct=to_decimal(args.risk_pct, field="risk-pct"),
                **(
                    {"max_weight_pct": to_decimal(args.max_weight_pct, field="max-weight-pct")}
                    if args.max_weight_pct is not None
                    else {}
                ),
            )
        except ValueError as exc:
            raise AgentError(f"breakout: {exc}") from exc
    capital = to_decimal(args.capital, field="capital")
    if capital <= 0:
        raise AgentError("--capital must be positive")
    plan, long_pct, long_symbols, coin_cap = None, Decimal("50"), None, None
    if SPLIT in strategies:
        plan, long_pct, long_symbols, coin_cap = split_settings(args, symbols, capital, config)

    trend_days = config.strategy.trend_days if args.trend_days is None else args.trend_days
    if trend_days < 0:
        raise AgentError("--trend-days must be 0 (off) or a number of days")
    if args.roll_window < 0 or args.roll_step <= 0:
        raise AgentError("--roll-window must be 0 (off) or days, and --roll-step positive")
    interval = config.strategy.bar_interval_minutes
    # Days of history fetched before the window, only to warm up averages: the
    # ladder's trend average, the breakout's 100-day one, and the long-term
    # sleeve's 200-day one.
    warmup_days = max(
        trend_days,
        rule.warmup_days + 2 if rule else 0,
        plan.average_days + 1 if plan else 0,
    )

    # The window traded stays the same whatever the warm-up.
    series: dict[str, list[Candle]] = {}
    if args.bars_file:
        if len(symbols) != 1:
            raise AgentError("--bars-file replays one symbol: pass exactly one in --symbols")
        series[symbols[0]] = candles_from_rows(
            _read_json(args.bars_file),
            symbols[0],
            interval,
            observations=FULL_BAR_OBSERVATIONS,
        )
    else:
        for symbol in symbols:
            try:
                series[symbol] = backtest_history(
                    config, symbol, days=args.days + warmup_days, refresh=args.refresh
                )
            except AgentError as exc:
                print(f"  ! {symbol}: {exc}", file=sys.stderr)
    series = {symbol: bars for symbol, bars in series.items() if bars}
    if not series:
        raise AgentError("no history to backtest: every symbol failed to load")

    full = dict(series)  # warm-up included, for the breakout's daily indicators
    window_start = (
        None
        if args.bars_file
        else max(bars[-1].end for bars in series.values()) - timedelta(days=args.days)
    )
    trends: dict[str, dict[Any, bool] | None] = {}
    for symbol, bars in list(series.items()):
        if trend_days:
            trends[symbol] = backtest_mod.above_trend(
                bars, backtest_mod.trend_bars(trend_days, interval)
            )
        else:
            trends[symbol] = None
        if not args.bars_file:
            # A bars file is replayed whole; the filter just waits for its average.
            start = bars[-1].end - timedelta(days=args.days)
            series[symbol] = [c for c in bars if c.start >= start]

    base: dict[str, list[backtest_mod.Result]] = {}
    stressed: dict[str, list[backtest_mod.Result]] = {}
    for symbol, candles in series.items() if per_coin else ():
        for runs, cost in ((base, round_trip), (stressed, round_trip * backtest_mod.STRESS_FACTOR)):
            runs[symbol] = backtest_mod.run_all(
                candles,
                strategies=per_coin,
                steps=steps,
                round_trip_pct=cost,
                trend=trends[symbol],
                trend_days=trend_days,
            )

    breakout = split = None
    min_trade = config.risk.min_notional_per_trade_usd
    prepared = portfolio_mod.prepare(full, rule) if rule is not None else {}
    if rule is not None and BREAKOUT in strategies:
        breakout = portfolio_mod.evaluate(
            prepared,
            rule,
            capital=capital,
            round_trip_pct=round_trip,
            start=window_start,
            min_trade=min_trade,
        )
    if rule is not None and plan is not None:
        missing = sorted(set(long_symbols or ()) - set(prepared))
        if missing:
            raise AgentError(f"--long-symbols: no history loaded for {', '.join(missing)}")
        split = portfolio_mod.evaluate_split(
            prepared,
            rule,
            plan,
            capital=capital,
            long_pct=long_pct,
            round_trip_pct=round_trip,
            start=window_start,
            min_trade=min_trade,
            long_symbols=long_symbols,
            coin_cap_pct=coin_cap,
        )

    if args.json:
        payload: dict[str, Any] = {
            symbol: [backtest_mod.summary(r) for r in results] for symbol, results in base.items()
        }
        if breakout is not None:
            payload[BREAKOUT] = portfolio_mod.summary(breakout)
        if split is not None:
            payload[SPLIT] = portfolio_mod.split_summary(split)
        print(json.dumps(payload, indent=2))
        return EXIT_OK
    if per_coin:
        print(
            backtest_mod.render_report(
                base,
                stressed,
                steps=steps,
                round_trip_pct=round_trip,
                config=config,
                trend_days=trend_days,
            )
        )
    printed = bool(per_coin)
    if breakout is not None and rule is not None:
        if printed:
            print()
        print(portfolio_mod.render(breakout, rule))
        printed = True
    if split is not None and rule is not None:
        if printed:
            print()
        print(portfolio_mod.render_split(split, rule))
    if args.roll_window:
        window = timedelta(days=args.roll_window)
        step = timedelta(days=args.roll_step)
        if per_coin:
            windows = backtest_mod.run_rolling(
                series,
                trends,
                window=window,
                step=step,
                strategies=per_coin,
                steps=steps,
                round_trip_pct=round_trip,
                trend_days=trend_days,
            )
            print()
            print(backtest_mod.render_rolling(windows, window=window, step=step))
        if rule is not None and breakout is not None:
            rolling = portfolio_mod.run_rolling(
                series,
                prepared,
                rule,
                window=window,
                step=step,
                capital=capital,
                round_trip_pct=round_trip,
                min_trade=min_trade,
            )
            print()
            print(portfolio_mod.render_rolling(rolling, window=window, step=step))
        if rule is not None and plan is not None:
            rolling_split = portfolio_mod.run_rolling_split(
                series,
                prepared,
                rule,
                plan,
                window=window,
                step=step,
                capital=capital,
                long_pct=long_pct,
                round_trip_pct=round_trip,
                min_trade=min_trade,
                long_symbols=long_symbols,
                coin_cap_pct=coin_cap,
            )
            print()
            print(portfolio_mod.render_rolling_split(rolling_split, window=window, step=step))
    return EXIT_OK


def split_settings(
    args: argparse.Namespace, symbols: Sequence[str], capital: Decimal, config: AgentConfig
) -> tuple[Accumulate, Decimal, list[str] | None, Decimal]:
    """The split's long-term plan, its share of --capital, its coins, and the
    most of the whole account one coin may be. What is not given defaults to
    the split the forward test runs (config/shadow.yaml)."""
    shadow = config.shadow
    try:
        plan = Accumulate(mode=args.long_mode or (shadow.long_mode if shadow else "dip"))
    except ValueError as exc:
        raise AgentError(f"--long-mode: {exc}") from exc
    long_pct = (
        to_decimal(args.long_pct, field="long-pct")
        if args.long_pct is not None
        else (shadow.long_pct if shadow else Decimal("50"))
    )
    if not 0 < long_pct < 100:
        raise AgentError("--long-pct must be strictly between 0 and 100")
    long_symbols = None
    if args.long_symbols:
        long_symbols = [canonical(s) for s in args.long_symbols.split(",") if s.strip()]
        outside = sorted(set(long_symbols) - set(symbols))
        if outside:
            raise AgentError(f"--long-symbols must be among --symbols; not {', '.join(outside)}")
    coins = len(long_symbols or symbols)
    # A small share buys fewer, minimum-sized tranches; one too small for a
    # single minimum trade could never buy at all.
    share = capital * long_pct / 100 / coins
    if share < config.risk.min_notional_per_trade_usd:
        raise AgentError(
            f"the long-term sleeve would hold ${round_money(share)} of each coin, under the "
            f"${config.risk.min_notional_per_trade_usd} minimum trade: raise --capital or "
            "--long-pct, or pass fewer --long-symbols"
        )
    coin_cap = (
        to_decimal(args.coin_cap_pct, field="coin-cap-pct")
        if args.coin_cap_pct is not None
        else config.risk.max_position_pct_of_portfolio
    )
    if not 0 < coin_cap <= 100:
        raise AgentError("--coin-cap-pct must be a percent in (0, 100]")
    return plan, long_pct, long_symbols, coin_cap


def cmd_shadow(args: argparse.Namespace) -> int:
    """The forward test's paper account, replayed from Coinbase's daily closes."""
    config = load_config(args.config_dir, data_dir=args.data_dir)
    print(shadow_mod.report(config, AuditLog(config.audit_path)))
    return EXIT_OK


def describe_forward_test(config: AgentConfig) -> str:
    """The ``rhca run`` banner's line for the forward test."""
    shadow = config.shadow
    if shadow is None:
        return "off (no config/shadow.yaml)"
    _, plan = shadow_mod.rules(config)
    return (
        f"paper split from the {shadow.start:%Y-%m-%d} close: ${round_money(shadow.long_capital)} "
        f"{plan.label} + ${round_money(shadow.short_capital)} breakout on "
        f"{', '.join(shadow.symbols)}; never proposes"
    )


def _keep_awake() -> bool:
    """Ask Windows not to sleep while this process runs (reverts when it exits)."""
    if sys.platform != "win32":
        return False
    import ctypes

    es_continuous, es_system_required = 0x80000000, 0x00000001
    return bool(
        ctypes.windll.kernel32.SetThreadExecutionState(es_continuous | es_system_required)
    )


def cmd_run(args: argparse.Namespace) -> int:
    loaded = getattr(args, "dotenv_loaded", [])
    config = load_config(args.config_dir, data_dir=args.data_dir)

    robinhood = RobinhoodClient.from_env()
    if robinhood is None:
        raise AgentError(
            f"{ROBINHOOD_KEY_ENV} and {ROBINHOOD_PRIVATE_KEY_ENV} must be set, in the "
            "environment or in .env -- see docs/runbook.md, 'Shadow run'"
        )
    account = robinhood.account()
    expected_account = os.environ.get(CRYPTO_ACCOUNT_ENV, "").strip()
    check_account(account, expected_account)
    state = StateCache(config.data_dir / "market_state.json")

    dashboard_url = os.environ.get("RHCA_DASHBOARD_URL")
    dashboard_token = os.environ.get("RHCA_DASHBOARD_TOKEN")
    services = runner_mod.Services(
        robinhood=robinhood,
        dashboard=(dashboard_url, dashboard_token) if dashboard_url and dashboard_token else None,
    )

    # A legacy Windows console encoding would otherwise turn a stray character
    # in a log line into a logging traceback.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    print("rhca run -- SHADOW MODE: proposes and records, never places an order")
    if loaded:
        print(f"  loaded from .env : {', '.join(loaded)}")
    pin = "pinned" if expected_account else f"NOT pinned -- set {CRYPTO_ACCOUNT_ENV}"
    print(
        f"  robinhood API key: crypto {mask(account.account_number)} ({pin}), "
        "for quotes and pairs only"
    )
    print(f"  agentic account  : {describe_ingested_balance(state)}")
    print(f"  watchlist        : {', '.join(config.watchlist)}")
    settings = config.strategy
    _, plan = split_rules(config)
    print(
        f"  rule             : the split, on each UTC daily close -- "
        f"${round_money(settings.split_long_capital)} long-term ({plan.label}), "
        f"${round_money(settings.split_short_capital)} short-term (breakout)"
    )
    print(
        f"  risk             : 14 rules, no coin over {config.risk.max_position_pct_of_portfolio}% "
        "of the account; a human approves each proposal by id"
    )
    print(f"  forward test     : {describe_forward_test(config)}")
    print(f"  dashboard sync   : {'on' if services.dashboard else 'off'}")
    # Bootstrapping here rather than asking for it beforehand: the trend
    # average needs weeks of bars, and every restart leaves a gap between the
    # last imported bar and the first polled one. Coinbase being unreachable
    # is not a reason to refuse to start.
    if args.no_bootstrap:
        print("  history          : bootstrap skipped (--no-bootstrap)")
    else:
        imported, failures = import_recent_history(config, PriceStore(config.price_store_path))
        bars = sum(count for _, count, _ in imported)
        note = f"{bars} hourly bar(s) imported for {len(imported)} symbol(s)"
        print(f"  history          : {note}{f', {len(failures)} failed' if failures else ''}")
        for failure in failures:
            print(f"    ! {failure}", file=sys.stderr)
        daily, daily_failures = warm_daily_bars(config)
        print(
            f"  daily closes     : {daily}"
            + (f", {len(daily_failures)} failed" if daily_failures else "")
        )
        for failure in daily_failures:
            print(f"    ! {failure}", file=sys.stderr)
    if args.keep_awake:
        print(f"  keep awake       : {'on' if _keep_awake() else 'unavailable on this OS'}")
    print("Ctrl+C to stop.\n")

    runner = runner_mod.Runner(
        config, services, robinhood_account=mask(account.account_number)
    )
    try:
        runner.run(once=args.once, max_seconds=args.minutes * 60 if args.minutes else None)
    except KeyboardInterrupt:
        print("\nstopped")
    return EXIT_OK


# -- parser ---------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rhca",
        description=(
            "Robinhood crypto agent: analyzes, proposes, and gates execution. "
            "Orders are placed only by Claude Code via MCP, after a human approves."
        ),
    )
    parser.add_argument("--config-dir", default="config", help="configuration directory")
    parser.add_argument("--data-dir", default=None, help="override the data directory")
    parser.add_argument(
        "--env-file", default=None, help="credentials file (default: .env beside config/)"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser(
        "run", help="the real-time loop in shadow mode: poll, run the split, propose, record"
    )
    run.add_argument("--once", action="store_true", help="one cycle of every task, then exit")
    run.add_argument("--minutes", type=float, default=None, help="stop after this long")
    run.add_argument(
        "--keep-awake", action="store_true", help="stop Windows sleeping while it runs"
    )
    run.add_argument(
        "--no-bootstrap",
        action="store_true",
        help="skip the Coinbase history import that otherwise runs at startup",
    )
    run.set_defaults(func=cmd_run)

    shadow = sub.add_parser(
        "shadow",
        help="the forward test: the split account on paper, against its bar (never orders)",
    )
    shadow.set_defaults(func=cmd_shadow)

    bootstrap = sub.add_parser(
        "bootstrap-history",
        help="import Coinbase bars so the trend average and anchor exist at once",
    )
    bootstrap.set_defaults(func=cmd_bootstrap_history)

    backtest = sub.add_parser(
        "backtest",
        help="replay the trend ladder, the breakout, the split, and their baselines on history",
    )
    backtest.add_argument("--symbols", default=None, help="comma-separated; default the watchlist")
    backtest.add_argument("--days", type=int, default=90, help="history to fetch (default 90)")
    backtest.add_argument(
        "--strategies",
        default="ladder,trend,hold,breakout",
        help="any of ladder, trend, hold, breakout, split",
    )
    backtest.add_argument(
        "--ladder",
        default=None,
        help="percent:dollars steps (default: config/strategy.yaml's, 5:5,10:10,20:20)",
    )
    backtest.add_argument(
        "--spread-pct", default=str(backtest_mod.DEFAULT_ROUND_TRIP_PCT),
        help="round trip charged on every trade, percent (default 1.9)",
    )
    backtest.add_argument(
        "--trend-days", type=int, default=None,
        help="the trend average's window, in days (default: config/strategy.yaml's; 0 = off)",
    )
    backtest.add_argument(
        "--roll-window", type=int, default=0,
        help="also re-run every strategy over windows this many days long (0 = off)",
    )
    backtest.add_argument(
        "--roll-step", type=int, default=30,
        help="days between rolling windows' starts (default 30)",
    )
    backtest.add_argument(
        "--capital", default=str(portfolio_mod.DEFAULT_CAPITAL),
        help="the breakout's (or the split's) starting account, dollars (default 500)",
    )
    backtest.add_argument(
        "--risk-pct", default="1",
        help="the breakout's risk per trade, percent of the account (default 1)",
    )
    backtest.add_argument(
        "--max-weight-pct", default=None,
        help="the breakout's cap per coin, percent of its account (default 10, the rule's own)",
    )
    backtest.add_argument(
        "--long-pct", default=None,
        help="split: the share of --capital held long-term, percent "
        "(default: config/shadow.yaml's, else 50)",
    )
    backtest.add_argument(
        "--long-mode", default=None, choices=LONG_MODES,
        help="split: how the long-term sleeve buys -- dip (buy low), dca, lump "
        "(default: config/shadow.yaml's, else dip)",
    )
    backtest.add_argument(
        "--long-symbols", default=None,
        help="split: the long-term sleeve's coins (default: every --symbols coin)",
    )
    backtest.add_argument(
        "--coin-cap-pct", default=None,
        help="split: the most of the whole account one coin may be, both sleeves together "
        "(default: risk_limits.yaml's max_position_pct_of_portfolio)",
    )
    backtest.add_argument("--refresh", action="store_true", help="refetch instead of the cache")
    backtest.add_argument(
        "--bars-file", default=None,
        help="replay these bars (JSON, as import-history takes) instead of fetching",
    )
    backtest.add_argument("--json", action="store_true", help="numbers as JSON")
    backtest.set_defaults(func=cmd_backtest)

    keygen = sub.add_parser(
        "keygen", help="make the Robinhood API key pair; the private half goes into .env"
    )
    keygen.add_argument(
        "--force", action="store_true", help="replace a private key that is already set"
    )
    keygen.set_defaults(func=cmd_keygen)

    status = sub.add_parser("status", help="mode, kill switch, risk usage, data coverage")
    status.set_defaults(func=cmd_status)

    ingest = sub.add_parser("ingest", help="ingest an MCP tool response")
    ingest.add_argument(
        "kind", choices=["quotes", "pairs", "positions", "accounts", "portfolio"]
    )
    ingest.add_argument("--file", "-f", default="-", help="JSON file, or - for stdin")
    ingest.add_argument("--rhs-account-number", default=None, dest="rhs_account_number")
    ingest.set_defaults(func=cmd_ingest)

    history = sub.add_parser(
        "import-history", help="import external OHLC bars to bootstrap history"
    )
    history.add_argument("symbol")
    history.add_argument("--file", "-f", default="-")
    history.add_argument("--interval", type=int, default=60, help="bar minutes")
    history.set_defaults(func=cmd_import_history)

    analyze = sub.add_parser("analyze", help="generate proposals from cached state")
    analyze.add_argument("--symbols", default=None, help="comma-separated subset")
    analyze.add_argument("--json", action="store_true", help="machine-readable output")
    analyze.add_argument("--verbose", "-v", action="store_true")
    analyze.add_argument(
        "--no-record", action="store_true", help="do not write proposals to the audit log"
    )
    analyze.set_defaults(func=cmd_analyze)

    plan = sub.add_parser("plan-order", help="emit preview payloads for a proposal")
    plan.add_argument("proposal_id")
    plan.set_defaults(func=cmd_plan_order)

    approve = sub.add_parser(
        "approve", help="validate a human approval and emit the place payload"
    )
    approve.add_argument("proposal_id")
    approve.add_argument(
        "--approval",
        required=True,
        help="the human's exact approval text; must name the proposal id",
    )
    approve.add_argument("--quote", default=None, help="fresh get_crypto_quotes JSON")
    approve.add_argument("--tranche", type=int, default=0)
    approve.add_argument("--ref-id", default=None, dest="ref_id")
    approve.set_defaults(func=cmd_approve)

    record = sub.add_parser("record-execution", help="record a place_crypto_order response")
    record.add_argument("proposal_id")
    record.add_argument("--file", "-f", default="-")
    record.add_argument("--tranche", type=int, default=None)
    record.add_argument("--override", action="store_true", help="log as a risk override")
    record.set_defaults(func=cmd_record_execution)

    pnl = sub.add_parser("record-pnl", help="record realized P&L for the daily loss cap")
    pnl.add_argument("--file", "-f", default="-")
    pnl.add_argument("--amount", default=None, help="record an amount directly")
    pnl.add_argument("--date", default=None, help="YYYY-MM-DD (default: today, UTC)")
    pnl.set_defaults(func=cmd_record_pnl)

    audit_cmd = sub.add_parser("audit", help="inspect the audit log")
    audit_cmd.add_argument("--proposal-id", default=None, dest="proposal_id")
    audit_cmd.add_argument("--date", default=None)
    audit_cmd.add_argument("--kind", default=None)
    audit_cmd.add_argument("--limit", type=int, default=20)
    audit_cmd.set_defaults(func=cmd_audit)

    kill = sub.add_parser("kill-switch", help="engage or release the kill switch")
    kill.add_argument("action", choices=["on", "off", "status"])
    kill.add_argument("--reason", default=None)
    kill.set_defaults(func=cmd_kill_switch)

    describe = sub.add_parser("describe-tools", help="the MCP tool contract this agent uses")
    describe.set_defaults(func=cmd_describe_tools)

    accuracy = sub.add_parser("accuracy", help="how good the agent's proposals have been")
    accuracy.add_argument("--horizon", type=int, default=None, help="bars to score over")
    accuracy.add_argument("--hurdle", default=None, help="win threshold, percent")
    accuracy.add_argument("--json", action="store_true")
    accuracy.set_defaults(func=cmd_accuracy)

    export = sub.add_parser("dashboard-export", help="write the dashboard payload as JSON")
    export.add_argument("--out", "-o", default=None, help="file to write (default: stdout)")
    export.add_argument("--limit", type=int, default=None)
    export.add_argument("--horizon", type=int, default=None)
    export.add_argument("--hurdle", default=None)
    export.add_argument("--repo-url", default=None, dest="repo_url")
    export.set_defaults(func=cmd_dashboard_export)

    sync = sub.add_parser("dashboard-sync", help="push proposals and pull accept/decline decisions")
    sync.add_argument("--url", default=None, help="dashboard base URL (or RHCA_DASHBOARD_URL)")
    sync.add_argument("--token", default=None, help="bearer token (or RHCA_DASHBOARD_TOKEN)")
    sync.add_argument("--limit", type=int, default=None)
    sync.add_argument("--horizon", type=int, default=None)
    sync.add_argument("--hurdle", default=None)
    sync.add_argument("--repo-url", default=None, dest="repo_url")
    sync.set_defaults(func=cmd_dashboard_sync)

    validate = sub.add_parser("validate-order", help="check an order payload offline")
    validate.add_argument("--file", "-f", default="-")
    validate.set_defaults(func=cmd_validate_order)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    # Load .env before any command reads the config: RHCA_RHS_ACCOUNT_NUMBER
    # lives there, and plan-order and approve need it as much as run does.
    # keygen is the exception -- it writes .env, and reading its old key back
    # into the environment would make it warn that the key is set twice.
    args.dotenv_loaded = [] if args.func is cmd_keygen else load_dotenv(_env_file(args))
    try:
        return int(args.func(args))
    except AgentError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except BrokenPipeError:  # pragma: no cover - piping into head etc.
        return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
