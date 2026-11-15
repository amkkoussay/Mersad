"""Regression test: recall()/why() must survive long and wildcard-laden
queries. User text used to be embedded raw into LIKE patterns, so a query
over SQLite's LIKE pattern limit crashed recall with
"LIKE or GLOB pattern too complex", and % / _ in a query silently acted
as wildcards instead of literal text.

The limit that matters is Cloudflare D1's: 50 BYTES per pattern (verified
empirically 2026-11-15: 50 bytes succeeds, 51 raises) — stock SQLite
allows 50,000, which is why the 0.2.2 fix (a 500-char cap) still crashed
on D1 for ordinary 50-500 char multi-word queries. The core invariant is
therefore tested directly: every LIKE pattern the engine builds is
<= 50 bytes in UTF-8, escapes included.

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

# 7. THE D1 INVARIANT: every pattern _like_pattern builds is <= 50 bytes
# (D1's SQLITE_MAX_LIKE_PATTERN_LENGTH), whatever the input.
NASTY = [
    "MIRSAD 0.2.2 recall LIKE 500 characters escape percent underscore release",
    "x" * 60000,
    "ذاكرة مشتركة للمساعدين " * 3000,
    "%" * 100,
    "_" * 100,
    "\\" * 100,
    "a%b_c\\d" * 40,
    "كلمة" * 30,
    "MIRSAD characters escape percent underscore release",
]
bad = []
for q in NASTY:
    p = Mirsad._like_pattern(q)
    if p is not None and len(p.encode("utf-8")) > 50:
        bad.append((q[:30], len(p.encode("utf-8"))))
check("every LIKE pattern is <= 50 bytes (D1 limit)", not bad, str(bad))

# 8. The exact query an external tester reported failing on the live D1
# copy on 2026-11-15, AFTER the 0.2.2 fix had shipped.
try:
    r = m.recall("MIRSAD 0.2.2 recall LIKE 500 characters escape percent underscore release")
    check("recall survives the tester's exact 71-char query", isinstance(r, dict))
except Exception as e:  # noqa: BLE001
    check("recall survives the tester's exact 71-char query", False, f"{type(e).__name__}: {e}")

# 9. Single tokens around and past D1's 50-byte pattern limit.
for n in (49, 50, 200):
    try:
        r = m.recall("z" * n)
        check(f"recall survives a {n}-char single token", isinstance(r, dict))
    except Exception as e:  # noqa: BLE001
        check(f"recall survives a {n}-char single token", False, f"{type(e).__name__}: {e}")

# 10. version_info reports the running build's identity.
vi = m.version_info()
check("version_info returns build identity",
      bool(vi.get("package_version")) and vi.get("schema_version") == 2
      and vi.get("runtime") == "local", str(vi))

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("ALL PASS")
