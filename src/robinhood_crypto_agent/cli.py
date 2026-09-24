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

**``rhca run``, in shadow mode.** The real-time loop reads quotes and holdings
from Robinhood's Crypto API with a read-only client, labels news with Jev and
escalates strong candidates to Claude Sonnet 5 (see ``runner``). It proposes;
it cannot order. Its proposals go through the same ``rhca approve`` gate.
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

from . import dashboard as dashboard_mod
from . import reports
from . import runner as runner_mod
from .agent import Agent, MarketState
from .audit import AuditLog, day_from
from .bootstrap import fetch_coinbase_candles
from .config import AgentConfig, load_config
from .errors import AgentError
from .execution.gate import ApprovalGate
from .execution.kill_switch import REASON_DAILY_LOSS, REASON_MANUAL, KillSwitch
from .execution.orders import build_plan_requests
from .jev import API_KEY_ENV as JEV_KEY_ENV
from .jev import JevClient
from .mcp.contract import CRYPTO_TOOLS, TOOL_CONTRACTS, validate_crypto_order_args
from .mcp.parse import (
    parse_accounts,
    parse_currency_pairs,
    parse_order_response,
    parse_positions,
    parse_quotes,
    unwrap_results,
)
from .models import Candle, parse_timestamp
from .numeric import format_decimal, round_money, to_decimal
from .outcomes import DEFAULT_HORIZON_BARS, DEFAULT_HURDLE_PCT
from .robinhood import API_KEY_ENV as ROBINHOOD_KEY_ENV
from .robinhood import PRIVATE_KEY_ENV as ROBINHOOD_PRIVATE_KEY_ENV
from .robinhood import RobinhoodClient, generate_key_pair
from .serde import proposal_from_dict
from .store import PriceStore, StateCache
from .symbols import canonical
from .system2 import System2

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_BLOCKED = 2

#: The crypto account ``rhca run`` must read -- the full number or its last 4
#: digits. A Robinhood API key belongs to one account, and a key made on the
#: wrong one would otherwise run silently against the wrong money.
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
            f"but {CRYPTO_ACCOUNT_ENV} pins {mask(expected)}. Create the API key under the "
            f"pinned account (docs/runbook.md, 'Keys'), or correct {CRYPTO_ACCOUNT_ENV}."
        )


