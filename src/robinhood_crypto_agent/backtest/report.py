from __future__ import annotations

import csv
from pathlib import Path

from robinhood_crypto_agent.backtest.engine import BacktestResult


def format_report(result: BacktestResult) -> str:
    lines = ["Backtest report", "=" * 60]
    for i, fold in enumerate(result.folds, start=1):
        m = fold.metrics
        lines.append(f"\nFold {i}: {fold.start} -> {fold.end} ({len(fold.trades)} trades)")
        lines.append(
            f"  Return: {m.total_return_pct:+.2f}%  Sharpe: {m.sharpe:.2f}  "
            f"Sortino: {m.sortino:.2f}  Max DD: {m.max_drawdown_pct:.2f}%"
        )
        lines.append(
            f"  Win rate: {m.win_rate:.1%}  Avg trade P&L: ${m.avg_trade_pnl_usd:+.2f}  "
            f"Turnover: ${m.turnover_usd:,.2f}"
        )

    agg = result.aggregate_metrics
    lines.append("\n" + "-" * 60)
    lines.append("Aggregate (stitched across folds, costs included):")
    lines.append(
        f"  Return: {agg.total_return_pct:+.2f}%  Sharpe: {agg.sharpe:.2f}  "
        f"Sortino: {agg.sortino:.2f}  Max DD: {agg.max_drawdown_pct:.2f}%"
    )
    lines.append(
        f"  Win rate: {agg.win_rate:.1%}  Trades: {agg.num_trades}  "
        f"Avg trade P&L: ${agg.avg_trade_pnl_usd:+.2f}  Turnover: ${agg.turnover_usd:,.2f}"
    )
    lines.append(
        "\nReminder: this validates the strategy against history under a cost "
        "model. It is not a guarantee of future performance and never "
        "overrides a risk limit or the propose-only approval gate (see "
        "docs/risk-controls.md)."
    )
    return "\n".join(lines)


def write_trade_csv(result: BacktestResult, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "symbol",
                "entry_date",
                "exit_date",
                "entry_price",
                "exit_price",
                "qty",
                "pnl_usd",
                "exit_reason",
            ]
        )
        for fold in result.folds:
            for t in fold.trades:
                writer.writerow(
                    [
                        t.symbol,
                        t.entry_date,
                        t.exit_date,
                        t.entry_price,
                        t.exit_price,
                        t.qty,
                        t.pnl_usd,
                        t.exit_reason,
                    ]
                )
