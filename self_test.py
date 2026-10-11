#!/usr/bin/env python3
"""Self-test for agent-budget-ledger.

Three tiers (ALIGNMENT rule 97):
  A. functional unit tests  (internal consistency)
  B. exception / error paths (external adaptability)
  C. extreme edges          (empty input, huge input, corrupted state)

Run:  python self_test.py
Exit 0 = all PASS.
"""
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from main import (BREAKER_TRIPPED, BudgetError, BudgetExceeded, CorruptState,
                  Ledger, UnknownScope, UnknownToken, main)

PASS = 0
FAIL = 0
PERF = []


def check(name: str, cond: bool):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}")


def tmp_ledger() -> tuple[Ledger, str]:
    fd, path = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    os.unlink(path)
    return Ledger(path), path


# ---------------------------------------------------------------- A. functional
print("[A] functional unit tests")
led, path = tmp_ledger()
led.init_scope("agent:writer", limit=10.0, currency="USD")
check("init returns scope", led.status("agent:writer")["limit"] == 10.0)

tok = led.reserve("agent:writer", 0.05)
check("reserve returns token", isinstance(tok, str) and len(tok) == 32)
check("reserved tracked", led.status("agent:writer")["reserved"] == 0.05)

r = led.commit(tok, 0.043)
check("commit adds to spent", abs(led.status("agent:writer")["spent"] - 0.043) < 1e-9)
check("commit clears reservation", led.status("agent:writer")["reserved"] == 0.0)
check("commit result fields", r["scope"] == "agent:writer" and r["tripped"] is False)

tok2 = led.reserve("agent:writer", 0.01)
r2 = led.release(tok2)
check("release refunds reservation", led.status("agent:writer")["reserved"] == 0.0)

# persistence round-trip
led2 = Ledger(path)
check("reload sees spent", abs(led2.status("agent:writer")["spent"] - 0.043) < 1e-9)

# multi-scope isolation
led.init_scope("goal:launch", limit=5.0)
led.reserve("goal:launch", 1.0)
check("scopes isolated", led.status("agent:writer")["reserved"] == 0.0)

# breaker trips on overspend-commit
led3, p3 = tmp_ledger()
led3.init_scope("t", limit=1.0)
t1 = led3.reserve("t", 0.9)
t2 = led3.reserve("t", 0.05)
led3.commit(t1, 1.2)  # overspend commits, trips breaker
check("breaker tripped after overspend", led3.tripped("t"))

# report rendering
rep = led.report()
check("report table has rows", "agent:writer" in rep and "goal:launch" in rep)
repj = json.loads(led.report("json"))
check("report json parses", repj["agent:writer"]["limit"] == 10.0)

# CLI smoke (exit codes)
rc = main(["--ledger", path, "status"])
check("CLI status exit 0", rc == 0)
rc = main(["--ledger", p3, "check", "--scope", "t"])
check("CLI check exit 2 when tripped", rc == 2)

# ---------------------------------------------------------------- B. exceptions
print("[B] exception / error paths")
try:
    led.reserve("nope:missing", 0.1)
    check("unknown scope raises", False)
except UnknownScope:
    check("unknown scope raises", True)

try:
    led.commit("deadbeef" * 4, 0.1)
    check("unknown token raises", False)
except UnknownToken:
    check("unknown token raises", True)

try:
    led.init_scope("agent:writer", limit=1.0)
    check("duplicate scope rejected", False)
except BudgetError:
    check("duplicate scope rejected", True)

led4, p4 = tmp_ledger()
led4.init_scope("t", limit=0.5)
try:
    led4.reserve("t", 0.6)  # exceeds hard limit
    check("over-limit reserve rejected", False)
except BudgetExceeded:
    check("over-limit reserve rejected", True)
check("breaker auto-tripped", led4.tripped("t"))

t5 = led4.reserve("t", 0.1) if not led4.tripped("t") else None
# after trip, everything rejected (fail-closed)
try:
    led4.reserve("t", 0.01)
    check("tripped breaker rejects all", False)
except BudgetExceeded:
    check("tripped breaker rejects all", True)

try:
    led.reserve("agent:writer", -1.0)
    check("negative cost rejected", False)
except ValueError:
    check("negative cost rejected", True)

try:
    led.init_scope("", limit=1.0)
    check("empty scope rejected", False)
except ValueError:
    check("empty scope rejected", True)

try:
    led.init_scope("x", limit=0)
    check("zero limit rejected", False)