#: Every credential ``rhca run`` reads. Only names are ever printed.
CREDENTIAL_ENVS = (
    ROBINHOOD_KEY_ENV,
    ROBINHOOD_PRIVATE_KEY_ENV,
    JEV_KEY_ENV,
    "ANTHROPIC_API_KEY",
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

    load_dotenv(_env_file(args))
    for line in runner_mod.describe_heartbeat(
        runner_mod.read_json(config.heartbeat_path),
        stale_after_seconds=runner_mod.stale_after_seconds(config),
    ):
        print(line)
    present = [name for name in CREDENTIAL_ENVS if os.environ.get(name)]
    missing = [name for name in CREDENTIAL_ENVS if not os.environ.get(name)]
    print(f"  keys set     : {', '.join(present) or 'none'}")
    if missing:
        print(f"  keys missing : {', '.join(missing)}")
    cached = state.account()
    if cached is not None:
        expected = os.environ.get(CRYPTO_ACCOUNT_ENV, "").strip()
        if not expected:
            verdict = f"not pinned -- set {CRYPTO_ACCOUNT_ENV}"
        elif cached.account_number.endswith(expected):
            verdict = "matches the pin"
        else:
            verdict = f"DOES NOT MATCH the pinned {mask(expected)}"
        print(f"  robinhood    : last read crypto account {mask(cached.account_number)} ({verdict})")
    print()

    coverages = [
        store.coverage(
            symbol,
            interval_minutes=config.strategy.bar_interval_minutes,
            required_bars=config.strategy.min_bars,
        )
        for symbol in config.watchlist
    ]
    print(reports.render_coverage(coverages))
    return EXIT_OK


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
        account = accounts[0]
        if len(accounts) > 1 and args.rhs_account_number:
            matching = [
                a for a in accounts if a.rhs_account_number == args.rhs_account_number
            ]
            if matching:
                account = matching[0]
        state.put_account(account)
        print(f"cached account {account.account_number}")
        print(f"  rhs_account_number (for order tools): {account.rhs_account_number}")
        if len(accounts) > 1:
            print(
                f"  note: {len(accounts)} accounts returned; cached the first. "
                "Pass --rhs-account-number to pick a specific one."
            )
        return EXIT_OK

    if kind == "portfolio":
        rows = unwrap_results(payload)
        if not rows:
            raise AgentError("no portfolio data in the payload")
        row = rows[0]
        value = None
        for key in (
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
                "total_market_value, market_value, equity, total_equity, portfolio_value."
            )
        state.put_portfolio_value(value)
        print(f"cached portfolio value ${round_money(value)}")
        return EXIT_OK

    raise AgentError(f"unknown ingest kind: {kind}")


def cmd_import_history(args: argparse.Namespace) -> int:
    config, state, store, audit = _context(args)
    payload = _read_json(args.file)
    rows = payload if isinstance(payload, list) else unwrap_results(payload)
    symbol = canonical(args.symbol)

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
            else start + timedelta(minutes=args.interval)
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
                observations=1,
            )
        )

    if not candles:
        raise AgentError(
            "no usable bars found. Expected objects with start/open/high/low/close "
            "(or begins_at/open_price/high_price/low_price/close_price)."
        )

    written = store.import_candles(candles)
    print(
        f"imported {len(candles)} bar(s) for {symbol} as {written} synthetic "
        "observation(s), marked source=import"
    )
    coverage = store.coverage(
        symbol,
        interval_minutes=config.strategy.bar_interval_minutes,
        required_bars=config.strategy.min_bars,
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
                        if o.proposal is None
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


def cmd_bootstrap_history(args: argparse.Namespace) -> int:
    config, state, store, audit = _context(args)
    interval = config.strategy.bar_interval_minutes
    for symbol in config.watchlist:
        candles = fetch_coinbase_candles(symbol, interval_minutes=interval)
        existing = {
            c.start
            for c in store.candles(symbol, interval_minutes=interval, include_partial=True)
        }
        missing = [c for c in candles if c.start not in existing]
        store.import_candles(missing)
        coverage = store.coverage(
            symbol, interval_minutes=interval, required_bars=config.strategy.min_bars
        )
        print(f"{symbol}: imported {len(missing)} bar(s) from Coinbase. {coverage.describe()}")
    print(
        "\nImported bars are marked source=import and feed only the indicators; proposals "
        "are always priced off a live Robinhood quote."
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
    loaded = load_dotenv(_env_file(args))
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

    system2 = None
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        import anthropic  # only the live loop needs the SDK

        fetch_quote, fetch_holdings = runner_mod.system2_tools(robinhood)
        system2 = System2(
            anthropic.Anthropic(),
            fetch_quote=fetch_quote,
            fetch_holdings=fetch_holdings,
            model=config.pipeline.system2_model,
            bar_minutes=config.strategy.bar_interval_minutes,
            market_data_url=config.pipeline.market_data_mcp_url or None,
        )

    dashboard_url = os.environ.get("RHCA_DASHBOARD_URL")
    dashboard_token = os.environ.get("RHCA_DASHBOARD_TOKEN")
    services = runner_mod.Services(
        robinhood=robinhood,
        jev=JevClient.from_env(),
        system2=system2,
        dashboard=(dashboard_url, dashboard_token) if dashboard_url and dashboard_token else None,
    )

    # Headlines carry curly quotes and emoji; a legacy Windows console encoding
    # would otherwise turn one into a logging traceback.
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
    buying_power = (
        f"${round_money(account.buying_power)}" if account.buying_power is not None else "unknown"
    )
    pin = "pinned" if expected_account else f"NOT pinned -- set {CRYPTO_ACCOUNT_ENV}"
    print(
        f"  robinhood account: crypto {mask(account.account_number)}, "
        f"buying power {buying_power} ({pin})"
    )
    print(f"  watchlist        : {', '.join(config.watchlist)}")
    print("  System 1         : indicators + news signal + 16 risk rules")
    print(f"  Jev news labels  : {'on' if services.jev else f'OFF (set {JEV_KEY_ENV})'}")
    system2_state = config.pipeline.system2_model if system2 else "OFF (set ANTHROPIC_API_KEY)"
    print(f"  System 2         : {system2_state}")
    market_data = config.pipeline.market_data_mcp_url if system2 else ""
    print(f"  market data      : {market_data or 'off'} (System 2's MCP connector)")
    print(f"  dashboard sync   : {'on' if services.dashboard else 'off'}")
    if args.keep_awake:
        print(f"  keep awake       : {'on' if _keep_awake() else 'unavailable on this OS'}")
    print("Ctrl+C to stop.\n")

    runner = runner_mod.Runner(config, services)
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
        "run", help="the real-time loop in shadow mode: poll, score, escalate, record"
    )
    run.add_argument("--once", action="store_true", help="one cycle of every task, then exit")
    run.add_argument("--minutes", type=float, default=None, help="stop after this long")
    run.add_argument(
        "--keep-awake", action="store_true", help="stop Windows sleeping while it runs"
    )
    run.set_defaults(func=cmd_run)

    bootstrap = sub.add_parser(
        "bootstrap-history", help="import recent bars from Coinbase so indicators work at once"
    )
    bootstrap.set_defaults(func=cmd_bootstrap_history)

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
    try:
        return int(args.func(args))
    except AgentError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except BrokenPipeError:  # pragma: no cover - piping into head etc.
        return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
