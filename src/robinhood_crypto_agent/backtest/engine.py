from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date as date_type

import pandas as pd

from robinhood_crypto_agent.backtest.costs import CostModel
from robinhood_crypto_agent.backtest.metrics import BacktestMetrics, compute_metrics
from robinhood_crypto_agent.models import Side
from robinhood_crypto_agent.risk.limits import RiskLimits
from robinhood_crypto_agent.sizing.volatility_scaled import size_position
from robinhood_crypto_agent.strategy.base import MarketSnapshot
from robinhood_crypto_agent.strategy.composite import CompositeStrategy

# Bars of history required before a symbol is eligible to trade - must cover
# every signal source's longest lookback (trend.py's SMA50 is the longest).
MIN_LOOKBACK_DAYS = 60


@dataclass
class OpenPosition:
    symbol: str
    qty: float
    entry_price: float
    entry_date: date_type


@dataclass
class ClosedTrade:
    symbol: str
    entry_date: date_type
    exit_date: date_type
    entry_price: float
    exit_price: float
    qty: float
    pnl_usd: float
    exit_reason: str


@dataclass
class FoldResult:
    start: date_type
    end: date_type
    metrics: BacktestMetrics
    equity_curve: pd.Series
    trades: list[ClosedTrade] = field(default_factory=list)


@dataclass
class BacktestResult:
    folds: list[FoldResult]
    aggregate_metrics: BacktestMetrics
    aggregate_equity_curve: pd.Series


def _mark_to_market(
    cash: float, positions: dict[str, OpenPosition], prices_today: dict[str, float]
) -> float:
    value = cash
    for symbol, pos in positions.items():
        value += pos.qty * prices_today.get(symbol, pos.entry_price)
    return value


def run_single_pass(
    candles_by_symbol: dict[str, pd.DataFrame],
    strategy: CompositeStrategy,
    risk_limits: RiskLimits,
    fear_greed: pd.DataFrame | None,
    initial_cash: float,
    cost_model: CostModel,
    holding_period_days: int = 5,
    trade_start_date: date_type | None = None,
) -> tuple[pd.Series, list[ClosedTrade], float]:
    """Simulate one pass over a shared date index, no lookahead.

    Execution model: a signal computed from data available through day t-1
    is acted on at day t's open. A BUY signal opens a new long position (spot
    only - this agent never shorts); a SELL signal on an already-open
    position closes it early; otherwise a position is held for exactly
    `holding_period_days` calendar bars and then closed at that day's open.
    This is a deliberate simplification of real order execution (no partial
    fills, no intraday timing), documented in docs/strategy.md.

    `trade_start_date`, when set, restricts new entries to dates on/after it
    while still using earlier dates in `candles_by_symbol` as indicator
    warm-up/lookback - this is what makes fold-based walk-forward
    evaluation possible without re-fetching data per fold.

    Returns (equity_curve, closed_trades, turnover_usd).
    """
    all_dates = sorted(set().union(*(df.index for df in candles_by_symbol.values())))

    cash = initial_cash
    positions: dict[str, OpenPosition] = {}
    trades: list[ClosedTrade] = []
    equity_curve: dict[date_type, float] = {}
    turnover_usd = 0.0

    for i, current_date in enumerate(all_dates):
        prices_today = {
            symbol: df.loc[current_date, "close"]
            for symbol, df in candles_by_symbol.items()
            if current_date in df.index
        }

        if i < MIN_LOOKBACK_DAYS:
            equity_curve[current_date] = _mark_to_market(cash, positions, prices_today)
            continue

        prev_date = all_dates[i - 1]
        watchlist_history = {
            symbol: df.loc[:prev_date]
            for symbol, df in candles_by_symbol.items()
            if prev_date in df.index and len(df.loc[:prev_date]) >= MIN_LOOKBACK_DAYS
        }
        fng_value = None
        if fear_greed is not None and prev_date in fear_greed.index:
            fng_value = float(fear_greed.loc[prev_date, "value"])

        signals = {}
        for symbol, history in watchlist_history.items():
            snapshot = MarketSnapshot(
                symbol=symbol,
                candles=history,
                watchlist_candles=watchlist_history,
                fear_greed_index=fng_value,
            )
            signals[symbol] = strategy.generate_signal(snapshot)

        # 1) Close positions: early exit on a SELL signal, else time-based.
        for symbol in list(positions.keys()):
            if current_date not in candles_by_symbol[symbol].index:
                continue
            position = positions[symbol]
            today_open = candles_by_symbol[symbol].loc[current_date, "open"]
            signal = signals.get(symbol)
            days_held = (current_date - position.entry_date).days

            should_exit = (signal is not None and signal.side is Side.SELL) or (
                days_held >= holding_period_days
            )
            if not should_exit:
                continue

            exit_price = cost_model.apply(today_open, side_is_buy=False)
            pnl = (exit_price - position.entry_price) * position.qty
            cash += position.qty * exit_price
            turnover_usd += position.qty * exit_price
            trades.append(
                ClosedTrade(
                    symbol=symbol,
                    entry_date=position.entry_date,
                    exit_date=current_date,
                    entry_price=position.entry_price,
                    exit_price=exit_price,
                    qty=position.qty,
                    pnl_usd=pnl,
                    exit_reason="signal" if signal is not None and signal.side is Side.SELL else "holding_period",
                )
            )
            del positions[symbol]

        # 2) Open new positions from a BUY signal, if trading is allowed today.
        entries_allowed = trade_start_date is None or current_date >= trade_start_date
        if entries_allowed and len(positions) < risk_limits.max_open_positions:
            for symbol, history in watchlist_history.items():
                if len(positions) >= risk_limits.max_open_positions:
                    break
                if symbol in positions or current_date not in candles_by_symbol[symbol].index:
                    continue
                signal = signals.get(symbol)
                if signal is None or signal.side is not Side.BUY:
                    continue

                reference_price = float(history["close"].iloc[-1])
                _, quantity = size_position(signal, history, reference_price, risk_limits)
                if quantity <= 0:
                    continue

                today_open = candles_by_symbol[symbol].loc[current_date, "open"]
                entry_price = cost_model.apply(today_open, side_is_buy=True)
                required_cash = quantity * entry_price
                if required_cash > cash:
                    continue

                cash -= required_cash
                turnover_usd += required_cash
                positions[symbol] = OpenPosition(
                    symbol=symbol, qty=quantity, entry_price=entry_price, entry_date=current_date
                )

        equity_curve[current_date] = _mark_to_market(cash, positions, prices_today)

    series = pd.Series(equity_curve).sort_index()
    if trade_start_date is not None:
        series = series[series.index >= trade_start_date]
    return series, trades, turnover_usd


