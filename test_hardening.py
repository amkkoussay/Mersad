"""Regression tests for the 2026-11-15 hardening batch.

Each test pins one weakness found in external review; every one of them
fails against the pre-fix code:
  1. single shared SQLite connection -> ProgrammingError across threads
  2. rewriting the same fact duplicated the active row (+ its FTS row)
  3. search_index rows were never deleted (stale recall hits forever)
  4. name resolution was a full table scan compared in Python
  5. graph_snapshot rebuilt everything (N+1 queries, full details) per poll
  7. no schema migration path existed (SCHEMA_VERSION stuck at 1)

Run: python3 test_hardening.py   (exit 0 = all pass)
"""

import json
import os
import sqlite3
import sys
import tempfile
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mirsad.core import Mirsad, SCHEMA, SCHEMA_VERSION  # noqa: E402


def tmpdb() -> str:
    return os.path.join(tempfile.mkdtemp(), "t.db")


def test_threaded_access():
    m = Mirsad(tmpdb())
    errors: list[str] = []

    def worker(n: int):
        try:
            for i in range(15):
                m.remember_fact(f"Entity{n}", "reading", f"v{i}",
                                source_agent=f"agent{n}")
                m.current_fact(f"Entity{n}", "reading")
                m.recall("reading")
        except Exception as e:  # noqa: BLE001
            errors.append(f"{type(e).__name__}: {e}")

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, f"cross-thread errors: {errors[:2]}"
    for n in range(4):
        assert m.current_fact(f"Entity{n}", "reading")["object"] == "v14"
    mode = m.db.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode == "wal", f"journal_mode is {mode}, expected wal"
    print("PASS threaded access (4 threads x 15 writes, WAL on)")


def test_duplicate_fact_idempotent():
    m = Mirsad(tmpdb())
    r1 = m.remember_fact("S", "color", "blue", source_agent="a")
    r2 = m.remember_fact("S", "color", "blue", source_agent="a")
    assert r2.get("duplicate") is True, f"second write not flagged: {r2}"
    assert r2["fact_id"] == r1["fact_id"]
    active = m.db.execute(
        "SELECT COUNT(*) FROM facts WHERE valid_to IS NULL").fetchone()[0]
    assert active == 1, f"{active} active facts after duplicate write"
    idx = m.db.execute("SELECT COUNT(*) FROM search_index").fetchone()[0]
    assert idx == 1, f"{idx} search_index rows after duplicate write"
    # a different agent repeating the same value is agreement, not conflict
    r3 = m.remember_fact("S", "color", "blue", source_agent="b")
    assert r3.get("duplicate") is True and r3["conflict_opened"] is None
    assert m.stats()["open_conflicts"] == 0
    print("PASS duplicate fact write is idempotent")


def test_fts_follows_validity():
    m = Mirsad(tmpdb())
    m.remember_fact("S", "color", "blue", source_agent="a")
    m.remember_fact("S", "color", "green", source_agent="a")
    bodies = [r[0] for r in m.db.execute("SELECT body FROM search_index")]
    assert bodies == ["green"], f"search_index holds {bodies}"
    hits = [h["body"] for h in m.recall("blue")["text_hits"]]
    assert "blue" not in hits, f"stale hit survived: {hits}"
    assert "green" in [h["body"] for h in m.recall("green")["text_hits"]]
    # superseded decisions leave the index too
    d1 = m.record_decision("Use SQLite", made_by="a",
                           reasons=["zero-config"])
    m.record_decision("Use Postgres", made_by="a", supersedes=d1,
                      reasons=["scale"])
    titles = [r[0] for r in m.db.execute("SELECT title FROM search_index")]
    assert "Use SQLite" not in titles and "Use Postgres" in titles, titles
    print("PASS search_index tracks validity (facts + decisions)")


def test_name_resolution_indexed():
    m = Mirsad(tmpdb())
    m.entity("مِرصاد")  # Arabic with diacritics
    m.entity("PostgreSQL")
    m.add_alias("PostgreSQL", "postgres")
    assert m.resolve_subject("مرصاد") == "مِرصاد"      # diacritic-free
    assert m.resolve_subject("postgresql") == "PostgreSQL"  # case variant
    assert m.resolve_subject("POSTGRES") == "PostgreSQL"    # alias, shouted
    rows = m.db.execute(
        "SELECT COUNT(*) FROM entities WHERE name_norm IS NULL").fetchone()[0]
    assert rows == 0, "entities with unpopulated name_norm"
    plan = " ".join(
        r["detail"] for r in m.db.execute(
            "EXPLAIN QUERY PLAN SELECT name FROM entities WHERE name_norm=?",
            ("x",)))
    assert "SEARCH" in plan and "SCAN entities" not in plan, plan
    print("PASS name resolution via indexed normalized columns")


