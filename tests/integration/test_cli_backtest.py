from __future__ import annotations

from typer.testing import CliRunner

import robinhood_crypto_agent.cli as cli_module
from tests.fixtures.helpers import make_trending_ohlcv

runner = CliRunner()


def test_backtest_runs_end_to_end_without_network(monkeypatch):
    btc = make_trending_ohlcv(n=150, daily_return=0.01, noise=0.0, seed=11)
    eth = make_trending_ohlcv(n=150, daily_return=0.01, noise=0.0, seed=12)
    btc.index = btc.index.date
    eth.index = eth.index.date
    fixture_candles = {"BTC": btc, "ETH": eth}

    def fake_fetch(self, symbol, start, end):
        return fixture_candles[symbol]

    monkeypatch.setattr(cli_module.PublicMarketDataClient, "fetch_daily_ohlcv", fake_fetch)
    monkeypatch.setattr(cli_module.FearGreedClient, "historical", lambda self, limit=0: None)

    result = runner.invoke(
        cli_module.app,
        [
            "backtest",
            "--symbols",
            "BTC,ETH",
            "--start",
            "2023-03-15",
            "--end",
            "2023-04-15",
            "--num-folds",
            "2",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Backtest report" in result.output
    assert "Aggregate" in result.output


def test_backtest_writes_trade_csv_when_requested(tmp_path, monkeypatch):
    btc = make_trending_ohlcv(n=150, daily_return=0.01, noise=0.0, seed=11)
    btc.index = btc.index.date

    monkeypatch.setattr(
        cli_module.PublicMarketDataClient, "fetch_daily_ohlcv", lambda self, symbol, start, end: btc
    )
    monkeypatch.setattr(cli_module.FearGreedClient, "historical", lambda self, limit=0: None)

    csv_path = tmp_path / "trades.csv"
    result = runner.invoke(
        cli_module.app,
        [
            "backtest",
            "--symbols",
            "BTC",
            "--start",
            "2023-03-15",
            "--end",
            "2023-04-15",
            "--num-folds",
            "1",
            "--trades-csv",
            str(csv_path),
        ],
    )

    assert result.exit_code == 0, result.output
    assert csv_path.exists()


def test_backtest_fails_gracefully_when_data_unavailable(monkeypatch):
    def raise_error(self, symbol, start, end):
        raise RuntimeError("simulated network failure")

    monkeypatch.setattr(cli_module.PublicMarketDataClient, "fetch_daily_ohlcv", raise_error)

    result = runner.invoke(
        cli_module.app,
        ["backtest", "--symbols", "BTC", "--start", "2023-03-15", "--end", "2023-04-15"],
    )
    assert result.exit_code != 0
