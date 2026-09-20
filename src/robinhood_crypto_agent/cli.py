from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd
import typer

from robinhood_crypto_agent.audit.log import AuditLog
from robinhood_crypto_agent.backtest.costs import CostModel
from robinhood_crypto_agent.backtest.engine import run_backtest
from robinhood_crypto_agent.backtest.report import format_report, write_trade_csv
from robinhood_crypto_agent.config import load_config
from robinhood_crypto_agent.data.external.feargreed import FearGreedClient
from robinhood_crypto_agent.data.public_market_data import PublicMarketDataClient
from robinhood_crypto_agent.execution.kill_switch import disengage_kill_switch, engage_kill_switch
from robinhood_crypto_agent.models import (
    ExecutionRecord,
    ProposalStatus,
    RiskCheckResult,
    Side,
    TradeProposal,
)
from robinhood_crypto_agent.reports.proposal_report import format_proposal_report
from robinhood_crypto_agent.risk.engine import RiskEngine
from robinhood_crypto_agent.sizing.volatility_scaled import size_position
from robinhood_crypto_agent.strategy.base import MarketSnapshot
from robinhood_crypto_agent.strategy.composite import CompositeStrategy, default_sources

app = typer.Typer(
    help="Analyze/propose/backtest for the Robinhood crypto agent. "
    "Order submission itself only ever happens via the robinhood-trading "
    "MCP server, called by Claude - never from this CLI."
)

MIN_CANDLES_REQUIRED = 60


def _load_candles_from_json(path: Path) -> tuple[dict[str, pd.DataFrame], float | None]:
    """Parse candles saved from an MCP market-data response.

    Expected shape: `{"candles": {"BTC": [{"timestamp": ..., "open": ...,
    "high": ..., "low": ..., "close": ..., "volume": ...}, ...], ...},
    "fear_greed_index": 42}` - `fear_greed_index` is optional; a bare
    `{"BTC": [...], ...}` (no "candles"/"fear_greed_index" wrapper) is also
    accepted.
    """
    payload = json.loads(path.read_text())
    fear_greed_index = payload.get("fear_greed_index") if "candles" in payload else None
    candles_payload = payload.get("candles", payload)

    watchlist_candles: dict[str, pd.DataFrame] = {}
    for symbol, records in candles_payload.items():
        df = pd.DataFrame(records)
        if "timestamp" in df.columns:
            df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
            df = df.sort_values("timestamp").set_index("timestamp")
        watchlist_candles[symbol] = df[["open", "high", "low", "close", "volume"]].astype(float)
    return watchlist_candles, fear_greed_index


