---
name: rhca-timemachine
description: >-
  Run the Time Machine: the odds of a split trade, replayed thousands of times
  on the coin's own past days, beside the rule's real record on that coin. For
  a breakout entry (a coin at the last close, or a breakout-entry proposal by
  id): how often the trailing stop sells it within 30/60/90 days, and its R.
  For the forward test: how likely it reaches its 3 priced paper fills by its
  read and its extension. Use when the owner says "run the time machine", "what
  are the odds on <id>", "replay this breakout", "how often does this get
  stopped out", "is this a coin flip", "will the forward test get enough
  trades", or before deciding on a breakout-entry proposal.
argument-hint: "[COIN | --proposal <id> | --forward-test]"
---

# The Time Machine

Before the owner accepts a breakout entry, this asks: if the coming days look
like this coin's own past days, how does the trade usually end? It replays the
next 90 days thousands of times from real daily bars, through the live rule's
own stop, and sets the rule's real record on the coin beside it. It reads
Coinbase's daily bars and changes nothing: no proposal, no audit-log record.
How it works is in `docs/timemachine.md`.

```
rhca timemachine --proposal <id>     # the breakout entry a proposal is for
rhca timemachine SOL-USD             # a breakout entry at the last close
rhca timemachine --forward-test      # odds of 3 priced paper fills by the read and extension
rhca timemachine SOL-USD --seed 7 --replays 5000 --json
```

Other flags: `--horizon` (days, default 90), `--sample-days` (default 1460),
`--block-days` (default 10), `--round-trip` (percent, default 1.9). On the
owner's Windows PC: `.venv\Scripts\rhca timemachine`. It needs Coinbase, so
run it there, not in a cloud sandbox.

## Steps

1. For a proposal, run `rhca timemachine --proposal <id>`. It only takes a
   short-term entry: a tranche is held for good and a stop is a sell, and it
   says so. Use `rhca-review-proposal` for those.
2. Report in this order, each line exactly as printed: the entry and its
   stop; "not a breakout today" if printed; the sampled days and their drift;
   stopped out by each checkpoint, at a gain and at a loss; still held; the R
   line; the price line; the history line; the verdict and every line under
   it. Say the seed.
3. If the verdict is close to break-even, say so plainly. If the replays and
   the history disagree, or the median and the mean point different ways,
   say that too: those lines are printed for it.
4. Say once: the replays carry the coin's past drift and know nothing of
   today's news, so they are odds, not a forecast.
5. For the forward test, run `rhca timemachine --forward-test` and report the
   fills so far, each deadline's line, the per-coin table and the verdict,
   as printed. It counts as priced only fills `rhca run` logged with a quote,
   and assumes the loop records every close from here: say that.
6. The decision stays the owner's, on the dashboard, by id.

## Rules

- Every number comes from the run. Never round a coin flip into an edge, or
  rewrite the verdict.
- A trailing stop can sell at a gain: "stopped out" is not "lost".
- One run per question. A changed coin, day, seed or setting is a new run;
  say which settings differ from the defaults.
- A good replay is not approval (CLAUDE.md, "Your role"; rule 1), and a poor
  one is not a reason to touch a limit or `config/*.yaml` (rule 9).

## What it never does

- Proposes, accepts, declines or places anything, or writes to the audit log.
- Calls a `RobinHood` MCP order tool, or runs `rhca approve` for a split
  proposal.
- Moves the forward test's bar, or reads a replay as one of its checks.
- Presents the odds as a prediction or a promise.
