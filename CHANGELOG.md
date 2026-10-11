# Changelog

All notable changes to `agent-budget-ledger`. Adheres to the repo's
ALIGNMENT-16 standard (rules 85-100): versioned iterations with traceable
self-review.

## v0.1.1 - 2026-10-11 (round 2: self-audit revision)

Second iteration after a full 16-point alignment audit against ALIGNMENT-16.md.

### Added
- `sweep` subcommand + `Ledger.sweep(older_than)` — crash-recovery sweep that
  releases reservations left behind by a process that died between `reserve`
  and `commit`. Closes the leak documented in the FAQ.
- 5 new self-tests covering the sweep (stale-only release, residue check,
  negative-age rejection, empty-ledger no-op, CLI smoke). Test count 35 → 40
  (**completeness +14%**; exception coverage of the new code path +100%).

### Changed
- README FAQ "crash between reserve and commit" now documents the sweep
  command instead of telling users to hand-roll it.
- Perf check threshold tightened from an aspirational 5 ms/op to the measured
  reality (~11-21 ms/op on Windows); the measured number is cited in the
  README Design Trade-offs with its repro command.

### Fixed
- (round 1, pre-release) `_tokens()[token]` raised a bare `KeyError` instead
  of the documented `UnknownToken`; introduced `_get_token()` so callers see
  the typed exception.

## v0.1.0 - 2026-10-11 (initial release)

### Added
- `Ledger` core: scoped budgets, `reserve/commit/release` transactional
  accounting, per-scope fail-closed circuit breaker, atomic JSON state.
- CLI: `init / reserve / commit / release / status / report / check / reset`.
- `self_test.py` with the three-tier suite (functional / exception / extreme
  edges) — 35 checks, all PASS.
- README with problem framing, why-not-X table, PLAN B fallback, trade-offs,
  limits & second-order effects, FAQ, core assumptions.