@app.command()
def analyze(
    input_file: Path | None = typer.Argument(
        None, help="JSON file of candles (required for --data-source mcp-json)"
    ),
    data_source: str = typer.Option(
        "public",
        "--data-source",
        help="'mcp-json' (candles pulled live via MCP, saved to a file by Claude) "
        "or 'public' (free historical data, for offline iteration only).",
    ),
    lookback_days: int = typer.Option(
        120, "--lookback-days", help="Only used for --data-source public"
    ),
) -> None:
    """Generate trade proposals: regime detection + composite strategy +
    sizing + risk check, for every symbol with enough history. Every
    candidate is logged (see audit/log.py) regardless of whether its risk
    check passed."""
    config = load_config()
    audit_log = AuditLog()

    if data_source == "mcp-json":
        if input_file is None:
            typer.echo("input_file is required for --data-source mcp-json", err=True)
            raise typer.Exit(code=1)
        watchlist_candles, fear_greed_index = _load_candles_from_json(input_file)
    elif data_source == "public":
        end = datetime.now(UTC)
        start = end - timedelta(days=lookback_days)
        client = PublicMarketDataClient()
        watchlist_candles = {}
        for symbol in config.watchlist:
            try:
                watchlist_candles[symbol] = client.fetch_daily_ohlcv(symbol, start, end)
            except Exception as exc:  # noqa: BLE001 - report and continue with other symbols
                typer.echo(f"Warning: failed to fetch data for {symbol}: {exc}", err=True)
        fear_greed_index = FearGreedClient().current()
    else:
        typer.echo("--data-source must be 'mcp-json' or 'public'", err=True)
        raise typer.Exit(code=1)

    strategy = CompositeStrategy(default_sources(), config.strategy_weights)
    risk_engine = RiskEngine(config.risk_limits)

    proposals: list[TradeProposal] = []
    for symbol, candles in watchlist_candles.items():
        if symbol not in config.watchlist or len(candles) < MIN_CANDLES_REQUIRED:
            continue

        snapshot = MarketSnapshot(
            symbol=symbol,
            candles=candles,
            watchlist_candles=watchlist_candles,
            fear_greed_index=fear_greed_index,
        )
        signal = strategy.generate_signal(snapshot)
        if signal is None:
            continue

        reference_price = float(candles["close"].iloc[-1])
        notional_usd, quantity = size_position(
            signal, candles, reference_price, config.risk_limits
        )
        if quantity <= 0:
            continue

        proposal = TradeProposal(
            symbol=symbol,
            side=signal.side,
            quantity=quantity,
            reference_price=reference_price,
            notional_usd=notional_usd,
            rationale=(
                f"Composite {signal.composite_value:+.2f} "
                f"(confidence {signal.composite_confidence:.2f}) in {signal.regime.value} regime"
            ),
            signal=signal,
            risk_check=RiskCheckResult(passed=True),
        )

        activity = audit_log.read_daily_activity()
        risk_check = risk_engine.evaluate(proposal, activity)
        proposal.risk_check = risk_check
        proposal.status = (
            ProposalStatus.PENDING if risk_check.passed else ProposalStatus.REJECTED
        )

        audit_log.log_proposal(proposal)
        if not risk_check.passed:
            audit_log.log_rejection(proposal, "; ".join(risk_check.violations))

        proposals.append(proposal)

    typer.echo(format_proposal_report(proposals))


@app.command()
def backtest(
    symbols: str = typer.Option(
        "", "--symbols", help="Comma-separated symbols; defaults to the full watchlist"
    ),
    start: str = typer.Option(..., "--start", help="YYYY-MM-DD"),
    end: str = typer.Option(..., "--end", help="YYYY-MM-DD"),
    initial_cash: float = typer.Option(10_000.0, "--initial-cash"),
    holding_period_days: int = typer.Option(5, "--holding-period-days"),
    num_folds: int = typer.Option(4, "--num-folds"),
    trades_csv: Path | None = typer.Option(None, "--trades-csv"),
) -> None:
    """Walk-forward-style validation of the strategy against free historical
    data, with a fee/slippage/spread cost model. See docs/strategy.md for
    what this does and doesn't prove."""
    config = load_config()
    symbol_list = [s.strip().upper() for s in symbols.split(",") if s.strip()] or config.watchlist

    start_date = datetime.strptime(start, "%Y-%m-%d").date()
    end_date = datetime.strptime(end, "%Y-%m-%d").date()

    lookback_buffer_days = 90
    fetch_start = datetime.combine(start_date, datetime.min.time()) - timedelta(
        days=lookback_buffer_days
    )
    fetch_end = datetime.combine(end_date, datetime.min.time())

    client = PublicMarketDataClient()
    candles_by_symbol: dict[str, pd.DataFrame] = {}
    for symbol in symbol_list:
        try:
            candles_by_symbol[symbol] = client.fetch_daily_ohlcv(symbol, fetch_start, fetch_end)
        except Exception as exc:  # noqa: BLE001 - report and continue with other symbols
            typer.echo(f"Warning: failed to fetch data for {symbol}: {exc}", err=True)

    if not candles_by_symbol:
        typer.echo("No historical data available for any requested symbol.", err=True)
        raise typer.Exit(code=1)

    fear_greed_df = None
    try:
        fear_greed_df = FearGreedClient().historical(limit=0)
    except Exception as exc:  # noqa: BLE001 - sentiment is optional, don't fail the backtest
        typer.echo(f"Warning: failed to fetch Fear & Greed history: {exc}", err=True)

    strategy = CompositeStrategy(default_sources(), config.strategy_weights)
    result = run_backtest(
        candles_by_symbol=candles_by_symbol,
        strategy=strategy,
        risk_limits=config.risk_limits,
        start=start_date,
        end=end_date,
        fear_greed=fear_greed_df,
        initial_cash=initial_cash,
        cost_model=CostModel(),
        holding_period_days=holding_period_days,
        num_folds=num_folds,
    )
    typer.echo(format_report(result))
    if trades_csv:
        write_trade_csv(result, trades_csv)
        typer.echo(f"\nWrote trade log to {trades_csv}")


