# Verdict day, 2026-12-26/27: PRs #43, #44, #45 in one sitting

For task t289. This is read-only: no PR was merged, no order was placed, no RobinHood tool was called, and no tolerance or limit was changed. It puts the three drift-gate PRs in one order and adds a checklist to the verdict sheet (`docs/forward-test-verdict-2026-12-26.md` §6).

## 1. Where the three PRs stand (checked 2026-11-27 UTC)

| PR | What it is | State | Base | Behind `main` | CI | Touches the order path? |
|---|---|---|---|---|---|---|
| #43 | Audit of the open live proposals (`docs/stale-live-proposals-2026-11-24.md`) | **Merged** 2026-11-24 06:50 UTC | `main` | n/a | green | No, docs only |
| #44 | Drift gate decision sheet (`docs/drift-gate-decision-2026-11-25.md`) | Open, mergeable, clean | `main` | 0 commits | green | No, docs only |
| #45 | Option B fix: ask-to-ask drift, and `rhca approve` refuses superseded or old ids | Open, mergeable, clean | `main`, not stacked | 0 commits | green | **Yes.** `execution/gate.py`, `execution/desk.py`, `cli.py`, `dashboard.py`, `models.py` |

"CI green" means pytest on Python 3.10 to 3.13, ruff, the dashboard typecheck and build, and the Vercel preview. As a set, #44 and #45 change different files. Merged together onto `main` at `88bf123` they gave no conflict, and the full suite passed (625 tests).

## 2. The order

1. **#43**: already merged. Nothing to do.
2. **#44**: merge any time. It is one Markdown file and changes no behaviour. Read its §4 and §5 before step 4, because #45 implements its Option B.
3. **This PR (t289)**: merge any time. Docs only.
4. **#45**: last, and only after you choose Option B and confirm the reading in §3, Decision 3 below. It changes the gate on the one code path that sends real orders, and its own description says `main` auto-deploys. If you choose A or C, close it unmerged.

## 3. Checklist for 2026-12-26/27

**First, read-only, before deciding anything**
- [ ] `rhca status`. If the kill switch is engaged, stop here: no accept today.
- [ ] `git log --oneline -5 origin/main`. Note which of #44 and #45 have merged.
- [ ] `rhca report` and `rhca shadow` (without `--backfill`). Update the verdict sheet §1 to §4 from them.

**What you decide, in this order**
1. [ ] **Decision 1: does the bar pass?** Watch the costs median against the 1.9% limit. If any check fails, stop approving the split's proposals. Decision 3 is then "keep declining", and #45 can wait.
2. [ ] **Decision 2: does a pass with zero breakout trades count for the breakout?**
3. [ ] **Drift gate: Option A, B or C** (#44 §5). If you choose B, confirm two things in writing on #45. First, that measuring a buy against the ask (and a sell against the bid) corrects the measure and does not widen the 0.5% tolerance. Second, that the `rhca approve` stale-id fix ships in the same PR. Then merge #45.
4. [ ] **Decision 3: start accepting tranches by id, or keep declining.** Under Option A, choosing "accept" changes nothing in practice, because the gate refuses every fresh buy at a ~1.9% spread. Record that if you choose it.
5. [ ] **Decision 4, plus #45's "Not in this PR" list:** the missing XRP tranche, the missed closes, `rhca approve` using a cached quote past 90 seconds, and `rhca approve` not checking the split's book, the daily cap or the dashboard's declines. Each becomes its own task. None of them blocks the verdict.

**What is retested first: after #45 merges, before any real accept**
- [ ] CI is green on `main` after the merge commit.
- [ ] #44 §6 script, read-only, against the live `data/audit_log.jsonl` and `data/market_state.json`. The newest proposal per coin should show ask-to-ask drift under 0.5%.
- [ ] Dashboard: the newest row per coin shows Accept with no "price has moved" blocker, unless the ask really moved.
- [ ] Only once `git log` shows #45 on `main`: `rhca approve <an old id> --approval "execute <id>"` must exit "superseded by …" or "… minutes old". It refuses before any payload is built. Do not follow it with a preview or an order.
- [ ] Only then: the first real accept. You make it yourself, on the dashboard, on the newest row for one coin, within the hour it was logged. Expect a `gtc` limit at the proposal's ask. It fills at once if the ask has not risen, and rests if the ask has risen by up to 0.5%.
- [ ] Sells (bid-to-bid) have unit tests only. Watch the first breakout exit.

**What must NOT be run**
- No agent session calls `place_crypto_order` or any RobinHood order tool. The split trades through the dashboard's desk, not the MCP tools (CLAUDE.md).
- Do not widen `price_drift_tolerance_pct` (0.5%) or any value in `config/risk_limits.yaml` to get an accept through. A "price has moved" refusal is the gate working.
- Do not run `rhca approve` on an old or superseded id while #45 is unmerged. Before #45, the audit counted 51 old ids that would produce a valid payload (#43 §4).
- Do not edit `config/shadow.yaml`, because that restarts the forward test. Do not run `rhca shadow --backfill` for the verdict figures.
- Do not delete `data/KILL_SWITCH`. Do not override a risk block without the exact phrase and `record-execution --override`.
- Do not promote anything to `auto`. A pass does not unlock Phase 2 (verdict sheet §6).

After #45 merges, `docs/stale-live-proposals-2026-11-24.md` §4 to §6 and #44 §3 describe the code from before the fix. Read them as history from then on.
