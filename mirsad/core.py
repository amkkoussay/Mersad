"""MIRSAD core engine — temporal knowledge graph + first-class decisions.

Design lessons applied (see ../research/):
- Graphiti: bi-temporal facts (valid time vs recorded time), facts are
  invalidated, never deleted; every fact traces to a raw episode.
- Cognee: contradictions become explicit conflict records, not silent merges.
- Mem0: every record carries who wrote it (source_agent attribution).
- Letta / Basic Memory: a human-readable Markdown mirror is always exportable.
- graph-agent-memory: reasoning/decisions are a first-class layer.

What none of them do, and MIRSAD does:
- A DECISION is one stored unit: decision -> reasons -> evidence ->
  rejected alternatives -> affected files -> outcome, with a supersedes chain.
- Contradiction handling is deterministic (no LLM in the write path):
  a newer value for the same subject+predicate closes the older fact's
  validity window. If a *different agent* overwrites another agent's active
  fact, a conflict is opened for the user instead of passing silently.
- Zero external services: one SQLite file. No LLM and no API key are needed
  to write or read the graph; an LLM is only the *client* (via MCP/CLI).
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS entities (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    type TEXT NOT NULL DEFAULT 'thing',
    created_at REAL NOT NULL,
    UNIQUE(name, type)
);
CREATE TABLE IF NOT EXISTS episodes (
    id INTEGER PRIMARY KEY,
    source_agent TEXT NOT NULL,
    source_ref TEXT,
    content TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS facts (
    id INTEGER PRIMARY KEY,
    subject_id INTEGER NOT NULL REFERENCES entities(id),
    predicate TEXT NOT NULL,
    object_text TEXT NOT NULL,
    object_id INTEGER REFERENCES entities(id),
    valid_from REAL NOT NULL,
    valid_to REAL,
    recorded_at REAL NOT NULL,
    expired_at REAL,
    source_agent TEXT NOT NULL,
    episode_id INTEGER REFERENCES episodes(id),
    confidence REAL NOT NULL DEFAULT 1.0
);
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    description TEXT,
    status TEXT NOT NULL DEFAULT 'accepted',  -- proposed|accepted|rejected|superseded
    made_by TEXT NOT NULL,
    made_at REAL NOT NULL,
    episode_id INTEGER REFERENCES episodes(id),
    supersedes_id INTEGER REFERENCES decisions(id),
    superseded_by_id INTEGER REFERENCES decisions(id)
);
CREATE TABLE IF NOT EXISTS decision_items (
    id INTEGER PRIMARY KEY,
    decision_id INTEGER NOT NULL REFERENCES decisions(id),
    kind TEXT NOT NULL,  -- reason|evidence|alternative|effect|outcome
    text TEXT NOT NULL,
    ref TEXT,
    recorded_at REAL NOT NULL,
    recorded_by TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS decision_entities (
    decision_id INTEGER NOT NULL REFERENCES decisions(id),
    entity_id INTEGER NOT NULL REFERENCES entities(id),
    PRIMARY KEY (decision_id, entity_id)
);
CREATE TABLE IF NOT EXISTS conflicts (
    id INTEGER PRIMARY KEY,
    subject TEXT NOT NULL,
    predicate TEXT NOT NULL,
    old_fact_id INTEGER REFERENCES facts(id),
    new_fact_id INTEGER REFERENCES facts(id),
    detail TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',  -- open|resolved
    resolution TEXT,
    created_at REAL NOT NULL,
    resolved_at REAL
);
CREATE TABLE IF NOT EXISTS meta (
    k TEXT PRIMARY KEY,
    v INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS entity_aliases (
    entity_id INTEGER NOT NULL REFERENCES entities(id),
    alias TEXT NOT NULL,
    PRIMARY KEY (entity_id, alias)
);
CREATE VIRTUAL TABLE IF NOT EXISTS search_index USING fts5(
    kind, title, body, tokenize='unicode61'
);
"""


