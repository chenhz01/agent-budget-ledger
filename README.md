A budget ledger and overspend circuit breaker for any agent orchestration layer. Wrap your planner's tool calls with reserve/commit accounting; when a goal, agent, or team exceeds its budget, spending trips a breaker instead of silently draining your API bill. Pure Python, stdlib only, state in one JSON file.

If you run multi-agent workflows without per-goal spending limits, this is for you. If you already have centralized billing with hard quotas, you don't need it.

## Problem

Agent orchestration tools talk about capabilities; almost none treat **per-agent budget as a first-class resource** (see paperclipai/paperclip for the "company metaphor" — org chart, budget, governance — which is where this gap became obvious). The overlooked angle: enterprise adoption of agent swarms is bottlenecked less by capability than by *cost accountability*. Today the common failure mode is a runaway loop discovering, hours later, that one subagent burned your monthly quota.

Existing options either (a) log costs after the fact — dashboards, not controls, or (b) hard-code limits inside one framework, which does not transfer.

**Why novel here:** reserve/commit/release accounting (like a DB transaction, but for dollars) plus a *fail-closed circuit breaker*: once a scope trips, every reserve is rejected until a human resets it. Confidence: **High (≥80%)** — the mechanism is ~300 lines, fully self-tested; the claim is only that the accounting shape is general, not that any specific limit is right for you.

## How it works

- **Scopes** are budget containers: `agent:writer`, `goal:launch`, `team:research`. Each has a `limit`, `spent`, `reserved`, `warn_at`.
- **reserve(scope, cost)** → token. Rejected (`BudgetExceeded`) if committed+reserved+cost would cross the hard limit. Also rejected if the breaker is tripped.
- **commit(token, actual)** → real spend. Actual can differ from the estimate (refund/overage handled). Overspend *commits but trips the breaker*.
- **release(token)** → refund a failed call's reservation. Fixes the classic "reserved but crashed" leak.
- **Circuit breaker** per scope: `OPEN` → `TRIPPED`. Fail-closed by design (safety > availability).
- State is one JSON file, written atomically (tempfile + `os.replace`), so a kill at any instant never corrupts the ledger.

## Quickstart (~5 min)

```bash
# 1. run the self-test (~10 s)
python self_test.py

# 2. minimal example (~1 min)
python main.py init --scope agent:writer --limit 10.0
TOK=$(python main.py reserve --scope agent:writer --cost 0.05)
python main.py commit --token $TOK --actual 0.043
python main.py report

# 3. wire into your orchestrator (~2 min): call `check` before dispatch;
#    exit code 2 means "stop spending on this scope"
python main.py check --scope agent:writer || echo "breaker tripped, halt"
```

Full path: integrate `from main import Ledger` directly — see the module docstring.

## Why not X

| Alternative | What it does | Why not enough |
|---|---|---|
| Provider dashboards (OpenAI/Anthropic usage pages) | Show aggregate spend after requests complete | After-the-fact; no per-goal scoping; cannot *reject* a reserve mid-loop |
| Framework-internal quotas (LangChain/LangSmith budgets) | Limits bound to one framework's runtime | Not portable; orchestration layers mix frameworks |
| DIY `if cost > n: break` in your planner | One-line guard | No reservation accounting, no refunds, no crash-safe state, trip state not shareable across processes |
| Full finops platforms (e.g. cloud cost managers) | Organization-wide billing analytics | Heavyweight, no agent-loop integration point, built for infra not for "this subagent may spend $0.50" |

## Minimal Fallback (PLAN B)

No Python? No persistence? The core invariant fits in a shell loop:

```bash
# read spent from any file, refuse to spend past LIMIT
SPENT=$(cat spent.txt 2>/dev/null || echo 0)
[ "$(echo "$SPENT < $LIMIT" | bc)" = 1 ] || exit 2
echo "$SPENT + $COST" | bc > spent.txt
```

You lose reservations, refunds, and atomicity — but you keep the fail-closed gate.

## Design Trade-offs

Priority order: **safety > correctness > performance > features.**

- **Fail-closed breaker.** A corrupt or ambiguous state stops spending rather than continuing. Costs availability; prevents silent drain.
- **Single JSON file, whole-file rewrite.** Simple, crash-atomic, human-inspectable. Not a concurrent multi-writer DB — one orchestrator process at a time. (No file locking; see Limits.)
- **Reservations are visible debt.** `reserved` is counted against the hard limit, so parallel loops can't each "almost" reach the cap. Costs a refund code path.
- **Token history grows unboundedly.** Committed/released tokens stay in state for auditability. This makes long-running ledgers slower over time: measured **~15–21 ms per reserve+commit cycle at 500 accumulated tokens** (Windows, CPython 3.13; repro: `python self_test.py`). Acceptable when LLM calls take seconds; unacceptable at >10k ops/min.

## Limits & Second-order Effects

- **No locking.** Two processes writing the same ledger can lose updates. Run one writer, or shard scopes across files.
- **Trusts cost estimates.** The ledger cannot verify that `actual` is what your provider charged. GIGO at the reporting edge.
- **Second-order effect 1 — sandbox complacency:** teams may treat the breaker as the *entire* cost strategy and stop reviewing what agents actually buy. The breaker stops overspend; it does not stop wasteful spend under the cap.
- **Second-order effect 2 — limit ratchet:** easy human resets invite "just raise the limit" culture, recreating the original problem with a bigger number. Reset requires an explicit operator action for this reason.
- **Second-order effect 3 — metric gaming:** if budgets gate agent autonomy, agents optimized for cheap tasks may under-deliver. Budgets are a constraint, not an objective.

## FAQ

**Q: Can I use currencies other than USD?**
The `currency` field is stored but not converted. Use one currency per ledger.

**Q: Does it work with my framework (CrewAI, LangGraph, custom loop)?**
Yes — it is deliberately dumb. You call `reserve` before each model/tool call and `commit` after, with whatever cost numbers you have.

**Q: What happens on crash between reserve and commit?**
The reservation stays in state and keeps inflating `reserved`. Run `python main.py sweep --older-than 3600` on restart to release stale tokens (a sweep test covers exactly this crash path in `self_test.py`).

**Q: When NOT to use this tool?** *(ask yourself this first)*
- Single agent, low volume: a spreadsheet or nothing beats middleware.
- You need billing-grade accuracy: the ledger only knows what you tell it.
- You need concurrent multi-writer budgets: use a real DB with transactions instead.
- You want automatic provider throttling: this never talks to the network by design.

**Q: Why trip on the *commit* instead of blocking the reserve that exceeds?**
Reserves over the hard limit *are* blocked. But `actual` can exceed `reserved` — real costs arrive after the API call finishes. Committing the truth and tripping keeps the ledger honest.

## [Core Assumptions]

1. Costs can be estimated *before* a call (reserve) and corrected *after* (commit). If your costs are unknowable in advance, reserves degenerate to guesses.
2. A single orchestrator process (or externally serialized writers) owns the ledger file; no concurrent multi-writer support is required.
3. Overspend is worse than interruption: failing closed on a tripped scope is the preferred error mode.
4. Cost units are consistent within a ledger (no mixed-currency or mixed-unit accounting).
5. Human operators are the only party allowed to reset a tripped breaker or raise limits.

---
Contact: hcac4735@agent.qq.com · License: MIT · Data/currency figures in this README: none time-sensitive; perf figures measured as noted.
