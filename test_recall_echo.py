"""Regression test: recall() must not echo an unbounded query back in its
result. A 60,000-char query used to come straight back in the "query"
field, so the response was ~60 KB of pure echo (reported by an external
tester against the live deployment, 2026-11-15). The echoed query is now
capped at 200 chars and flagged with query_truncated.

Run: python3 test_recall_echo.py  (exits non-zero on any failure)
"""

import json
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
m.remember_fact("Northwind Clinic", "uses", "Mirsad for shared memory", "agent-a")

big = "z" * 60000
res = m.recall(big)
blob = json.dumps(res, ensure_ascii=False)
check("60K query: full query NOT echoed verbatim", big not in blob)
check("60K query: echoed query capped at 200 chars", len(res["query"]) == 200,
      f"len={len(res['query'])}")
check("60K query: query_truncated flag set", res.get("query_truncated") is True)
check("60K query: whole result stays small", len(blob.encode("utf-8")) < 5000,
      f"bytes={len(blob.encode('utf-8'))}")

res2 = m.recall("Northwind Clinic")
check("short query: echoed in full", res2["query"] == "Northwind Clinic")
check("short query: no truncation flag", "query_truncated" not in res2)
check("short query: still finds the fact",
      any("Northwind" in f.get("subject", "") for f in res2["facts"]))

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("ALL PASS")
