# Strategy

## Regime first

Trend following and mean reversion are close to opposites. Blending them at
fixed weights averages out to noise: in a strong uptrend the mean-reversion
source is maximally bearish *and correct about the statistic it measures* —
price really is extended — while being exactly wrong about the trade.

So the first question is not "which way?" but "is there a trend?", and ADX
answers it without reference to direction:

| ADX | Regime | Reading |
|---|---|---|
| ≥ `adx_trend_threshold` (22) | `trending` | A directional move is in progress |
| < threshold | `ranging` | No dominant trend |
| not warmed up | `unknown` | Not enough bars to have an answer |

`unknown` is not a synonym for `ranging`. It means the question has no answer
yet, and the blend falls back to equal weights rather than committing to
either playbook.

## The four signals

Each returns a score in `[-1, 1]` and a confidence in `[0, 1]`, or **`None`**
when history is too thin. `None` is the honest answer: a source must never
return a neutral score to paper over missing data, because the composite
distinguishes "no opinion" from "an opinion of zero".

### trend
MACD histogram divided by ATR, plus fast/slow SMA alignment. Dividing by ATR
puts the reading in units of "typical bar range", which is comparable across
symbols — a $300 histogram means something very different on BTC than on DOGE,
and an un-normalized score would rank symbols by price level.

### momentum
Z-score of the close against its own recent window, capped at ±2σ before
scaling. Beyond 2σ a move is more likely a data artifact or a liquidation
cascade than a tradable trend.

### mean_reversion
RSI plus position within the Bollinger bands. Contrarian by construction: a low
RSI at the lower band is a *long*. Heavily down-weighted in a trending regime,
because "oversold" in a downtrend is a description, not an entry.

### breakout
Close beyond the prior N-bar Donchian channel, normalized by ATR. The channel
excludes the current bar — a channel that includes the current bar's own high
can never be exceeded by it, so the signal would never fire. Inside the channel
is a real "no breakout" reading, reported with halved confidence so it does not
dilute the blend as if it were a measurement.

## The blend

```
score      = Σ(wᵢ · cᵢ · sᵢ) / Σ(wᵢ · cᵢ)
confidence = Σ(wᵢ · cᵢ) / Σ(wᵢ over ALL configured sources)
```

The denominators differ deliberately. The **score** averages over the sources
that actually reported, so an unavailable signal does not drag the score toward
zero. The **confidence** divides by the total configured weight *including*
sources that reported nothing — so if three of four sources have no history,
the composite says so with a low confidence instead of presenting one source's
reading as the settled view.

A source with no configured weight is inert, not implicitly equal-weighted:
adding a signal source cannot change the blend until someone gives it a weight
on purpose.

## Default weights

| Signal | trending | ranging | unknown |
|---|---|---|---|
| trend | 0.50 | 0.15 | 0.25 |
| momentum | 0.25 | 0.15 | 0.25 |
| breakout | 0.15 | 0.20 | 0.25 |
| mean_reversion | 0.10 | 0.50 | 0.25 |

Weights are L1-normalized on load, so they read as relative importance and do
not have to sum to 1 by hand. Configuring one regime merges over the defaults
rather than replacing them.

## Sizing

Three factors, each bounded at 1.0, so the product can never exceed the
per-trade cap:

```
notional = max_notional_per_trade
         × conviction            (|score| × confidence)
         × volatility_scalar     (min(1, target_vol / realized_vol))
         ↓ then clamped by the concentration cap and the pair's limits
quantity = notional / reference_price, snapped DOWN to the pair's increment
```

Snapping down matters: it can only make an order smaller, so rounding never
pushes a position past a limit that was checked against the unrounded size.

Volatility scaling means a fixed dollar budget does not become a much larger
*risk* budget when the market gets twice as violent. Realized volatility is the
stdev of recent log returns, per bar — the same time scale as the bars, so no
annualization constant has to be guessed.

## Execution plans

Regime decides *how* to fill, which is a separate question from whether to
trade:

- **`PROMPT`** — one marketable limit at the reference price. Trend signals are
  time-sensitive; waiting for a better entry in a trending market usually means
  not getting filled.
- **`STAGED`** — three limit orders laddered away from the mark, spaced by ATR.
  In a ranging market price is expected to come back, so paying the spread for
  immediacy is waste. An unfilled tranche is an acceptable outcome.

Tranche quantities sum to exactly the approved quantity — the last one absorbs
the rounding remainder, so a plan can never total more than risk approved. Buy
limits round *down* to the tick and sell limits round *up*, so snapping never
makes an order more aggressive than it was priced to be.

A `market_orders_only` pair overrides all of this with a single market order,
since limit orders would simply be rejected.

## Exits

A buy is graded on where the price is `exit_after_bars` bars later (six, at
60-minute bars), so the time exit makes the real trade match its grade. Once
a lot the agent bought has been held that long, `rhca run` and `rhca analyze`
propose selling it: one limit order at the bid, approved by id like any other
trade. It never goes to System 2, because it is the owner's rule, not a signal
to second-guess.

What counts as the agent's comes from the audit log: filled buys, less filled
sells, first in first out. A coin bought outside the agent has no lot, so it is
never proposed for sale. The sale is capped at what the account holds. If the
holdings snapshot is older than the buy, the exit is still proposed, and
`sell_coverage` blocks it until positions are re-ingested. Time exits are not
scored: they close positions, they don't predict.

A signal sell can close a position sooner, when the view on a held coin turns
bearish. Signal sells are governed by `disable_sell_side` in
`config/risk_limits.yaml`; time exits are not.

## Validating a change

There is no backtest harness pointed at a public historical API — the
environment blocks those, and the MCP server has no crypto historicals tool.
What the repo offers instead:

1. `rhca import-history` to load bars from a source you trust.
2. `rhca analyze --no-record` to evaluate without writing to the audit log.
3. The test suite, where regime classification, blending, and sizing are
   asserted against synthetic trending, ranging, and thin-data series.

A positive result from any of these is **not** authorization to loosen a risk
limit or skip the approval gate. They validate a signal against history; they
say nothing about a specific live trade.
