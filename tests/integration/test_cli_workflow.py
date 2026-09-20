"""End-to-end CLI tests over the whole ingest -> analyze -> approve -> record loop.

These run entirely offline against JSON fixtures shaped like real MCP responses.
That is the architectural claim being tested: the decision path, the risk
limits, and the audit trail are all exercisable without an account.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
import yaml

from robinhood_crypto_agent.audit import AuditLog
from robinhood_crypto_agent.cli import EXIT_BLOCKED, EXIT_ERROR, EXIT_OK, main
from robinhood_crypto_agent.execution.kill_switch import KillSwitch


def envelope(results, **extra):
    return {"data": {"results": results, **extra}, "guide": "ignored"}


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """A config dir, a data dir, and fixture files shaped like MCP responses."""
    monkeypatch.setenv("RHCA_RHS_ACCOUNT_NUMBER", "123456789")
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "agent.yaml").write_text(
        yaml.safe_dump(
            {"execution_mode": "propose_only", "watchlist": ["BTC-USD", "ETH-USD"]}
        )
    )
    (config_dir / "risk_limits.yaml").write_text(
        yaml.safe_dump(
            {
                "limits": {
                    "max_notional_per_trade_usd": 250,
                    "max_daily_notional_usd": 1000,
                    "max_daily_loss_usd": 200,
                    "min_abs_score": 0.15,
                    "max_spread_pct": 0.75,
                }
            }
        )
    )
    data_dir = tmp_path / "data"

    now = datetime.now(timezone.utc)
    price = 60000.0
    bars = []
    for index in range(80):
        start = now - timedelta(hours=80 - index)
        close = price * 1.0035
        bars.append(
            {
                "start": start.isoformat(),
                "open": f"{price:.2f}",
                "high": f"{max(price, close) * 1.002:.2f}",
                "low": f"{min(price, close) * 0.998:.2f}",
                "close": f"{close:.2f}",
            }
        )
        price = close

    files = {
        "bars.json": bars,
        "quotes.json": envelope(
            [
                {
                    "symbol": "BTCUSD",
                    "bid_price": f"{price * 0.999:.2f}",
                    "ask_price": f"{price * 1.001:.2f}",
                    "mark_price": f"{price:.2f}",
                    "open_price": f"{price * 0.98:.2f}",
                    "updated_at": now.isoformat(),
                }
            ]
        ),
        "wide_quotes.json": envelope(
            [
                {
                    "symbol": "BTCUSD",
                    "bid_price": f"{price * 0.985:.2f}",
                    "ask_price": f"{price * 1.015:.2f}",
                    "mark_price": f"{price:.2f}",
                    "updated_at": now.isoformat(),
                }
            ]
        ),
        "moved_quotes.json": envelope(
            [
                {
                    "symbol": "BTCUSD",
                    "bid_price": f"{price * 1.029:.2f}",
                    "ask_price": f"{price * 1.031:.2f}",
                    "mark_price": f"{price * 1.03:.2f}",
                    "updated_at": now.isoformat(),
                }
            ]
        ),
        "pairs.json": envelope(
            [
                {
                    "id": "pair-1",
                    "symbol": "BTC-USD",
                    "tradability": "tradable",
                    "min_order_size": "0.000001",
                    "max_order_size": "100",
                    "min_order_quantity_increment": "0.00000001",
                    "min_order_price_increment": "0.01",
                    "market_orders_only": False,
                    "halted": False,
                    "halted_regions": [],
                }
            ]
        ),
        "accounts.json": envelope(
            [{"account_number": "RH12345678", "rhs_account_number": "123456789"}]
        ),
        "portfolio.json": envelope([{"total_market_value": "12500.00"}]),
    }
    for name, payload in files.items():
        (tmp_path / name).write_text(json.dumps(payload))

    class Workspace:
        def __init__(self):
            self.root = tmp_path
            self.config_dir = config_dir
            self.data_dir = data_dir
            self.reference_price = price

        def run(self, *args):
            return main(
                ["--config-dir", str(config_dir), "--data-dir", str(data_dir), *args]
            )

        def path(self, name):
            return str(tmp_path / name)

        def bootstrap(self):
            assert self.run("ingest", "accounts", "-f", self.path("accounts.json")) == EXIT_OK
            assert self.run("ingest", "pairs", "-f", self.path("pairs.json")) == EXIT_OK
            assert self.run("ingest", "portfolio", "-f", self.path("portfolio.json")) == EXIT_OK
            assert self.run("ingest", "quotes", "-f", self.path("quotes.json")) == EXIT_OK
            assert self.run("import-history", "BTC-USD", "-f", self.path("bars.json")) == EXIT_OK

        def first_proposal_id(self, capsys):
            capsys.readouterr()
            self.run("analyze", "--json")
            payload = json.loads(capsys.readouterr().out)
            assert payload["proposals"], "expected at least one proposal"
            return payload["proposals"][0]["proposal_id"]

        @property
        def audit(self):
            return AuditLog(self.data_dir / "audit_log.jsonl")

    return Workspace()


def test_status_on_an_empty_workspace_reports_no_history(workspace, capsys):
    assert workspace.run("status") == EXIT_OK
    out = capsys.readouterr().out
    assert "propose_only" in out
    assert "kill switch: released" in out
    assert "no observations recorded" in out


def test_analyze_without_quotes_fails_with_guidance(workspace, capsys):
    assert workspace.run("analyze") == EXIT_ERROR
    assert "ingest quotes" in capsys.readouterr().err


def test_analyze_explains_symbols_it_skipped(workspace, capsys):
    workspace.bootstrap()
    workspace.run("analyze")
    out = capsys.readouterr().out
    assert "NO PROPOSAL" in out
    assert "ETH-USD" in out and "no live quote" in out


def test_full_happy_path(workspace, capsys):
    workspace.bootstrap()
    proposal_id = workspace.first_proposal_id(capsys)

    assert workspace.run("plan-order", proposal_id) == EXIT_OK
    plan_output = capsys.readouterr().out
    assert "preview_crypto_order" in plan_output
    assert '"rhs_account_number": "123456789"' in plan_output

    assert (
        workspace.run(
            "approve",
            proposal_id,
            "--approval",
            f"approved, execute {proposal_id}",
            "--quote",
            workspace.path("quotes.json"),
        )
        == EXIT_OK
    )
    approve_output = capsys.readouterr().out
    assert "place_crypto_order" in approve_output
    assert '"ref_id"' in approve_output

    payload = json.loads(
        approve_output[approve_output.index("{") : approve_output.rindex("}") + 1]
    )
    response = envelope(
        [
            {
                "id": "ord-1",
                "symbol": "BTC-USD",
                "side": payload["side"],
                "state": "filled",
                "quantity": payload["quantity"],
                "average_price": payload["limit_price"],
                "ref_id": payload["ref_id"],
                "executions": [{"quantity": payload["quantity"]}],
            }
        ]
    )
    (workspace.root / "response.json").write_text(json.dumps(response))

    assert (
        workspace.run(
            "record-execution", proposal_id, "--tranche", "0", "-f", workspace.path("response.json")
        )
        == EXIT_OK
    )
    assert "filled" in capsys.readouterr().out

    activity = workspace.audit.daily_activity()
    assert activity.execution_count == 1
    assert activity.executed_notional > Decimal("0")


def test_vague_approval_is_refused(workspace, capsys):
    workspace.bootstrap()
    proposal_id = workspace.first_proposal_id(capsys)
    assert (
        workspace.run(
            "approve", proposal_id, "--approval", "looks good to me",
            "--quote", workspace.path("quotes.json"),
        )
        == EXIT_ERROR
    )
    assert "is not approval" in capsys.readouterr().err


def test_price_drift_refuses_a_stale_proposal(workspace, capsys):
    workspace.bootstrap()
    proposal_id = workspace.first_proposal_id(capsys)
    assert (
        workspace.run(
            "approve", proposal_id, "--approval", f"execute {proposal_id}",
            "--quote", workspace.path("moved_quotes.json"),
        )
        == EXIT_ERROR
    )
    assert "price has moved" in capsys.readouterr().err


def test_kill_switch_blocks_approval_then_releases(workspace, capsys):
    workspace.bootstrap()
    proposal_id = workspace.first_proposal_id(capsys)

    assert workspace.run("kill-switch", "on", "--reason", "testing") == EXIT_OK
    assert (
        workspace.run(
            "approve", proposal_id, "--approval", f"execute {proposal_id}",
            "--quote", workspace.path("quotes.json"),
        )
        == EXIT_ERROR
    )
    assert "ENGAGED" in capsys.readouterr().err

    assert workspace.run("kill-switch", "off") == EXIT_OK
    capsys.readouterr()
    assert (
        workspace.run(
            "approve", proposal_id, "--approval", f"execute {proposal_id}",
            "--quote", workspace.path("quotes.json"),
        )
        == EXIT_OK
    )


def test_wide_spread_blocks_the_proposal(workspace, capsys):
    """A 3% market-maker spread must not become a tradable proposal."""
    workspace.bootstrap()
    assert workspace.run("ingest", "quotes", "-f", workspace.path("wide_quotes.json")) == EXIT_OK
    capsys.readouterr()
    assert workspace.run("analyze") == EXIT_BLOCKED
    out = capsys.readouterr().out
    assert "[FAIL] spread" in out
    assert "BLOCKED BY RISK" in out


def test_daily_loss_cap_auto_engages_the_kill_switch(workspace, capsys):
    workspace.bootstrap()
    assert workspace.run("record-pnl", "--amount", "-250") == EXIT_OK
    assert "kill switch engaged automatically" in capsys.readouterr().out
    assert KillSwitch(workspace.data_dir / "KILL_SWITCH").engaged


def test_risk_rejected_proposals_are_still_logged(workspace, capsys):
    """The record of what was blocked is the evidence the controls work."""
    workspace.bootstrap()
    workspace.run("ingest", "quotes", "-f", workspace.path("wide_quotes.json"))
    capsys.readouterr()
    workspace.run("analyze")
    proposals = [
        event for event in workspace.audit.events(kind="proposal")
        if not event["risk_passed"]
    ]
    assert proposals
    assert "spread" in proposals[-1]["risk_failures"]


def test_audit_command_reports_a_proposal_and_its_fills(workspace, capsys):
    workspace.bootstrap()
    proposal_id = workspace.first_proposal_id(capsys)
    assert workspace.run("audit", "--proposal-id", proposal_id) == EXIT_OK
    out = capsys.readouterr().out
    assert proposal_id in out
    assert "0 execution(s) recorded" in out


def test_unknown_proposal_id_is_reported(workspace, capsys):
    workspace.bootstrap()
    assert workspace.run("plan-order", "deadbeefcafe") == EXIT_ERROR
    assert "no proposal deadbeefcafe" in capsys.readouterr().err


def test_validate_order_accepts_and_rejects(workspace, capsys, tmp_path):
    good = tmp_path / "good.json"
    good.write_text(
        json.dumps(
            {
                "rhs_account_number": "123456789",
                "symbol": "BTC-USD",
                "side": "buy",
                "type": "market",
                "dollar_amount": "100",
            }
        )
    )
    assert workspace.run("validate-order", "-f", str(good)) == EXIT_OK
    assert "valid" in capsys.readouterr().out

    bad = tmp_path / "bad.json"
    bad.write_text(
        json.dumps(
            {
                "rhs_account_number": "RH123",
                "symbol": "BTC-USD",
                "side": "buy",
                "type": "market",
                "dollar_amount": "100",
            }
        )
    )
    assert workspace.run("validate-order", "-f", str(bad)) == EXIT_ERROR
    assert "numeric account number" in capsys.readouterr().err


def test_describe_tools_flags_the_mutating_ones(workspace, capsys):
    assert workspace.run("describe-tools") == EXIT_OK
    out = capsys.readouterr().out
    assert "!! MUTATING  place_crypto_order" in out
    assert "read-only  get_crypto_quotes" in out


def test_malformed_json_input_is_reported(workspace, capsys, tmp_path):
    broken = tmp_path / "broken.json"
    broken.write_text("{not json")
    assert workspace.run("ingest", "quotes", "-f", str(broken)) == EXIT_ERROR
    assert "not valid JSON" in capsys.readouterr().err


def test_missing_input_file_is_reported(workspace, capsys):
    assert workspace.run("ingest", "quotes", "-f", "/nope/absent.json") == EXIT_ERROR
    assert "does not exist" in capsys.readouterr().err
