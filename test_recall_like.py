"""Regression test: recall()/why() must survive long and wildcard-laden
queries. User text used to be embedded raw into LIKE patterns, so a query
over SQLite's LIKE pattern limit (50,000 bytes) crashed recall with
"LIKE or GLOB pattern too complex", and % / _ in a query silently acted
as wildcards instead of literal text.

Run: python3 test_recall_like.py  (exits non-zero on any failure)
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mirsad.core import Mirsad  # noqa: E402

failures = []


def check(name, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + name + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        failures.append(name)


db = os.path.join(tempfile.mkdtemp(), "t.db")
m = Mirsad(db)
m.remember_fact("Northwind Clinic", "coverage", "coverage reached 50% of clinics", "agent-a")
m.remember_fact("Northwind Clinic", "intake", "coverage reached 500 clinics", "agent-a")
m.remember_fact("MIRSAD", "layout", "uses shared_memory layout", "agent-a")
m.remember_fact("MIRSAD", "summary", "a shared memory hub for assistants", "agent-a")
m.record_decision("Adopt shared memory", made_by="agent-a",
                  reasons=["one memory for all assistants"])

# 1. A query past the LIKE pattern limit must not raise.
try:
    r = m.recall("x" * 60000)
    check("recall survives a 60,000-char query", isinstance(r, dict))
except Exception as e:  # noqa: BLE001
    check("recall survives a 60,000-char query", False, f"{type(e).__name__}: {e}")

# 2. Same for a long Arabic query (2 bytes/char in UTF-8 -> past the limit sooner).
try:
    r = m.recall("ذاكرة مشتركة للمساعدين " * 3000)
    check("recall survives a long Arabic query", isinstance(r, dict))
except Exception as e:  # noqa: BLE001
    check("recall survives a long Arabic query", False, f"{type(e).__name__}: {e}")

# 3. why() with an over-limit topic must not raise either (recall calls it).
try:
    r = m.why("y" * 60000)
    check("why survives a 60,000-char topic", isinstance(r, list))
except Exception as e:  # noqa: BLE001
    check("why survives a 60,000-char topic", False, f"{type(e).__name__}: {e}")

# 4. % in a query is literal: "50%" finds the 50% fact, not the 500 one.
r = m.recall("50%")
objs = [f["object"] for f in r["facts"]]
check('recall("50%") finds the literal 50% fact', "coverage reached 50% of clinics" in objs, str(objs))
check('recall("50%") does not wildcard-match the 500 fact',
      "coverage reached 500 clinics" not in objs, str(objs))

# 5. _ in a query is literal: "shared_memory" finds only the underscore fact.
r = m.recall("shared_memory")
objs = [f["object"] for f in r["facts"]]
check('recall("shared_memory") finds the underscore fact',
      "uses shared_memory layout" in objs, str(objs))
check('recall("shared_memory") does not wildcard-match the spaced fact',
      "a shared memory hub for assistants" not in objs, str(objs))

# 6. Ordinary searches still work.
r = m.recall("shared memory")
check('recall("shared memory") still finds facts', len(r["facts"]) >= 1)
r = m.recall("MIRSAD")
check('recall("MIRSAD") still finds facts', len(r["facts"]) >= 1)
r = m.recall("shared memory")
check("recall still returns matching decisions", len(r["decisions"]) >= 1)

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("ALL PASS")