@app.command("log-execution")
def log_execution(
    proposal_id: str = typer.Option(..., "--proposal-id"),
    mcp_response: Path = typer.Option(
        ..., "--mcp-response", exists=True, help="JSON file with the raw MCP order response"
    ),
    override: bool = typer.Option(
        False, "--override", help="Mark this as a risk-check override (see CLAUDE.md)"
    ),
) -> None:
    """Record what an MCP order tool call actually returned. Never
    fabricate this - only real MCP response data belongs here."""
    audit_log = AuditLog()
    proposal = audit_log.find_proposal(proposal_id)
    if proposal is None:
        typer.echo(f"No proposal found with id {proposal_id}", err=True)
        raise typer.Exit(code=1)

    raw = json.loads(mcp_response.read_text())
    record = ExecutionRecord(
        proposal_id=proposal_id,
        symbol=proposal["symbol"],
        side=Side(proposal["side"]),
        mcp_order_id=raw.get("order_id"),
        status=raw.get("status", "unknown"),
        filled_price=raw.get("filled_price"),
        filled_qty=raw.get("filled_qty"),
        realized_pnl_usd=raw.get("realized_pnl_usd"),
        override=override,
        raw_mcp_response=raw,
    )
    audit_log.log_execution(record)
    typer.echo(f"Logged execution for proposal {proposal_id}: status={record.status}")


@app.command("show-audit")
def show_audit(
    date: str | None = typer.Option(None, "--date", help="YYYY-MM-DD"),
    symbol: str | None = typer.Option(None, "--symbol"),
) -> None:
    """Print raw audit log entries, optionally filtered."""
    audit_log = AuditLog()
    for entry in audit_log.read_entries():
        if date and not str(entry.get("logged_at", "")).startswith(date):
            continue
        payload = entry.get("proposal") or entry.get("execution") or {}
        if symbol and payload.get("symbol") != symbol:
            continue
        typer.echo(json.dumps(entry, indent=2, default=str))


@app.command()
def status() -> None:
    """Show execution mode, kill-switch state, and today's risk usage."""
    config = load_config()
    activity = AuditLog().read_daily_activity()

    typer.echo(f"execution_mode: {config.execution_mode.value}")
    typer.echo(f"kill_switch_engaged: {activity.kill_switch_engaged}")
    typer.echo(
        f"today's exposure: ${activity.exposure_usd:.2f} / "
        f"${config.risk_limits.max_daily_exposure_usd:.2f}"
    )
    typer.echo(
        f"today's realized loss: ${activity.realized_loss_usd:.2f} / "
        f"${config.risk_limits.max_daily_loss_usd:.2f}"
    )
    typer.echo(
        f"open positions: {len(activity.open_position_symbols)} / "
        f"{config.risk_limits.max_open_positions} "
        f"{sorted(activity.open_position_symbols)}"
    )


@app.command("kill-switch")
def kill_switch_cmd(
    action: str = typer.Argument(..., help="'on' or 'off'"),
    reason: str = typer.Option("", "--reason"),
) -> None:
    """Manually engage/disengage the kill switch."""
    if action == "on":
        engage_kill_switch(reason=reason)
        typer.echo("Kill switch engaged.")
    elif action == "off":
        disengage_kill_switch()
        typer.echo("Kill switch disengaged.")
    else:
        raise typer.BadParameter("action must be 'on' or 'off'")


if __name__ == "__main__":
    app()