def test_migration_from_v1():
    path = tmpdb()
    raw = sqlite3.connect(path)
    raw.executescript(SCHEMA)  # the v1 layout: no *_norm / fts_rowid columns
    raw.execute("INSERT INTO entities(id, name, type, created_at) "
                "VALUES (1, 'Legacy', 'thing', 1.0)")
    raw.execute("INSERT INTO facts(id, subject_id, predicate, object_text,"
                " valid_from, recorded_at, source_agent) VALUES"
                " (1, 1, 'color', 'blue', 1.0, 1.0, 'a'),"
                " (2, 1, 'color', 'green', 2.0, 2.0, 'a')")
    raw.execute("UPDATE facts SET valid_to=2.0, expired_at=2.0 WHERE id=1")
    raw.execute("INSERT INTO search_index(kind, title, body) VALUES"
                " ('fact', 'Legacy color', 'blue'),"
                " ('fact', 'Legacy color', 'green'),"
                " ('fact', 'Legacy color', 'blue')")  # stale + duplicate rows
    raw.commit()
    raw.close()

    m = Mirsad(path)  # opening must migrate, not crash
    assert SCHEMA_VERSION == 2
    assert m.version_info()["schema_version"] == 2
    assert m.current_fact("Legacy", "color")["object"] == "green"
    bodies = [r[0] for r in m.db.execute("SELECT body FROM search_index")]
    assert bodies == ["green"], f"legacy index not rebuilt: {bodies}"
    cols = {r["name"] for r in m.db.execute("PRAGMA table_info(entities)")}
    assert "name_norm" in cols
    assert m.resolve_subject("legacy") == "Legacy"  # backfilled norm works
    # and the migrated DB keeps working normally afterwards
    m.remember_fact("Legacy", "color", "red", source_agent="a")
    assert m.current_fact("Legacy", "color")["object"] == "red"
    print("PASS v1 database migrates to v2 (index rebuilt, data intact)")


def test_graph_light_and_detail():
    m = Mirsad(tmpdb())
    m.remember_fact("Alpha", "uses", "Beta", source_agent="a")
    m.remember_fact("Alpha", "status", "active", source_agent="a")
    did = m.record_decision("Ship it", made_by="a", reasons=["ready"],
                            entities=["Alpha"])
    full = m.graph_snapshot()
    light = m.graph_snapshot(light=True)
    assert "details" in full and "details" not in light
    assert len(full["nodes"]) == len(light["nodes"])
    assert len(full["edges"]) == len(light["edges"])
    assert len(json.dumps(light)) < len(json.dumps(full))
    ent_id = m._resolve_entity_id("Alpha")
    det = m.graph_node_detail(f"ent:{ent_id}")
    assert det["kind"] == "entity" and len(det["facts"]) == 2
    assert det["decisions"] == ["Ship it"]
    ddet = m.graph_node_detail(f"dec:{did}")
    assert ddet["kind"] == "decision" and ddet["reasons"] == ["ready"]
    assert m.graph_node_detail("val:1") is None  # derived client-side
    assert m.graph_node_detail("ent:9999") is None
    print("PASS graph snapshot light mode + on-demand node detail")


def test_server_binds_loopback_by_default():
    """The MCP server must not listen on all interfaces by default:
    its tools read/write the whole memory with no auth layer."""
    import subprocess
    env = dict(os.environ, MIRSAD_DB=tmpdb())
    env.pop("MIRSAD_HOST", None)
    out = subprocess.run(
        [sys.executable, "-c",
         "import mirsad.mcp_server as s; print(s.mcp.settings.host)"],
        capture_output=True, text=True, env=env, timeout=60,
        cwd=os.path.dirname(os.path.abspath(__file__)))
    assert out.returncode == 0, out.stderr[-400:]
    assert out.stdout.strip() == "127.0.0.1", out.stdout
    print("PASS MCP server binds loopback by default")


if __name__ == "__main__":
    test_threaded_access()
    test_duplicate_fact_idempotent()
    test_fts_follows_validity()
    test_name_resolution_indexed()
    test_migration_from_v1()
    test_graph_light_and_detail()
    test_server_binds_loopback_by_default()
    print("\nALL HARDENING TESTS PASS")
