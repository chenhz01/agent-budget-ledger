#!/usr/bin/env python3
"""agent-budget-ledger: budget ledger + overspend circuit breaker for agent orchestration.

Middleware API:
    ledger = Ledger(path)
    tok = ledger.reserve("agent:writer", 0.05)   # block if over budget
    ledger.commit(tok, 0.043)                    # actual cost
    ledger.release(tok)                          # refund on failure
    if ledger.tripped("agent:writer"): ...       # circuit breaker state

CLI:
    python main.py init    --scope agent:writer --limit 10.0 --currency USD
    python main.py reserve --scope agent:writer --cost 0.05
    python main.py commit  --token <id> --actual 0.043
    python main.py release --token <id>
    python main.py status  [--scope agent:writer]
    python main.py report  --format table|json
    python main.py check   --scope agent:writer   (exit 2 when breaker tripped)

Pure stdlib. State is one JSON file with atomic writes.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
import uuid

BREAKER_OPEN = "OPEN"      # normal: spending allowed
BREAKER_TRIPPED = "TRIPPED"  # overspent: all reserves rejected
# Breaker priority: SAFETY > FEATURE. We fail closed (reject spending) rather
# than fail open (allow) when state is ambiguous or corrupt.


class BudgetError(Exception):
    """Base error."""


class UnknownScope(BudgetError):
    pass


class UnknownToken(BudgetError):
    pass


class BudgetExceeded(BudgetError):
    """Raised/reserved-rejected when the scope's breaker is tripped."""


class CorruptState(BudgetError):
    pass


def _now() -> float:
    return time.time()