def _now() -> float:
    return time.time()


def fmt_ts(ts: float | None) -> str | None:
    if ts is None:
        return None
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


class Mirsad:
    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        self.db = sqlite3.connect(self.db_path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()

    # ---------- entities & episodes ----------

    def entity(self, name: str, type: str = "thing") -> int:
        row = self.db.execute(
            "SELECT id FROM entities WHERE name=? AND type=?", (name, type)
        ).fetchone()
        if row:
            return row["id"]
        cur = self.db.execute(
            "INSERT INTO entities(name, type, created_at) VALUES (?,?,?)",
            (name, type, _now()),
        )
        self.db.commit()
        return cur.lastrowid

    def _entity_name(self, entity_id: int) -> str:
        row = self.db.execute(
            "SELECT name FROM entities WHERE id=?", (entity_id,)
        ).fetchone()
        return row["name"] if row else f"#{entity_id}"

    def _bump(self) -> None:
        self.db.execute(
            """INSERT INTO meta(k, v) VALUES ('graph_version', 1)
               ON CONFLICT(k) DO UPDATE SET v = v + 1"""
        )

    def graph_version(self) -> int:
        row = self.db.execute(
            "SELECT v FROM meta WHERE k='graph_version'").fetchone()
        return row["v"] if row else 0

    def add_episode(self, source_agent: str, content: str, source_ref: str | None = None) -> int:
        self._bump()
        cur = self.db.execute(
            "INSERT INTO episodes(source_agent, source_ref, content, created_at) VALUES (?,?,?,?)",
            (source_agent, source_ref, content, _now()),
        )
        self.db.commit()
        return cur.lastrowid

    def _index(self, kind: str, title: str, body: str) -> None:
        self.db.execute(
            "INSERT INTO search_index(kind, title, body) VALUES (?,?,?)",
            (kind, title, body),
        )

    # ---------- names: normalization, aliases, resolution ----------

    _AR_MARKS = None  # compiled lazily in _norm

    @staticmethod
    def _norm(s: str) -> str:
        import re

        if Mirsad._AR_MARKS is None:
            Mirsad._AR_MARKS = re.compile(r"[\u064b-\u065f\u0670\u0640]")
        return Mirsad._AR_MARKS.sub("", (s or "").lower()).strip()

    _STOPWORDS = {
        "لماذا", "ليه", "ما", "ماذا", "هو", "هي", "اخترنا", "بنينا", "تم",
        "بناء", "انشاء", "سبب", "هدف", "the", "why", "did", "we", "build",
        "built", "choose", "chose", "chosen", "what", "was", "were", "is",
        "it", "our", "use",
    }

    @classmethod
    def _topic_tokens(cls, topic: str) -> list[str]:
        import re

        parts = re.split(r"[\s؟?.,،:;!«»\"'()]+", cls._norm(topic))
        return sorted({t for t in parts if len(t) >= 2 and t not in cls._STOPWORDS})

    def add_alias(self, entity_name: str, alias: str) -> None:
        self._bump()
        eid = self.entity(entity_name)
        self.db.execute(
            "INSERT OR IGNORE INTO entity_aliases(entity_id, alias) VALUES (?,?)",
            (eid, alias),
        )
        self.db.commit()

    def resolve_subject(self, name: str) -> str | None:
        """Resolve a name as an agent wrote it (any script, diacritics, case)
        to the canonical stored entity name, via exact match, normalized
        match, or a registered alias."""
        row = self.db.execute(
            "SELECT name FROM entities WHERE name=?", (name,)
        ).fetchone()
        if row:
            return row["name"]
        target = self._norm(name)
        if not target:
            return None
        for e in self.db.execute("SELECT name FROM entities").fetchall():
            if self._norm(e["name"]) == target:
                return e["name"]
        for a in self.db.execute(
            """SELECT a.alias AS alias, e.name AS canonical FROM entity_aliases a
               JOIN entities e ON e.id = a.entity_id"""
        ).fetchall():
            if self._norm(a["alias"]) == target:
                return a["canonical"]
        return None

    # ---------- facts (bi-temporal) ----------

    def remember_fact(
        self,
        subject: str,
        predicate: str,
        object_text: str,
        source_agent: str,
        subject_type: str = "thing",
        valid_from: float | None = None,
        source_ref: str | None = None,
        episode: str | None = None,
        confidence: float = 1.0,
    ) -> dict:
        """Store a fact. If an active fact exists for the same
        subject+predicate with a different value, it is invalidated
        (valid_to closed, expired_at stamped) — never deleted.
        A cross-agent overwrite opens a conflict for the user."""
        self._bump()
        valid_from = valid_from if valid_from is not None else _now()
        ep_id = self.add_episode(source_agent, episode or f"{subject} {predicate} {object_text}", source_ref) \
            if (episode or source_ref) else None
        sid = self.entity(subject, subject_type)

        old = self.db.execute(
            """SELECT * FROM facts WHERE subject_id=? AND predicate=?
               AND valid_to IS NULL ORDER BY recorded_at DESC LIMIT 1""",
            (sid, predicate),
        ).fetchone()

        superseded = None
        conflict_id = None
        if old and old["object_text"] != object_text:
            self.db.execute(
                "UPDATE facts SET valid_to=?, expired_at=? WHERE id=?",
                (valid_from, _now(), old["id"]),
            )
            superseded = old["id"]
            if old["source_agent"] != source_agent:
                cur = self.db.execute(
                    """INSERT INTO conflicts(subject, predicate, old_fact_id, new_fact_id,
                       detail, created_at) VALUES (?,?,?,?,?,?)""",
                    (
                        subject,
                        predicate,
                        old["id"],
                        None,  # filled below after insert of the new fact
                        f"{old['source_agent']} recorded '{old['object_text']}'; "
                        f"{source_agent} now records '{object_text}'. Latest wins for "
                        f"current queries, but the disagreement is kept for review.",
                        _now(),
                    ),
                )
                conflict_id = cur.lastrowid

        cur = self.db.execute(
            """INSERT INTO facts(subject_id, predicate, object_text, valid_from,
               recorded_at, source_agent, episode_id, confidence)
               VALUES (?,?,?,?,?,?,?,?)""",
            (sid, predicate, object_text, valid_from, _now(), source_agent, ep_id, confidence),
        )
        new_id = cur.lastrowid
        if conflict_id:
            self.db.execute(
                "UPDATE conflicts SET new_fact_id=? WHERE id=?", (new_id, conflict_id)
            )
        self._index("fact", f"{subject} {predicate}", object_text)
        self.db.commit()
        return {
            "fact_id": new_id,
            "superseded_fact_id": superseded,
            "conflict_opened": conflict_id,
        }

    def current_fact(self, subject: str, predicate: str) -> dict | None:
        subject = self.resolve_subject(subject) or subject
        row = self.db.execute(
            """SELECT f.*, e.name AS subject FROM facts f
               JOIN entities e ON e.id = f.subject_id
               WHERE e.name=? AND f.predicate=? AND f.valid_to IS NULL
               ORDER BY f.recorded_at DESC LIMIT 1""",
            (subject, predicate),
        ).fetchone()
        return self._fact_dict(row) if row else None

    def fact_at(self, subject: str, predicate: str, at: float) -> dict | None:
        """What was true at time `at` (world time), per current knowledge."""
        row = self.db.execute(
            """SELECT f.*, e.name AS subject FROM facts f
               JOIN entities e ON e.id = f.subject_id
               WHERE e.name=? AND f.predicate=? AND f.valid_from<=?
               AND (f.valid_to IS NULL OR f.valid_to>?)
               ORDER BY f.recorded_at DESC LIMIT 1""",
            (subject, predicate, at, at),
        ).fetchone()
        return self._fact_dict(row) if row else None

    def timeline(self, subject: str) -> list[dict]:
        subject = self.resolve_subject(subject) or subject
        rows = self.db.execute(
            """SELECT f.*, e.name AS subject FROM facts f
               JOIN entities e ON e.id = f.subject_id
               WHERE e.name=? ORDER BY f.valid_from, f.recorded_at""",
            (subject,),
        ).fetchall()
        return [self._fact_dict(r) for r in rows]

    def _fact_dict(self, row: sqlite3.Row) -> dict:
        return {
            "id": row["id"],
            "subject": row["subject"],
            "predicate": row["predicate"],
            "object": row["object_text"],
            "valid_from": fmt_ts(row["valid_from"]),
            "valid_to": fmt_ts(row["valid_to"]),
            "recorded_at": fmt_ts(row["recorded_at"]),
            "expired_at": fmt_ts(row["expired_at"]),
            "source_agent": row["source_agent"],
            "confidence": row["confidence"],
            "currently_valid": row["valid_to"] is None,
        }

    # ---------- decisions (first-class) ----------

    def record_decision(
        self,
        title: str,
        made_by: str,
        description: str = "",
        reasons: list[str] | None = None,
        evidence: list[dict] | None = None,
        alternatives_rejected: list[str] | None = None,
        effects: list[str] | None = None,
        entities: list[str] | None = None,
        status: str = "accepted",
        supersedes: int | None = None,
        episode: str | None = None,
    ) -> int:
        self._bump()
        ep_id = self.add_episode(made_by, episode or f"Decision: {title}") if episode else None
        cur = self.db.execute(
            """INSERT INTO decisions(title, description, status, made_by, made_at,
               episode_id, supersedes_id) VALUES (?,?,?,?,?,?,?)""",
            (title, description, status, made_by, _now(), ep_id, supersedes),
        )
        did = cur.lastrowid
        if supersedes:
            self.db.execute(
                "UPDATE decisions SET status='superseded', superseded_by_id=? WHERE id=?",
                (did, supersedes),
            )
        items: list[tuple[str, str, str | None]] = []
        for r in reasons or []:
            items.append(("reason", r, None))
        for ev in evidence or []:
            items.append(("evidence", ev.get("text", ""), ev.get("ref")))
        for alt in alternatives_rejected or []:
            items.append(("alternative", alt, None))
        for eff in effects or []:
            items.append(("effect", eff, None))
        for kind, text, ref in items:
            self.db.execute(
                """INSERT INTO decision_items(decision_id, kind, text, ref, recorded_at, recorded_by)
                   VALUES (?,?,?,?,?,?)""",
                (did, kind, text, ref, _now(), made_by),
            )
        for name in entities or []:
            eid = self.entity(name)
            self.db.execute(
                "INSERT OR IGNORE INTO decision_entities(decision_id, entity_id) VALUES (?,?)",
                (did, eid),
            )
        self._index(
            "decision",
            title,
            " ".join([description] + [t for _, t, _ in items]),
        )
        self.db.commit()
        return did

    def record_outcome(self, decision_id: int, outcome: str, recorded_by: str) -> None:
        self._bump()
        self.db.execute(
            """INSERT INTO decision_items(decision_id, kind, text, recorded_at, recorded_by)
               VALUES (?, 'outcome', ?, ?, ?)""",
            (decision_id, outcome, _now(), recorded_by),
        )
        self.db.commit()

    def get_decision(self, decision_id: int) -> dict | None:
        row = self.db.execute(
            "SELECT * FROM decisions WHERE id=?", (decision_id,)
        ).fetchone()
        if not row:
            return None
        items = self.db.execute(
            "SELECT kind, text, ref, recorded_by FROM decision_items WHERE decision_id=? ORDER BY id",
            (decision_id,),
        ).fetchall()
        ents = self.db.execute(
            """SELECT e.name FROM entities e JOIN decision_entities de
               ON de.entity_id=e.id WHERE de.decision_id=?""",
            (decision_id,),
        ).fetchall()
        d = {
            "id": row["id"],
            "title": row["title"],
            "description": row["description"],
            "status": row["status"],
            "made_by": row["made_by"],
            "made_at": fmt_ts(row["made_at"]),
            "supersedes": row["supersedes_id"],
            "superseded_by": row["superseded_by_id"],
            "entities": [e["name"] for e in ents],
            "reasons": [],
            "evidence": [],
            "alternatives_rejected": [],
            "effects": [],
            "outcomes": [],
        }
        for it in items:
            if it["kind"] == "reason":
                d["reasons"].append(it["text"])
            elif it["kind"] == "evidence":
                d["evidence"].append({"text": it["text"], "ref": it["ref"]})
            elif it["kind"] == "alternative":
                d["alternatives_rejected"].append(it["text"])
            elif it["kind"] == "effect":
                d["effects"].append(it["text"])
            elif it["kind"] == "outcome":
                d["outcomes"].append({"text": it["text"], "by": it["recorded_by"]})
        return d

    def why(self, topic: str) -> list[dict]:
        """Answer 'why did we choose X?' — decisions whose title, description,
        items or linked entities match the topic, oldest first, each with its
        full chain (reasons, evidence, rejected alternatives, outcomes)."""
        rows = self.db.execute(
            """SELECT DISTINCT d.id FROM decisions d
               LEFT JOIN decision_entities de ON de.decision_id = d.id
               LEFT JOIN entities e ON e.id = de.entity_id
               WHERE d.title LIKE ? OR d.description LIKE ? OR e.name LIKE ?
               ORDER BY d.made_at""",
            (f"%{topic}%", f"%{topic}%", f"%{topic}%"),
        ).fetchall()
        if not rows:  # fall back to item text
            rows = self.db.execute(
                """SELECT DISTINCT d.id FROM decisions d
                   JOIN decision_items di ON di.decision_id = d.id
                   WHERE di.text LIKE ? ORDER BY d.made_at""",
                (f"%{topic}%",),
            ).fetchall()
        if not rows:  # last resort: word by word + alias resolution
            ids: list[int] = []
            for tok in self._topic_tokens(topic):
                like = f"%{tok}%"
                for r in self.db.execute(
                    """SELECT DISTINCT d.id FROM decisions d
                       LEFT JOIN decision_items di ON di.decision_id = d.id
                       WHERE d.title LIKE ? OR d.description LIKE ? OR di.text LIKE ?""",
                    (like, like, like),
                ).fetchall():
                    if r["id"] not in ids:
                        ids.append(r["id"])
                canonical = self.resolve_subject(tok)
                if canonical:
                    for r in self.db.execute(
                        """SELECT DISTINCT d.id FROM decisions d
                           JOIN decision_entities de ON de.decision_id = d.id
                           JOIN entities e ON e.id = de.entity_id
                           WHERE e.name = ?""",
                        (canonical,),
                    ).fetchall():
                        if r["id"] not in ids:
                            ids.append(r["id"])
            found = [self.get_decision(i) for i in ids]
            found.sort(key=lambda d: d["made_at"] or "")
            return found
        return [self.get_decision(r["id"]) for r in rows]

    # ---------- conflicts ----------

    def open_conflicts(self) -> list[dict]:
        rows = self.db.execute(
            "SELECT * FROM conflicts WHERE status='open' ORDER BY created_at"
        ).fetchall()
        return [
            {
                "id": r["id"],
                "subject": r["subject"],
                "predicate": r["predicate"],
                "detail": r["detail"],
                "created_at": fmt_ts(r["created_at"]),
            }
            for r in rows
        ]

    def resolve_conflict(self, conflict_id: int, resolution: str) -> None:
        self._bump()
        self.db.execute(
            "UPDATE conflicts SET status='resolved', resolution=?, resolved_at=? WHERE id=?",
            (resolution, _now(), conflict_id),
        )
        self.db.commit()

    # ---------- recall (hybrid: FTS + graph neighbours) ----------

    def recall(self, query: str, limit: int = 10) -> dict:
        toks = self._topic_tokens(query)
        fts_q = " OR ".join(f'"{t}"' for t in toks) if toks else f'"{query}"'
        hits = self.db.execute(
            """SELECT kind, title, body FROM search_index
               WHERE search_index MATCH ? LIMIT ?""",
            (fts_q, limit),
        ).fetchall() if query.strip() else []
        canonical = self.resolve_subject(query)
        terms = list(dict.fromkeys([query, canonical, *toks]))
        terms = [t for t in terms if t]
        cond = " OR ".join("(e.name LIKE ? OR f.object_text LIKE ?)" for _ in terms)
        binds: list = []
        for t in terms:
            binds += [f"%{t}%", f"%{t}%"]
        facts = self.db.execute(
            f"""SELECT f.*, e.name AS subject FROM facts f JOIN entities e ON e.id=f.subject_id
               WHERE {cond} ORDER BY f.recorded_at DESC LIMIT ?""",
            (*binds, limit),
        ).fetchall()
        return {
            "query": query,
            "text_hits": [{"kind": h["kind"], "title": h["title"], "body": h["body"]} for h in hits],
            "facts": [self._fact_dict(f) for f in facts],
            "decisions": self.why(query),
        }

    # ---------- Markdown mirror ----------

    # ---------- graph snapshot (for the live viewer) ----------

    def graph_snapshot(self) -> dict:
        nodes: list[dict] = []
        edges: list[dict] = []
        details: dict[str, dict] = {}
        ent_node: dict[str, str] = {}

        for e in self.db.execute("SELECT id, name, type FROM entities ORDER BY id"):
            nid = f"ent:{e['id']}"
            ent_node[e["name"]] = nid
            facts = self.db.execute(
                """SELECT predicate, object_text, source_agent, valid_from
                   FROM facts WHERE subject_id=? AND valid_to IS NULL
                   ORDER BY recorded_at""", (e["id"],)).fetchall()
            decs = self.db.execute(
                """SELECT d.title FROM decisions d
                   JOIN decision_entities de ON de.decision_id = d.id
                   WHERE de.entity_id=? ORDER BY d.made_at""", (e["id"],)).fetchall()
            timeline_count = self.db.execute(
                "SELECT COUNT(*) c FROM facts WHERE subject_id=?",
                (e["id"],)).fetchone()["c"]
            nodes.append({"id": nid, "kind": "entity", "label": e["name"],
                          "sub": e["type"]})
            details[nid] = {
                "kind": "entity", "title": e["name"], "sub": e["type"],
                "facts": [dict(f) for f in facts],
                "decisions": [d["title"] for d in decs],
                "timeline_count": timeline_count,
            }

        for d in self.db.execute("SELECT id, title, status FROM decisions ORDER BY id"):
            nid = f"dec:{d['id']}"
            nodes.append({"id": nid, "kind": "decision", "label": d["title"],
                          "sub": d["status"]})
            details[nid] = {"kind": "decision", **self.get_decision(d["id"])}
        for link in self.db.execute(
            """SELECT de.decision_id, e.name FROM decision_entities de
               JOIN entities e ON e.id = de.entity_id"""):
            edges.append({"source": f"dec:{link['decision_id']}",
                          "target": ent_node[link["name"]],
                          "label": "involves", "kind": "decision"})
        for d in self.db.execute(
                "SELECT id, supersedes_id FROM decisions WHERE supersedes_id IS NOT NULL"):
            edges.append({"source": f"dec:{d['id']}",
                          "target": f"dec:{d['supersedes_id']}",
                          "label": "supersedes", "kind": "decision"})

        val_node: dict[str, str] = {}
        for f in self.db.execute(
            """SELECT f.*, e.name AS subject FROM facts f
               JOIN entities e ON e.id = f.subject_id
               WHERE f.valid_to IS NULL ORDER BY f.recorded_at"""):
            target = ent_node.get(f["object_text"])
            if target is None:
                if f["object_text"] not in val_node:
                    vid = f"val:{len(val_node) + 1}"
                    val_node[f["object_text"]] = vid
                    nodes.append({"id": vid, "kind": "value",
                                  "label": f["object_text"], "sub": "value"})
                    details[vid] = {"kind": "value",
                                    "title": f["object_text"], "facts": []}
                target = val_node[f["object_text"]]
                details[target]["facts"].append(
                    {"subject": f["subject"], "predicate": f["predicate"],
                     "source_agent": f["source_agent"],
                     "valid_from": f["valid_from"]})
            edges.append({"source": ent_node[f["subject"]], "target": target,
                          "label": f["predicate"], "kind": "fact",
                          "by": f["source_agent"], "at": f["valid_from"]})

        open_conflicts = self.open_conflicts()
        conflicted = {ent_node[c["subject"]] for c in open_conflicts
                      if c["subject"] in ent_node}
        for n in nodes:
            if n["id"] in conflicted:
                n["conflict"] = True
        return {
            "version": self.graph_version(),
            "nodes": nodes, "edges": edges, "details": details,
            "open_conflicts": open_conflicts,
            "stats": {**self.stats(),
                      "nodes": len(nodes), "edges": len(edges)},
        }

    def export_markdown(self) -> str:
        lines = ["# MIRSAD — سجلّ الذاكرة المشتركة", ""]
        lines.append("## القرارات")
        for row in self.db.execute("SELECT id FROM decisions ORDER BY made_at").fetchall():
            d = self.get_decision(row["id"])
            lines.append(f"\n### {d['title']}  [{d['status']}]")
            lines.append(f"- اتُّخذ بواسطة: {d['made_by']} — {d['made_at']}")
            if d["description"]:
                lines.append(f"- الوصف: {d['description']}")
            for r in d["reasons"]:
                lines.append(f"- سبب: {r}")
            for ev in d["evidence"]:
                lines.append(f"- دليل: {ev['text']}" + (f" ({ev['ref']})" if ev["ref"] else ""))
            for a in d["alternatives_rejected"]:
                lines.append(f"- بديل مرفوض: {a}")
            for e in d["effects"]:
                lines.append(f"- أثّر على: {e}")
            for o in d["outcomes"]:
                lines.append(f"- نتيجة: {o['text']} (سجّلها {o['by']})")
            if d["superseded_by"]:
                lines.append(f"- استُبدل بالقرار رقم {d['superseded_by']}")
        lines.append("\n## الحقائق الحالية")
        for row in self.db.execute(
            """SELECT f.*, e.name AS subject FROM facts f JOIN entities e ON e.id=f.subject_id
               WHERE f.valid_to IS NULL ORDER BY e.name, f.predicate"""
        ).fetchall():
            f = self._fact_dict(row)
            lines.append(
                f"- {f['subject']} — {f['predicate']}: {f['object']} "
                f"(من {f['valid_from']}، سجّلها {f['source_agent']})"
            )
        conf = self.open_conflicts()
        if conf:
            lines.append("\n## تعارضات مفتوحة تحتاج قرارك")
            for c in conf:
                lines.append(f"- #{c['id']} {c['subject']}/{c['predicate']}: {c['detail']}")
        return "\n".join(lines) + "\n"

    def stats(self) -> dict:
        q = lambda sql: self.db.execute(sql).fetchone()[0]
        return {
            "entities": q("SELECT COUNT(*) FROM entities"),
            "facts_total": q("SELECT COUNT(*) FROM facts"),
            "facts_current": q("SELECT COUNT(*) FROM facts WHERE valid_to IS NULL"),
            "decisions": q("SELECT COUNT(*) FROM decisions"),
            "episodes": q("SELECT COUNT(*) FROM episodes"),
            "open_conflicts": q("SELECT COUNT(*) FROM conflicts WHERE status='open'"),
        }
