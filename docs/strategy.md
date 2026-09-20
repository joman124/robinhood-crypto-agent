# Strategy design

## Why not a single technical indicator

An early draft used a single RSI + SMA-trend-filter rule. On its own this has
no real edge — it's one of the most commonly used and arbitraged patterns in
liquid crypto markets, and tends to wash out to noise after fees/slippage.
The design below exists to give the agent an actual, defensible edge instead
of a placeholder.

## Regime detection first

Trend-following and mean-reversion signals perform oppositely depending on
market conditions, so the strategy classifies the regime before weighting
anything (`strategy/regime.py`):

- **TRENDING**: ADX(14) above `adx_trending_threshold` (default 25).
- **RANGING**: otherwise.

## Independent signal sources (`strategy/signals/`)

Each source scores a symbol in `[-1, 1]` (bearish → bullish) with a
`confidence` in `[0, 1]`. Sources deliberately mix price-derived and
non-price-derived inputs so agreement means something beyond "these
indicators are all computed from the same price series":

| Source | What it measures | Input |
|---|---|---|
| `trend.py` | SMA/MACD crossover direction & strength | OHLCV |
| `mean_reversion.py` | RSI extremes (oversold/overbought) | OHLCV |
| `volume.py` | On-balance-volume divergence / volume spikes | OHLCV |
| `relative_strength.py` | Momentum rank of this symbol vs. the rest of the watchlist | OHLCV across watchlist |
| `sentiment.py` | Contrarian score from the crypto Fear & Greed Index | **External**: [Alternative.me Fear & Greed Index](https://alternative.me/crypto/fear-and-greed-index/) (free, no API key) |

`sentiment.py` is the "data from elsewhere" source — it's the only
widely-available, free, no-key crypto sentiment series. On-chain/orderflow
data (Glassnode, CryptoQuant, live order-book depth) is a real future
upgrade but requires paid keys or confirmed MCP support; the `SignalSource`
protocol (`strategy/base.py`) is designed so a new source can be added
without touching `composite.py`.

## Regime-weighted composite (`strategy/composite.py`)

Each regime has its own weight vector over the five sources (see
`config/strategy_weights.yaml`). Trending regimes weight trend/volume/
relative-strength higher; ranging regimes weight mean-reversion/sentiment
higher (mean-reversion in a strong trend just means "still moving," and
trend-chasing in chop is a well-known way to bleed to fees). A proposal is
only generated if the weighted composite score clears `signal_threshold` and
average confidence clears `min_confidence`.

## Volatility-scaled sizing (`sizing/volatility_scaled.py`)

Position size scales with signal confidence and inversely with realized
volatility (ATR-based) — higher-conviction, lower-volatility setups get more
size — and is always clamped by `risk_limits.max_position_usd` and the
absolute code ceiling. Sizing decides how big to *propose*; the risk engine
independently and unconditionally decides whether to *allow* it.

## Execution planning (`execution/plan.py`)

Every proposal also gets an `ExecutionPlan` describing *how* to fill it,
built purely from its regime. This is informational only — it never affects
whether a proposal is allowed (that's still the risk engine's job alone) and
never touches the propose-only safety gate (`execution/adapter.py` still
always refuses to submit; a human still approves every proposal by ID).

The design was prompted by comparing four Polymarket-style trading bots for
transferable ideas (see conversation history / PR discussion for the full
writeup). Two ideas survived the trip to spot crypto with no derivatives and
no shorting:

- **Timeframe-conditional behavior** (the "mo-money" pattern): don't try to
  optimize entry the same way regardless of how much time a signal gives
  you. A TRENDING signal is time-sensitive — that bot's own short-timeframe
  data showed it paying a premium (~$1.07 combined cost on 5-minute markets)
  rather than waiting for a second entry, because waiting cost more than it
  saved. A RANGING signal has more runway to be patient.
- **Passive, staged accumulation** (the "almach" pattern): when there's
  time, place resting limit orders at favorable levels instead of crossing
  the spread, accepting that some orders may go unfilled. That bot's data
  showed tightly matched, favorably-priced positions (e.g. ~$0.97–$0.99
  combined cost on 1h/4h markets) built entirely from passive fills.

Two other ideas from the same comparison were **excluded** as structurally
inapplicable, not merely suboptimal:

- A "temporal complete-set arbitrage" pattern depends on buying two
  complementary conditional-outcome tokens that merge into a fixed $1
  redemption at settlement — spot crypto has no complementary instrument and
  no settlement event to redeem against, so the entire edge mechanism has no
  analog here.
- A sub-minute, near-100%-both-sided market-making pattern requires
  continuous two-sided quoting against a hard resolution event, at a trade
  cadence far beyond what a human-approval-gated execution model
  (`execution/adapter.py`) can support.

Concretely, `build_execution_plan(proposal)` maps `Signal.regime` to an
`ExecutionStyle`:

- **TRENDING → `PROMPT`**: a single tranche, sized to the full proposal, at
  the reference price — submit without delay.
- **RANGING → `STAGED`**: three tranches (weighted 40/35/25% of the size),
  with limit prices stepped 0% / 0.35% / 0.70% away from the reference price
  in the favorable direction (below reference for a BUY, above for a SELL).

The proposal report shows the plan so the human approver can see exactly how
it's meant to be filled before approving.

## Backtesting (`backtest/`)

Before a strategy change is trusted to generate live proposals, it's
validated with a walk-forward simulation (rolling train/validate windows, no
lookahead in indicator computation) against historical OHLCV pulled from
CoinGecko's public API, with a fee/slippage/spread cost model applied to
every simulated fill. Metrics: total return, Sharpe, Sortino, max drawdown,
win rate, average trade P&L, turnover. See `docs/risk-controls.md` for what
this does and doesn't prove.