def _atomic_write(path: str, data: str) -> None:
    d = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".abl-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class Ledger:
    """Durable budget ledger with reserve/commit/release accounting."""

    def __init__(self, path: str = "budget.json", hard_limit_factor: float = 1.0):
        self.path = path
        if hard_limit_factor <= 0:
            raise ValueError("hard_limit_factor must be > 0")
        self.hard_limit_factor = hard_limit_factor
        self._state = self._load()

    # ---------- persistence ----------
    def _empty(self) -> dict:
        return {"version": 1, "scopes": {}, "tokens": {}, "history": []}

    def _load(self) -> dict:
        if not os.path.exists(self.path):
            return self._empty()
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                raw = f.read()
            state = json.loads(raw) if raw.strip() else self._empty()
        except (OSError, json.JSONDecodeError) as e:
            raise CorruptState(f"state file {self.path} unreadable: {e}") from e
        if not isinstance(state, dict) or "scopes" not in state or "tokens" not in state:
            raise CorruptState("state file has wrong schema (missing scopes/tokens)")
        return state

    def _save(self) -> None:
        _atomic_write(self.path, json.dumps(self._state, indent=1, sort_keys=True))

    # ---------- scopes ----------
    def init_scope(self, scope: str, limit: float, currency: str = "USD",
                   warn_at: float = 0.8) -> dict:
        if not scope or not isinstance(scope, str):
            raise ValueError("scope must be a non-empty string")
        if limit <= 0 or not (limit < float("inf")):
            raise ValueError("limit must be a finite positive number")
        if not 0 < warn_at <= 1:
            raise ValueError("warn_at must be in (0, 1]")
        scopes = self._state["scopes"]
        if scope in scopes:
            raise BudgetError(f"scope already exists: {scope}")
        scopes[scope] = {
            "limit": limit, "spent": 0.0, "reserved": 0.0,
            "currency": currency, "warn_at": warn_at,
            "breaker": BREAKER_OPEN, "created_at": _now(),
        }
        self._save()
        return dict(scopes[scope])

    def _scope(self, scope: str) -> dict:
        try:
            return self._state["scopes"][scope]
        except KeyError:
            raise UnknownScope(f"no such scope: {scope}") from None

    # ---------- reserve / commit / release ----------
    def reserve(self, scope: str, cost: float) -> str:
        """Reserve `cost` against the scope. Returns a token id.

        Raises BudgetExceeded when the breaker is tripped or the reserve
        would push committed+reserved past the hard limit.
        """
        s = self._scope(scope)
        if not isinstance(cost, (int, float)) or isinstance(cost, bool) or cost != cost or cost in (float("inf"),):
            raise ValueError("cost must be a finite number")
        if cost < 0:
            raise ValueError("cost must be >= 0 (use release for refunds)")
        if s["breaker"] == BREAKER_TRIPPED:
            raise BudgetExceeded(f"breaker TRIPPED for {scope}: budget exhausted")
        hard = s["limit"] * self.hard_limit_factor
        if s["spent"] + s["reserved"] + cost > hard:
            s["breaker"] = BREAKER_TRIPPED
            self._save()
            raise BudgetExceeded(
                f"reserve of {cost} would exceed hard limit {hard} for {scope}")
        tok = uuid.uuid4().hex
        s["reserved"] += cost
        self._state["tokens"][tok] = {
            "scope": scope, "amount": cost, "state": "reserved", "ts": _now()}
        self._save()
        return tok

    def commit(self, token: str, actual: float) -> dict:
        """Convert a reservation into real spend."""
        t = self._get_token(token)  # raises UnknownToken
        if t["state"] != "reserved":
            raise BudgetError(f"token {token} already {t['state']}")
        s = self._scope(t["scope"])
        if actual < 0 or actual != actual or actual == float("inf"):
            raise ValueError("actual must be a finite non-negative number")
        reserved = t["amount"]
        delta = actual - reserved
        # overspend beyond hard limit still commits but trips the breaker
        s["reserved"] -= reserved
        s["spent"] += actual
        if s["spent"] > s["limit"]:
            s["breaker"] = BREAKER_TRIPPED
        t["state"] = "committed"
        t["actual"] = actual
        self._save()
        return {"scope": t["scope"], "actual": actual,
                "warn": s["spent"] >= s["limit"] * s["warn_at"],
                "tripped": s["breaker"] == BREAKER_TRIPPED}

    def release(self, token: str) -> dict:
        """Refund a reservation (call failed / was rolled back)."""
        t = self._get_token(token)
        if t["state"] != "reserved":
            raise BudgetError(f"token {token} already {t['state']}")
        s = self._scope(t["scope"])
        s["reserved"] -= t["amount"]
        t["state"] = "released"
        self._save()
        return {"scope": t["scope"], "refunded": t["amount"]}

    def _tokens(self) -> dict:
        return self._state["tokens"]

    def _get_token(self, token: str) -> dict:
        t = self._tokens().get(token)
        if t is None:
            raise UnknownToken(f"no such token: {token}")
        return t

    # ---------- breaker ----------
    def tripped(self, scope: str) -> bool:
        return self._scope(scope)["breaker"] == BREAKER_TRIPPED

    def reset(self, scope: str, new_limit: float | None = None) -> dict:
        """Human-gated breaker reset (operator action)."""
        s = self._scope(scope)
        s["breaker"] = BREAKER_OPEN
        if new_limit is not None:
            if new_limit < s["spent"]:
                raise ValueError(
                    f"new limit {new_limit} below already-spent {s['spent']}")
            s["limit"] = new_limit
        self._save()
        return dict(s)

    def sweep(self, older_than: float = 3600.0) -> list[str]:
        """Release stale reserved tokens (crash-recovery sweep).

        Releases every token still in state 'reserved' whose ts is older
        than `older_than` seconds. Returns released token ids.
        """
        if older_than < 0:
            raise ValueError("older_than must be >= 0")
        now = _now()
        released: list[str] = []
        for tok, t in list(self._state["tokens"].items()):
            if t["state"] == "reserved" and now - t["ts"] > older_than:
                try:
                    self.release(tok)
                    released.append(tok)
                except BudgetError:
                    pass
        return released

    # ---------- reporting ----------
    def status(self, scope: str | None = None) -> dict | list:
        scopes = self._state["scopes"]
        if scope is not None:
            return dict(self._scope(scope))
        return {k: dict(v) for k, v in scopes.items()}

    def report(self, fmt: str = "table") -> str:
        scopes = self._state["scopes"]
        if fmt == "json":
            return json.dumps(self.status(), indent=2, sort_keys=True)
        if fmt != "table":
            raise ValueError(f"unknown format: {fmt}")
        if not scopes:
            return "(no scopes)"
        lines = ["%-24s %10s %12s %8s  %s" % ("scope", "spent", "limit", "use%", "breaker")]
        for name, s in sorted(scopes.items()):
            pct = 100.0 * s["spent"] / s["limit"] if s["limit"] else 0.0
            lines.append("%-24s %10.4f %12.2f %7.1f%%  %s" %
                         (name, s["spent"], s["limit"], pct, s["breaker"]))
        return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="agent-budget-ledger",
                                description="budget ledger + overspend circuit breaker")
    p.add_argument("--ledger", default="budget.json", help="state file path")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("init"); sp.add_argument("--scope", required=True)
    sp.add_argument("--limit", type=float, required=True)
    sp.add_argument("--currency", default="USD"); sp.add_argument("--warn-at", type=float, default=0.8)

    sp = sub.add_parser("reserve"); sp.add_argument("--scope", required=True)
    sp.add_argument("--cost", type=float, required=True)

    sp = sub.add_parser("commit"); sp.add_argument("--token", required=True)
    sp.add_argument("--actual", type=float, required=True)

    sp = sub.add_parser("release"); sp.add_argument("--token", required=True)
    sp = sub.add_parser("status"); sp.add_argument("--scope", default=None)
    sp = sub.add_parser("report"); sp.add_argument("--format", choices=["table", "json"], default="table")
    sp = sub.add_parser("check"); sp.add_argument("--scope", required=True)
    sp = sub.add_parser("reset"); sp.add_argument("--scope", required=True)
    sp.add_argument("--limit", type=float, default=None)
    sp = sub.add_parser("sweep"); sp.add_argument("--older-than", type=float, default=3600.0)

    a = p.parse_args(argv)
    try:
        led = Ledger(a.ledger)
        if a.cmd == "init":
            print(json.dumps(led.init_scope(a.scope, a.limit, a.currency, a.warn_at), indent=2))
        elif a.cmd == "reserve":
            print(led.reserve(a.scope, a.cost))
        elif a.cmd == "commit":
            print(json.dumps(led.commit(a.token, a.actual)))
        elif a.cmd == "release":
            print(json.dumps(led.release(a.token)))
        elif a.cmd == "status":
            print(json.dumps(led.status(a.scope), indent=2))
        elif a.cmd == "report":
            print(led.report(a.format))
        elif a.cmd == "check":
            print("TRIPPED" if led.tripped(a.scope) else "OK")
            return 2 if led.tripped(a.scope) else 0
        elif a.cmd == "reset":
            print(json.dumps(led.reset(a.scope, a.limit), indent=2))
        elif a.cmd == "sweep":
            print(json.dumps({"released": led.sweep(a.older_than)}, indent=2))
    except BudgetError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
