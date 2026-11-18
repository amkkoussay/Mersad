"""Regression tests for the 2026-11-18 core review fixes:

- foreign keys actually enforced (the pragma was never set);
- composite writes are atomic (helpers used to commit mid-operation,
  leaving orphan episodes/entities behind failed writes);
- a cross-agent challenge no longer takes currency on arrival — the
  standing value stays current, the challenger waits pending, and the
  owner's resolve_conflict ruling is what changes the current value;
- episodes are indexed and searchable (recall always claimed they
  were; nothing indexed them);
- fact_at resolves aliases like every other query;
- subject_brief assembles a subject's whole story in one call;
- why() ranks by how much of the question a decision answers.
"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(__file__))
from mirsad.core import Mirsad  # noqa: E402

FAILED = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f" — {extra}" if extra and not cond else ""))
    if not cond:
        FAILED.append(name)


def fresh():
    return Mirsad(os.path.join(tempfile.mkdtemp(), "t.db"))


# ---------- foreign keys ----------
m = fresh()
check("foreign_keys pragma is ON",
      m.db.execute("PRAGMA foreign_keys").fetchone()[0] == 1)
try:
    m.db.execute(
        "INSERT INTO facts(subject_id, predicate, object_text, valid_from,"
        " recorded_at, source_agent, episode_id)"
        " VALUES (999999,'p','v',0,0,'a',999999)")
    m.db.commit()
    check("dangling fact reference rejected", False, "insert succeeded")
except Exception:
    m.db.rollback()
    check("dangling fact reference rejected", True)
try:
    m.record_outcome(424242, "outcome for a decision that does not exist", "a")
    check("record_outcome on missing decision raises", False)
except ValueError:
    check("record_outcome on missing decision raises", True)

# ---------- atomicity ----------
m = fresh()
try:
    m.remember_fact("Beta", "size", {"bad": "type"}, source_agent="agent-a",
                    source_ref="ref-1")
    check("bad-type write raises", False)
except Exception:
    check("bad-type write raises", True)
st = m.stats()
check("failed write leaves no orphan episode", st["episodes"] == 0, str(st))
check("failed write leaves no orphan entity", st["entities"] == 0, str(st))
check("failed write leaves no fact", st["facts_total"] == 0, str(st))

# ---------- conflict semantics: the owner rules, not the last writer ----------
m = fresh()
m.remember_fact("Gamma", "capital", "Tunis", source_agent="agent-a")
r = m.remember_fact("Gamma", "capital", "Paris", source_agent="agent-b")
check("cross-agent challenge opens a conflict", r["conflict_opened"] is not None)
check("challenger is stored pending", r["pending"] is True)
cur = m.current_fact("Gamma", "capital")
check("standing value keeps currency", cur["object"] == "Tunis", str(cur))
check("stats count one pending", m.stats()["facts_pending"] == 1)
tl = {f["object"]: f for f in m.timeline("Gamma")}
check("timeline flags the pending challenger",
      tl["Paris"]["pending"] is True and tl["Paris"]["currently_valid"] is False)
res = m.recall("Paris")
check("pending challenger not served as standing knowledge",
      all(f["object"] != "Paris" for f in res["facts"]))
check("recall surfaces the open conflict",
      len(res.get("conflicts", [])) == 1)
r2 = m.remember_fact("Gamma", "capital", "Paris", source_agent="agent-c")
check("re-proposing the same challenger is a duplicate",
      r2.get("duplicate") is True and len(m.open_conflicts()) == 1)

out = m.resolve_conflict(r["conflict_opened"], "Owner ruling: Tunis stands.",
                         winner="standing")
check("ruling for standing keeps Tunis current",
      out["current_value"] == "Tunis" and
      m.current_fact("Gamma", "capital")["object"] == "Tunis")
tl = {f["object"]: f for f in m.timeline("Gamma")}
check("rejected challenger kept in history",
      tl["Paris"]["currently_valid"] is False and tl["Paris"]["pending"] is False)
check("conflict closed after ruling", len(m.open_conflicts()) == 0)

m = fresh()
m.remember_fact("Gamma", "capital", "Tunis", source_agent="agent-a")
r = m.remember_fact("Gamma", "capital", "Paris", source_agent="agent-b")
out = m.resolve_conflict(r["conflict_opened"], "Owner ruling: Paris is right.",
                         winner="challenger")
check("ruling for challenger promotes it",
      out["current_value"] == "Paris" and
      m.current_fact("Gamma", "capital")["object"] == "Paris")
tl = {f["object"]: f for f in m.timeline("Gamma")}
check("demoted standing value kept in history",
      tl["Tunis"]["currently_valid"] is False)
res = m.recall("Paris")
check("promoted challenger becomes searchable knowledge",
      any(f["object"] == "Paris" for f in res["facts"]))
try:
    m.resolve_conflict(r["conflict_opened"], "again")
    check("double resolution raises", False)
except ValueError:
    check("double resolution raises", True)
try:
    m.resolve_conflict(987654, "nobody")
    check("resolving a missing conflict raises", False)
except ValueError:
    check("resolving a missing conflict raises", True)

m = fresh()
m.remember_fact("Delta", "port", "8080", source_agent="agent-a")
r = m.remember_fact("Delta", "port", "9090", source_agent="agent-a")
check("same-agent correction takes currency immediately",
      r["conflict_opened"] is None and
      m.current_fact("Delta", "port")["object"] == "9090")

# ---------- episodes are searchable ----------
m = fresh()
m.add_episode("agent-a", "the zzqwx protocol buffers spatial uploads")
res = m.recall("zzqwx")
check("recall finds text that exists only in an episode",
      any(h["kind"] == "episode" for h in res["text_hits"]),
      str(res["text_hits"]))
path = m.db_path
rows_before = m.db.execute(
    "SELECT COUNT(*) FROM search_index WHERE kind='episode'").fetchone()[0]
m2 = Mirsad(path)  # reopening must not double-index via the migration
rows_after = m2.db.execute(
    "SELECT COUNT(*) FROM search_index WHERE kind='episode'").fetchone()[0]
check("reopening does not double-index episodes", rows_before == rows_after == 1)

# ---------- fact_at resolves names ----------
m = fresh()
m.remember_fact("مرصاد", "النوع", "ذاكرة", source_agent="agent-a")
m.add_alias("مرصاد", "Mirsad")
got = m.fact_at("Mirsad", "النوع", time.time() + 5)
check("fact_at resolves an alias", got is not None and got["object"] == "ذاكرة",
      str(got))
got = m.fact_at("مِرصاد", "النوع", time.time() + 5)
check("fact_at resolves a diacritics variant", got is not None)

# ---------- subject_brief ----------
m = fresh()
m.remember_fact("Proj", "language", "Python", source_agent="agent-a")
m.remember_fact("Proj", "language", "Java", source_agent="agent-b")
m.record_decision("Adopt Python", made_by="owner",
                  reasons=["team knows it"], entities=["Proj"])
b = m.subject_brief("Proj")
check("brief finds the subject", b["found"] is True)
check("brief shows the standing fact",
      [f["object"] for f in b["current_facts"]] == ["Python"])
check("brief shows the pending challenger",
      [f["object"] for f in b["pending_candidates"]] == ["Java"])
check("brief shows the open conflict", len(b["open_conflicts"]) == 1)
check("brief shows linked decisions",
      [d["title"] for d in b["decisions"]] == ["Adopt Python"])
check("brief of unknown subject is explicit",
      m.subject_brief("Nope")["found"] is False)

# ---------- why() ranking ----------
m = fresh()
m.record_decision("Use SQLite for storage", made_by="a",
                  reasons=["single file, zero services"])
m.record_decision("Storage layout notes", made_by="a",
                  reasons=["misc"], description="unrelated housekeeping")
res = m.why("why did we choose SQLite for storage")
check("why ranks the fuller match first",
      res and res[0]["title"] == "Use SQLite for storage",
      str([d["title"] for d in res]))

# ---------- schema ----------
m = fresh()
check("schema version is 3", m.version_info()["schema_version"] == 3)

print()
if FAILED:
    print(f"{len(FAILED)} FAILURES: {FAILED}")
    sys.exit(1)
print("core fixes: all checks passed")