def run_backtest(
    candles_by_symbol: dict[str, pd.DataFrame],
    strategy: CompositeStrategy,
    risk_limits: RiskLimits,
    start: date_type,
    end: date_type,
    fear_greed: pd.DataFrame | None = None,
    initial_cash: float = 10_000.0,
    cost_model: CostModel | None = None,
    holding_period_days: int = 5,
    num_folds: int = 4,
) -> BacktestResult:
    """Walk-forward-style evaluation: splits [start, end] into `num_folds`
    sequential, non-overlapping periods and evaluates the strategy
    independently in each (fresh capital per fold), so a strategy that only
    "worked" in one stretch of history doesn't hide behind a single
    blended number. There are no strategy parameters being fit/optimized
    per fold in this version - see docs/strategy.md for what this Phase 1
    backtest does and doesn't validate.
    """
    cost_model = cost_model or CostModel()

    total_days = (end - start).days
    if total_days <= 0:
        raise ValueError("`end` must be after `start`")
    fold_length = max(1, total_days // num_folds)

    folds: list[FoldResult] = []
    fold_start = start
    for fold_index in range(num_folds):
        is_last = fold_index == num_folds - 1
        fold_end = end if is_last else min(end, _add_days(fold_start, fold_length))

        equity_curve, trades, turnover_usd = run_single_pass(
            candles_by_symbol=candles_by_symbol,
            strategy=strategy,
            risk_limits=risk_limits,
            fear_greed=fear_greed,
            initial_cash=initial_cash,
            cost_model=cost_model,
            holding_period_days=holding_period_days,
            trade_start_date=fold_start,
        )
        fold_equity = equity_curve[equity_curve.index <= fold_end]
        fold_trades = [t for t in trades if fold_start <= t.entry_date <= fold_end]
        metrics = compute_metrics(
            fold_equity, [t.pnl_usd for t in fold_trades], turnover_usd
        )
        folds.append(
            FoldResult(start=fold_start, end=fold_end, metrics=metrics, equity_curve=fold_equity, trades=fold_trades)
        )
        fold_start = _add_days(fold_end, 1)
        if fold_start > end:
            break

    all_trade_pnls = [t.pnl_usd for fold in folds for t in fold.trades]
    total_turnover = sum(
        fold.metrics.turnover_usd for fold in folds
    )
    # Chain each fold's normalized (start=1.0) equity curve to report a
    # single stitched curve across the whole tested period.
    chained = pd.Series(dtype=float)
    multiplier = 1.0
    for fold in folds:
        if len(fold.equity_curve) == 0:
            continue
        normalized = fold.equity_curve / fold.equity_curve.iloc[0] * multiplier
        chained = pd.concat([chained, normalized])
        multiplier = normalized.iloc[-1]

    aggregate_metrics = compute_metrics(chained, all_trade_pnls, total_turnover)
    return BacktestResult(folds=folds, aggregate_metrics=aggregate_metrics, aggregate_equity_curve=chained)


def _add_days(d: date_type, days: int) -> date_type:
    from datetime import timedelta

    return d + timedelta(days=days)
