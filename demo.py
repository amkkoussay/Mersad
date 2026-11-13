"""MIRSAD acceptance demo — every check can fail; exit code is non-zero
if any check fails. Scenario: three different assistants (agent-a, agent-b,
agent-c) share ONE memory about a project, exactly the collective-memory use
case MIRSAD is built for."""

import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mirsad.core import Mirsad

FAILURES = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


db = os.path.join(tempfile.mkdtemp(), "demo.db")
m = Mirsad(db)

# 1) Agent-a records a decision as one unit (the classic example, extended)
d1 = m.record_decision(
    "Use Redis for caching",
    made_by="agent-a",
    description="Cache layer for the project API",
    reasons=["Cut response latency"],
    evidence=[{"text": "Benchmark on 2026-11-10: p95 latency dropped after adding Redis",
               "ref": "benchmark-2026-11-10"}],
    alternatives_rejected=["Memcached — rejected: no persistence, weaker data structures"],
    effects=["cache.py"],
    entities=["Redis", "Project"],
    episode="User reported slow responses; benchmark confirmed cache misses as the cause.",
)
m.record_outcome(d1, "Deployed; p95 latency improved in production", recorded_by="agent-c")

# 2) The matching fact is stored in the SAME memory, alongside the decision
m.remember_fact("Project", "cache_backend", "Redis", source_agent="agent-a",
                episode="Recorded with the decision above.")

# 3) Agent-a asks: why did we choose Redis? — must get the full chain
chain = m.why("Redis")
check("why() finds the decision", len(chain) == 1, f"got {len(chain)}")
if chain:
    d = chain[0]
    check("why() returns the reason", d["reasons"] == ["Cut response latency"])
    check("why() returns the evidence", len(d["evidence"]) == 1 and d["evidence"][0]["ref"] == "benchmark-2026-11-10")
    check("why() returns the rejected alternative", any("Memcached" in a for a in d["alternatives_rejected"]))
    check("why() returns the effect", d["effects"] == ["cache.py"])
    check("why() returns the outcome", len(d["outcomes"]) == 1)

# 3b) Name resolution: an Arabic alias + a full Arabic question must work
# (this is the exact failure an Arabic-script query hit before the fix).
m.add_alias("Project", "المشروع")
tl_alias = m.timeline("المشروع")
check("timeline() resolves an Arabic alias", len(tl_alias) == 1 and tl_alias[0]["object"] == "Redis")
chain_ar = m.why("لماذا اخترنا Redis بالذات؟")
check("why() understands a full Arabic question", len(chain_ar) == 1 and chain_ar[0]["id"] == d1)
rec_alias = m.recall("المشروع")
check("recall() finds facts through the alias", len(rec_alias["facts"]) >= 1)

# 4) Same-agent update: TTL 60 -> 300. Old value closed, not deleted.
m.remember_fact("Redis config", "ttl_seconds", "60", source_agent="agent-a")
t_mid = time.time()
time.sleep(0.01)
m.remember_fact("Redis config", "ttl_seconds", "300", source_agent="agent-a")
cur = m.current_fact("Redis config", "ttl_seconds")
check("current TTL is 300", cur is not None and cur["object"] == "300")
tl = m.timeline("Redis config")
check("timeline keeps both values", len(tl) == 2)
check("old TTL was invalidated, not deleted",
      tl[0]["object"] == "60" and tl[0]["valid_to"] is not None)
past = m.fact_at("Redis config", "ttl_seconds", t_mid)
check("fact_at() answers 'what was true then'", past is not None and past["object"] == "60")
check("same-agent update opens no conflict", len(m.open_conflicts()) == 0)

# 5) Cross-agent contradiction: agent-b overwrites agent-a's active fact.
m.remember_fact("Project", "cache_backend", "Memcached", source_agent="agent-b")
conf = m.open_conflicts()
check("cross-agent overwrite opens exactly one conflict", len(conf) == 1, f"got {len(conf)}")
cur = m.current_fact("Project", "cache_backend")
check("latest value wins for current queries", cur is not None and cur["object"] == "Memcached")
tl = m.timeline("Project")
check("contradicted fact is preserved in history",
      any(f["object"] == "Redis" and f["valid_to"] is not None for f in tl))

# 6) User resolves the conflict; the ruling is kept on record.
if conf:
    m.resolve_conflict(conf[0]["id"], "User ruling: Redis is correct; agent-b had stale context.")
check("conflict resolved", len(m.open_conflicts()) == 0)

# 7) A later decision supersedes the first; the chain stays walkable.
d2 = m.record_decision(
    "Raise Redis TTL from 60 to 300 seconds",
    made_by="agent-c",
    reasons=["Cache hit rate too low at 60s"],
    entities=["Redis"],
    supersedes=d1,
)
old = m.get_decision(d1)
check("superseded decision keeps its status", old["status"] == "superseded" and old["superseded_by"] == d2)

# 8) Markdown mirror renders decisions + current facts + stats are sane.
md = m.export_markdown()
check("markdown mirror contains the decision", "Use Redis for caching" in md)
check("markdown mirror contains the reason", "Cut response latency" in md)
st = m.stats()
check("stats count decisions", st["decisions"] == 2, str(st))
check("stats count current facts", st["facts_current"] == 2, str(st))

print()
print("STATS:", st)
print("RESULT:", "ALL PASS" if not FAILURES else f"{len(FAILURES)} FAILURES: {FAILURES}")
sys.exit(1 if FAILURES else 0)
