"""``rhca report``: the forward test summarized from the log alone.

What these pin down: the equity curve, paper P&L and max drawdown are read
from the recorded ``shadow_day`` equities (no price fetch); the sparkline is
ASCII and survives a flat series; proposals are counted from the start date and
their risk-pass rate is grouped by coin; and an empty log says so.
"""

from datetime import datetime, timezone
from decimal import Decimal

from robinhood_crypto_agent import summary as sm
from robinhood_crypto_agent.audit import KIND_PROPOSAL, KIND_SHADOW_DAY, AuditLog
from robinhood_crypto_agent.cli import EXIT_OK, main
from robinhood_crypto_agent.config import AgentConfig, RiskLimits, ShadowConfig

D = Decimal
START = datetime(2026, 9, 28, tzinfo=timezone.utc)


def _config(tmp_path):
    return AgentConfig(
        watchlist=("BTC-USD",),
        data_dir=tmp_path / "data",
        risk=RiskLimits(min_notional_per_trade_usd=D(5)),
        shadow=ShadowConfig(start=START, symbols=("BTC-USD", "ETH-USD")),
    )


def _setup(config):
    from robinhood_crypto_agent import shadow as sh

    return sh.setup(config)


def _day(audit, config, day, split_equity, *, fills=0):
    audit.append(
        KIND_SHADOW_DAY,
        {
            "day": day,
            "setup": _setup(config),
            "fills": [{"symbol": "BTC-USD"}] * fills,
            "accounts": {
                "long-term": {"capital": "250.00", "equity": "250.00"},
                "short-term": {"capital": "250.00", "equity": "250.00"},
                "split": {"capital": "500.00", "equity": split_equity},
                "hold": {"capital": "500.00", "equity": "480.00"},
            },
        },
    )


def test_sparkline_is_ascii_and_flat_lines_are_flat():
    assert sm.sparkline([D(5), D(5), D(5)]) == "---"
    curve = sm.sparkline([D(1), D(2), D(4)])
    assert curve[0] == sm.RAMP[0] and curve[-1] == sm.RAMP[-1]
    assert all(ord(c) < 128 for c in curve)  # survives cp1252


def test_max_drawdown_is_peak_to_trough():
    # 100 -> 120 (peak) -> 90: fall of 30 from 120 = 25%.
    assert sm.max_drawdown_pct([D(100), D(120), D(90), D(110)]) == D(25)
    assert sm.max_drawdown_pct([D(500), D(500)]) == D(0)


def test_report_reads_equity_pnl_and_drawdown_from_the_log(tmp_path):
    config = _config(tmp_path)
    audit = AuditLog(config.audit_path)
    _day(audit, config, "2026-09-28", "500.00")
    _day(audit, config, "2026-09-29", "550.00")
    _day(audit, config, "2026-09-30", "495.00", fills=2)

    out = sm.render(config, audit)
    assert "closes recorded : 3" in out
    assert "paper fills     : 2" in out
    assert "$495.00" in out  # final split equity
    assert "-$5.00" in out  # paper P&L vs $500 capital
    assert "10.0%  (split equity)" in out  # 550 -> 495 drawdown


def test_proposals_are_counted_from_start_and_rated_by_coin(tmp_path):
    config = _config(tmp_path)
    audit = AuditLog(config.audit_path)
    _day(audit, config, "2026-09-28", "500.00")
    # One before the start (ignored by the count), two after.
    audit.append(KIND_PROPOSAL, {"recorded_at": "2026-09-20T00:00:00+00:00",
                                 "symbol": "BTC-USD", "risk_passed": False})
    audit.append(KIND_PROPOSAL, {"recorded_at": "2026-09-29T00:00:00+00:00",
                                 "symbol": "BTC-USD", "risk_passed": True})
    audit.append(KIND_PROPOSAL, {"recorded_at": "2026-09-30T00:00:00+00:00",
                                 "symbol": "BTC-USD", "risk_passed": False})

    out = sm.render(config, audit)
    assert "proposals since 2026-09-28 : 2" in out
    assert "BTC-USD" in out and "1/2" in out  # one of two passed


def test_an_empty_log_says_so(tmp_path):
    config = _config(tmp_path)
    out = sm.render(config, AuditLog(config.audit_path))
    assert "No close recorded yet" in out


def test_the_command_runs(tmp_path, capsys):
    config = _config(tmp_path)
    _day(AuditLog(config.audit_path), config, "2026-09-28", "500.00")
    assert main(["--data-dir", str(config.data_dir), "report"]) == EXIT_OK
    assert "FORWARD TEST SUMMARY" in capsys.readouterr().out