except ValueError:
    check("zero limit rejected", True)

t_neg = led.reserve("goal:launch", 0.01)
try:
    led.commit(t_neg, -0.5)
    check("negative actual rejected", False)
except ValueError:
    check("negative actual rejected", True)
led.release(t_neg)

# double-commit same token
try:
    led.commit(tok, 0.01)
    check("double commit rejected", False)
except BudgetError:
    check("double commit rejected", True)

# ---------------------------------------------------------------- C. edges
print("[C] extreme edges")
# empty ledger (fresh file, no scopes)
led5, p5 = tmp_ledger()
check("empty ledger report", led5.report() == "(no scopes)")
check("empty ledger status", led5.status() == {})

# corrupted state file
fd, p6 = tempfile.mkstemp(suffix=".json")
os.write(fd, b"{not valid json!!")
os.close(fd)
try:
    Ledger(p6)
    check("corrupt json raises CorruptState", False)
except CorruptState:
    check("corrupt json raises CorruptState", True)

fd, p7 = tempfile.mkstemp(suffix=".json")
os.write(fd, b'{"hello": 1}')
os.close(fd)
try:
    Ledger(p7)
    check("wrong schema raises CorruptState", False)
except CorruptState:
    check("wrong schema raises CorruptState", True)

# huge numbers
led6, p6b = tmp_ledger()
led6.init_scope("whale", limit=1e12)
tok6 = led6.reserve("whale", 1e11)
led6.commit(tok6, 1e11)
check("huge amounts handled", abs(led6.status("whale")["spent"] - 1e11) < 1e3)

# tiny costs: many small reserves then release all (leak check)
led7, p7b = tmp_ledger()
led7.init_scope("micro", limit=1.0)
toks = [led7.reserve("micro", 0.0001) for _ in range(200)]
for t in toks:
    led7.release(t)
check("200 reserve+release zero residue", abs(led7.status("micro")["reserved"]) < 1e-12)

# empty-scope-string / whitespace scope via CLI
rc = main(["--ledger", p7b, "status", "--scope", "ghost"])
check("CLI ghost scope exit 2", rc == 2)

# NaN / inf cost
try:
    led7.reserve("micro", float("nan"))
    check("NaN cost rejected", False)
except ValueError:
    check("NaN cost rejected", True)
try:
    led7.reserve("micro", float("inf"))
    check("inf cost rejected", False)
except ValueError:
    check("inf cost rejected", True)

# crash-recovery sweep (round-2 feature)
led9, p9 = tmp_ledger()
led9.init_scope("sweep-test", limit=10.0)
stale = led9.reserve("sweep-test", 0.1)
fresh = led9.reserve("sweep-test", 0.1)
led9._state["tokens"][stale]["ts"] -= 7200  # forge 2h-old reservation
led9._save()
rel = led9.sweep(older_than=3600.0)
check("sweep releases stale only", rel == [stale])
check("sweep clears reserved", led9.status("sweep-test")["reserved"] == 0.1)
led9.release(fresh)
try:
    led9.sweep(older_than=-1)
    check("sweep negative age rejected", False)
except ValueError:
    check("sweep negative age rejected", True)
led10, _ = tmp_ledger()  # fresh ledger, no scopes
check("sweep on empty ledger ok", led10.sweep() == [])
_rc = main(["--ledger", p9, "sweep", "--older-than", "1"])
check("CLI sweep exit 0", _rc == 0)

# perf: reserve+commit cycle overhead (measured, cited in README)
led8, p8 = tmp_ledger()
led8.init_scope("perf", limit=1e9)
N = 500
t0 = time.perf_counter()
for _ in range(N):
    tk = led8.reserve("perf", 0.000001)
    led8.commit(tk, 0.000001)
dt = time.perf_counter() - t0
us_per_op = dt / N * 1e6
PERF.append(f"reserve+commit cycle: {us_per_op:.1f} us/op ({N} ops, incl. 2 atomic writes each)")
print(f"  PERF  reserve+commit cycle: {us_per_op:.1f} us/op over {N} ops")
check("perf measured < 50ms/op", us_per_op < 50_000)

for p in (path, p3, p4, p5, p6, p7, p6b, p7b, p8):
    try:
        os.unlink(p)
    except OSError:
        pass

print(f"\nRESULT: PASS {PASS}/{PASS + FAIL}")
sys.exit(0 if FAIL == 0 else 1)
