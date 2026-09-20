from __future__ import annotations

import json

from typer.testing import CliRunner

import robinhood_crypto_agent.cli as cli_module
from robinhood_crypto_agent.audit.log import AuditLog
from tests.fixtures.helpers import make_flat_ohlcv, make_trending_ohlcv

runner = CliRunner()


def _write_candles_json(path, watchlist_dfs, fear_greed_index=None):
    payload = {"candles": {}}
    if fear_greed_index is not None:
        payload["fear_greed_index"] = fear_greed_index
    for symbol, df in watchlist_dfs.items():
        records = [
            {
                "timestamp": ts.isoformat(),
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "volume": float(row["volume"]),
            }
            for ts, row in df.iterrows()
        ]
        payload["candles"][symbol] = records
    path.write_text(json.dumps(payload))


def test_analyze_generates_a_proposal_and_logs_every_candidate(tmp_path, monkeypatch):
    audit_log = AuditLog(tmp_path / "audit_log.jsonl")
    monkeypatch.setattr(cli_module, "AuditLog", lambda: audit_log)

    btc = make_trending_ohlcv(n=90, daily_return=0.03, noise=0.0, start_price=100, seed=7)
    eth = make_flat_ohlcv(n=90, price=50.0)
    candles_path = tmp_path / "candles.json"
    _write_candles_json(candles_path, {"BTC": btc, "ETH": eth})

    result = runner.invoke(
        cli_module.app, ["analyze", str(candles_path), "--data-source", "mcp-json"]
    )

    assert result.exit_code == 0, result.output
    assert "BTC" in result.output

    entries = audit_log.read_entries()
    assert any(e["type"] == "proposal" for e in entries)
    proposal_entry = next(e for e in entries if e["type"] == "proposal")
    assert proposal_entry["proposal"]["symbol"] == "BTC"
    assert proposal_entry["proposal"]["risk_check"]["passed"] is True


def test_analyze_requires_input_file_for_mcp_json(tmp_path, monkeypatch):
    audit_log = AuditLog(tmp_path / "audit_log.jsonl")
    monkeypatch.setattr(cli_module, "AuditLog", lambda: audit_log)

    result = runner.invoke(cli_module.app, ["analyze", "--data-source", "mcp-json"])
    assert result.exit_code != 0


def test_analyze_with_no_signal_reports_none_and_logs_nothing(tmp_path, monkeypatch):
    audit_log = AuditLog(tmp_path / "audit_log.jsonl")
    monkeypatch.setattr(cli_module, "AuditLog", lambda: audit_log)

    flat = make_flat_ohlcv(n=90, price=100.0)
    candles_path = tmp_path / "candles.json"
    _write_candles_json(candles_path, {"BTC": flat})

    result = runner.invoke(
        cli_module.app, ["analyze", str(candles_path), "--data-source", "mcp-json"]
    )

    assert result.exit_code == 0, result.output
    assert "No proposals generated" in result.output
    assert audit_log.read_entries() == []


def test_log_execution_records_from_mcp_response(tmp_path, monkeypatch):
    audit_log = AuditLog(tmp_path / "audit_log.jsonl")
    monkeypatch.setattr(cli_module, "AuditLog", lambda: audit_log)

    btc = make_trending_ohlcv(n=90, daily_return=0.03, noise=0.0, seed=7)
    eth = make_flat_ohlcv(n=90, price=50.0)
    candles_path = tmp_path / "candles.json"
    _write_candles_json(candles_path, {"BTC": btc, "ETH": eth})
    runner.invoke(cli_module.app, ["analyze", str(candles_path), "--data-source", "mcp-json"])

    proposal_entry = next(e for e in audit_log.read_entries() if e["type"] == "proposal")
    proposal_id = proposal_entry["proposal"]["id"]

    mcp_response = {
        "order_id": "abc123",
        "status": "filled",
        "filled_price": 101.0,
        "filled_qty": 0.1,
    }
    response_path = tmp_path / "response.json"
    response_path.write_text(json.dumps(mcp_response))

    result = runner.invoke(
        cli_module.app,
        ["log-execution", "--proposal-id", proposal_id, "--mcp-response", str(response_path)],
    )
    assert result.exit_code == 0, result.output

    executions = [e for e in audit_log.read_entries() if e["type"] == "execution"]
    assert len(executions) == 1
    assert executions[0]["execution"]["mcp_order_id"] == "abc123"
    assert executions[0]["execution"]["symbol"] == "BTC"


def test_log_execution_unknown_proposal_id_fails(tmp_path, monkeypatch):
    audit_log = AuditLog(tmp_path / "audit_log.jsonl")
    monkeypatch.setattr(cli_module, "AuditLog", lambda: audit_log)

    response_path = tmp_path / "response.json"
    response_path.write_text(json.dumps({"status": "filled"}))

    result = runner.invoke(
        cli_module.app,
        ["log-execution", "--proposal-id", "nonexistent", "--mcp-response", str(response_path)],
    )
    assert result.exit_code != 0


def test_status_and_kill_switch_roundtrip(tmp_path, monkeypatch):
    from robinhood_crypto_agent.execution.kill_switch import (
        disengage_kill_switch,
        engage_kill_switch,
    )

    audit_log = AuditLog(tmp_path / "audit_log.jsonl")
    monkeypatch.setattr(cli_module, "AuditLog", lambda: audit_log)

    kill_switch_path = tmp_path / "kill_switch.flag"
    monkeypatch.setattr(
        cli_module, "engage_kill_switch", lambda reason="": engage_kill_switch(kill_switch_path, reason)
    )
    monkeypatch.setattr(
        cli_module, "disengage_kill_switch", lambda: disengage_kill_switch(kill_switch_path)
    )
    monkeypatch.setattr(
        "robinhood_crypto_agent.audit.log.is_kill_switch_engaged",
        lambda: kill_switch_path.exists(),
    )

    result = runner.invoke(cli_module.app, ["status"])
    assert result.exit_code == 0
    assert "kill_switch_engaged: False" in result.output

    result = runner.invoke(cli_module.app, ["kill-switch", "on"])
    assert result.exit_code == 0

    result = runner.invoke(cli_module.app, ["status"])
    assert "kill_switch_engaged: True" in result.output

    result = runner.invoke(cli_module.app, ["kill-switch", "off"])
    assert result.exit_code == 0

    result = runner.invoke(cli_module.app, ["status"])
    assert "kill_switch_engaged: False" in result.output
