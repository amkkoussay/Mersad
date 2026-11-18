"""MIRSAD core engine — temporal knowledge graph + first-class decisions.

Design lessons applied (see README.md):
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
  when the SAME agent rewrites its own value, the older fact's validity
  window closes. When a *different agent* challenges a standing value,
  the standing value keeps currency and the challenger waits as a
  pending candidate with a conflict opened for the owner — only the
  owner's ruling (resolve_conflict, choosing the winner) changes what
  is current. A machine's version never wins by arriving later.
- Zero external services: one SQLite file. No LLM and no API key are needed
  to write or read the graph; an LLM is only the *client* (via MCP/CLI).
"""

from __future__ import annotations

import sqlite3
import threading
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


# Version of the database layout created by SCHEMA above plus the
# migrations in Mirsad._migrate. Bump when the schema changes; reported
# by version_info so clients can verify builds.
#   v1: the original layout (<= 0.2.3), no migration path.
#   v2: entities.name_norm + entity_aliases.alias_norm (indexed name
#       resolution), facts/decisions.fts_rowid (FTS rows are deleted when
#       their record is invalidated/superseded), search_index rebuilt
#       from current records only.
#   v3: facts.pending (a cross-agent challenger value waits as a pending
#       candidate; the standing value stays current until the owner
#       rules), episodes.fts_rowid (episode content is indexed and
#       searchable like facts and decisions).
SCHEMA_VERSION = 3


class Mirsad:
    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        # One connection PER THREAD, never shared across threads: the MCP
        # server runs tool calls on a thread pool, and a single shared
        # sqlite3 connection raises ProgrammingError the moment another
        # thread touches it (check_same_thread). WAL lets the per-thread
        # connections read concurrently; busy_timeout rides out the
        # single-writer moments instead of failing with "database is
        # locked". (Verified failure + fix, 2026-11-15.)
        self._local = threading.local()
        conn = self._open_connection()
        self._local.conn = conn
        conn.executescript(SCHEMA)
        conn.commit()
        self._migrate(conn)

    def _open_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=10000")
        # The schema declares REFERENCES throughout, but SQLite enforces
        # them only when asked — per connection, every connection. Until
        # 2026-11-18 this pragma was never set, so dangling references
        # (a fact pointing at a non-existent episode, an outcome attached
        # to a non-existent decision) were silently accepted.
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    @property
    def db(self) -> sqlite3.Connection:
        """The calling thread's own connection, opened on first use."""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._open_connection()
            self._local.conn = conn
        return conn

    # ---------- schema migrations ----------

    def _stored_schema_version(self, conn: sqlite3.Connection) -> int:
        row = conn.execute(
            "SELECT v FROM meta WHERE k='schema_version'").fetchone()
        return row["v"] if row else 1  # pre-migration databases are v1

    def _set_schema_version(self, conn: sqlite3.Connection, v: int) -> None:
        conn.execute(
            """INSERT INTO meta(k, v) VALUES ('schema_version', ?)
               ON CONFLICT(k) DO UPDATE SET v = excluded.v""",
            (v,),
        )

    @staticmethod
    def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
        return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}

    def _migrate(self, conn: sqlite3.Connection) -> None:
        """Bring any older database to SCHEMA_VERSION, step by step.
        Every step is guarded so re-running it on a current database is
        a no-op; a fresh database walks the same steps as a legacy one."""
        if self._stored_schema_version(conn) < 2:
            self._migrate_v2(conn)
            self._set_schema_version(conn, 2)
        if self._stored_schema_version(conn) < 3:
            self._migrate_v3(conn)
            self._set_schema_version(conn, 3)
        conn.commit()

    def _migrate_v3(self, conn: sqlite3.Connection) -> None:
        # Pending candidates: a fact written by one agent over another
        # agent's standing value no longer takes currency on arrival —
        # it waits (pending=1) until the owner rules on the conflict.
        # Legacy rows are all standing (pending=0): the migration does
        # not retroactively change which value is current anywhere.
        if "pending" not in self._columns(conn, "facts"):
            conn.execute(
                "ALTER TABLE facts ADD COLUMN pending "
                "INTEGER NOT NULL DEFAULT 0")
        # Episodes join the search index: recall's contract always said
        # it searches raw episodes; until now nothing indexed them.
        if "fts_rowid" not in self._columns(conn, "episodes"):
            conn.execute("ALTER TABLE episodes ADD COLUMN fts_rowid INTEGER")
        for ep in conn.execute(
                """SELECT id, source_agent, source_ref, content FROM episodes
                   WHERE fts_rowid IS NULL"""):
            title = ep["source_agent"] + (
                f" · {ep['source_ref']}" if ep["source_ref"] else "")
            rid = self._index_on(conn, "episode", title, ep["content"])
            conn.execute("UPDATE episodes SET fts_rowid=? WHERE id=?",
                         (rid, ep["id"]))

    def _migrate_v2(self, conn: sqlite3.Connection) -> None:
        # Indexed normalized-name columns (name resolution was a full
        # table scan compared in Python on every lookup).
        if "name_norm" not in self._columns(conn, "entities"):
            conn.execute("ALTER TABLE entities ADD COLUMN name_norm TEXT")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_entities_name_norm "
            "ON entities(name_norm)")
        for r in conn.execute(
                "SELECT id, name FROM entities WHERE name_norm IS NULL"):
            conn.execute("UPDATE entities SET name_norm=? WHERE id=?",
                         (self._norm(r["name"]), r["id"]))
        if "alias_norm" not in self._columns(conn, "entity_aliases"):
            conn.execute(
                "ALTER TABLE entity_aliases ADD COLUMN alias_norm TEXT")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_aliases_alias_norm "
            "ON entity_aliases(alias_norm)")
        for r in conn.execute(
                "SELECT entity_id, alias FROM entity_aliases "
                "WHERE alias_norm IS NULL"):
            conn.execute(
                "UPDATE entity_aliases SET alias_norm=? "
                "WHERE entity_id=? AND alias=?",
                (self._norm(r["alias"]), r["entity_id"], r["alias"]))
        # FTS bookkeeping: each indexed record remembers its search_index
        # rowid so the row can be deleted when the record stops being
        # current (invalidated fact, superseded decision).
        if "fts_rowid" not in self._columns(conn, "facts"):
            conn.execute("ALTER TABLE facts ADD COLUMN fts_rowid INTEGER")
        if "fts_rowid" not in self._columns(conn, "decisions"):
            conn.execute(
                "ALTER TABLE decisions ADD COLUMN fts_rowid INTEGER")
        # Rebuild the index from CURRENT records only: legacy indexes
        # carry a stale row for every value ever written (duplicates and
        # superseded values included), and recall surfaced them as hits.
        conn.execute("DELETE FROM search_index")
        for f in conn.execute(
                """SELECT id, subject_id, predicate, object_text FROM facts
                   WHERE valid_to IS NULL"""):
            rid = self._index_on(
                conn, "fact",
                f"{self._entity_name(f['subject_id'])} {f['predicate']}",
                f["object_text"])
            conn.execute("UPDATE facts SET fts_rowid=? WHERE id=?",
                         (rid, f["id"]))
        for d in conn.execute(
                """SELECT id, title, description FROM decisions
                   WHERE status != 'superseded'"""):
            texts = [i["text"] for i in conn.execute(
                "SELECT text FROM decision_items WHERE decision_id=?",
                (d["id"],))]
            rid = self._index_on(
                conn, "decision", d["title"],
                " ".join([d["description"] or ""] + texts))
            conn.execute("UPDATE decisions SET fts_rowid=? WHERE id=?",
                         (rid, d["id"]))

    @staticmethod
    def _index_on(conn: sqlite3.Connection, kind: str, title: str,
                  body: str) -> int:
        cur = conn.execute(
            "INSERT INTO search_index(kind, title, body) VALUES (?,?,?)",
            (kind, title, body),
        )
        return cur.lastrowid

    # ---------- entities & episodes ----------

    def _entity(self, name: str, type: str = "thing") -> int:
        """Get-or-create WITHOUT committing: composite writes (a fact,
        a decision) call this inside their own transaction, so a failure
        later in the operation rolls the new entity back with everything
        else instead of leaving it committed on its own. Standalone
        callers use entity(), which commits."""
        row = self.db.execute(
            "SELECT id FROM entities WHERE name=? AND type=?", (name, type)
        ).fetchone()
        if row:
            return row["id"]
        cur = self.db.execute(
            "INSERT INTO entities(name, type, created_at, name_norm) VALUES (?,?,?,?)",
            (name, type, _now(), self._norm(name)),
        )
        return cur.lastrowid

    def entity(self, name: str, type: str = "thing") -> int:
        eid = self._entity(name, type)
        self.db.commit()
        return eid

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

    def _add_episode(self, source_agent: str, content: str,
                     source_ref: str | None = None) -> int:
        """Insert an episode WITHOUT committing (see _entity) and index
        its content, so recall can find the raw record a fact or decision
        traces to — not just the distilled fact."""
        self._bump()
        cur = self.db.execute(
            "INSERT INTO episodes(source_agent, source_ref, content, created_at) VALUES (?,?,?,?)",
            (source_agent, source_ref, content, _now()),
        )
        ep_id = cur.lastrowid
        title = source_agent + (f" · {source_ref}" if source_ref else "")
        rid = self._index("episode", title, content)
        self.db.execute(
            "UPDATE episodes SET fts_rowid=? WHERE id=?", (rid, ep_id))
        return ep_id

    def add_episode(self, source_agent: str, content: str, source_ref: str | None = None) -> int:
        ep_id = self._add_episode(source_agent, content, source_ref)
        self.db.commit()
        return ep_id

    def _index(self, kind: str, title: str, body: str) -> int:
        return self._index_on(self.db, kind, title, body)

    def _unindex(self, fts_rowid: int | None) -> None:
        """Remove a record's search_index row (it stopped being current:
        an invalidated fact or a superseded decision must not keep
        surfacing in recall as if it were live knowledge)."""
        if fts_rowid is not None:
            self.db.execute(
                "DELETE FROM search_index WHERE rowid=?", (fts_rowid,))

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

    # SQLite refuses LIKE patterns longer than SQLITE_MAX_LIKE_PATTERN_LENGTH
    # with "LIKE or GLOB pattern too complex". Stock SQLite allows 50,000
    # bytes, but Cloudflare D1's limit is 50 BYTES — verified empirically
    # 2026-11-15 (a 50-byte pattern succeeds, 51 bytes raises). The 0.2.2
    # fix capped patterns at 500 chars, which was still 10x over D1's real
    # limit, so ordinary multi-word queries of 50-500 chars kept crashing
    # on D1. Every pattern built here is therefore guaranteed <= 50 bytes
    # in UTF-8, escapes included; longer terms are truncated to a prefix
    # instead of being dropped, so long inputs still search.
    _LIKE_PATTERN_MAX = 50

    @classmethod
    def _like_pattern(cls, term: str) -> str | None:
        """Literal LIKE pattern for user text, guaranteed <= 50 bytes.
        %, _ and \\ are escaped so the text matches literally, not as a
        pattern language. Over-long terms are truncated byte-wise
        (escape-aware) to a prefix pattern."""
        t = (term or "").strip()
        if not t:
            return None
        budget = cls._LIKE_PATTERN_MAX - 2  # the two % wrappers
        out: list[str] = []
        used = 0
        for ch in t:
            esc = ("\\" + ch) if ch in "\\%_" else ch
            b = len(esc.encode("utf-8"))
            if used + b > budget:
                break
            out.append(esc)
            used += b
        if not out:
            return None
        return "%" + "".join(out) + "%"

    def add_alias(self, entity_name: str, alias: str) -> int:
        self._bump()
        eid = self._entity(entity_name)
        self.db.execute(
            "INSERT OR IGNORE INTO entity_aliases(entity_id, alias, alias_norm) VALUES (?,?,?)",
            (eid, alias, self._norm(alias)),
        )
        linked = self._backfill_object_links(eid)
        self.db.commit()
        return linked

    def resolve_subject(self, name: str) -> str | None:
        """Resolve a name as an agent wrote it (any script, diacritics, case)
        to the canonical stored entity name, via exact match, normalized
        match, or a registered alias. All three lookups are indexed
        (entities.name UNIQUE, entities.name_norm, aliases.alias_norm) —
        this used to fetch every entity and alias and compare in Python."""
        row = self.db.execute(
            "SELECT name FROM entities WHERE name=?", (name,)
        ).fetchone()
        if row:
            return row["name"]
        target = self._norm(name)
        if not target:
            return None
        row = self.db.execute(
            "SELECT name FROM entities WHERE name_norm=?", (target,)
        ).fetchone()
        if row:
            return row["name"]
        row = self.db.execute(
            """SELECT e.name AS canonical FROM entity_aliases a
               JOIN entities e ON e.id = a.entity_id
               WHERE a.alias_norm=?""",
            (target,),
        ).fetchone()
        return row["canonical"] if row else None

    def _resolve_entity_id(self, name: str) -> int | None:
        """Resolve any name (entity name or registered alias, any script or
        case, Arabic diacritics ignored) to an entity id. None if unknown.
        Indexed lookups only — see resolve_subject."""
        row = self.db.execute(
            "SELECT id FROM entities WHERE name=?", (name,)
        ).fetchone()
        if row:
            return row["id"]
        target = self._norm(name)
        if not target:
            return None
        row = self.db.execute(
            "SELECT id FROM entities WHERE name_norm=?", (target,)
        ).fetchone()
        if row:
            return row["id"]
        row = self.db.execute(
            "SELECT entity_id FROM entity_aliases WHERE alias_norm=?",
            (target,),
        ).fetchone()
        return row["entity_id"] if row else None

    def _backfill_object_links(self, entity_id: int) -> int:
        """Auto-link: when an entity appears (created or given a new alias),
        point every still-unlinked current fact whose WHOLE object text is
        exactly that entity's name or one of its aliases at it. Whole-text
        equality only — a name merely mentioned inside a sentence never
        creates an edge, and a fact never links an entity to itself."""
        row = self.db.execute(
            "SELECT name FROM entities WHERE id=?", (entity_id,)
        ).fetchone()
        if not row:
            return 0
        names = {self._norm(row["name"])}
        for a in self.db.execute(
            "SELECT alias FROM entity_aliases WHERE entity_id=?", (entity_id,)
        ).fetchall():
            names.add(self._norm(a["alias"]))
        linked = 0
        for f in self.db.execute(
            """SELECT id, subject_id, object_text FROM facts
               WHERE object_id IS NULL AND valid_to IS NULL"""
        ).fetchall():
            if f["subject_id"] == entity_id:
                continue
            v = self._norm(f["object_text"])
            if v and len(v) <= 80 and v in names:
                self.db.execute(
                    "UPDATE facts SET object_id=? WHERE id=?",
                    (entity_id, f["id"]),
                )
                linked += 1
        return linked

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
        """One transaction: any failure rolls the whole write back, so a
        half-written fact (an episode with no fact, an entity with no
        fact) can never be committed later by an unrelated write that
        happens to share this thread's connection."""
        try:
            return self._remember_fact_impl(
                subject, predicate, object_text, source_agent,
                subject_type=subject_type, valid_from=valid_from,
                source_ref=source_ref, episode=episode,
                confidence=confidence)
        except Exception:
            self.db.rollback()
            raise

    def _remember_fact_impl(
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
        """Store a fact. If the SAME agent rewrites its own standing
        value, the old one is invalidated (valid_to closed, expired_at
        stamped) — never deleted — and the new one takes currency.
        If a DIFFERENT agent challenges a standing value, the standing
        value KEEPS currency and the challenger is stored as a pending
        candidate with a conflict opened for the owner: a machine's
        version never takes over just by arriving later, and only the
        owner's ruling (resolve_conflict) can promote the challenger.
        Rewriting the SAME value that is already current is a no-op
        (idempotent): agents retry writes, and a duplicate active row
        would silently double the fact in storage and in the index."""
        valid_from = valid_from if valid_from is not None else _now()
        resolved = self._resolve_entity_id(subject)
        subject_created = resolved is None
        sid = resolved if resolved is not None else self._entity(subject, subject_type)

        old = self.db.execute(
            """SELECT * FROM facts WHERE subject_id=? AND predicate=?
               AND valid_to IS NULL AND pending=0
               ORDER BY recorded_at DESC LIMIT 1""",
            (sid, predicate),
        ).fetchone()

        if old and old["object_text"] == object_text:
            return {
                "fact_id": old["id"],
                "superseded_fact_id": None,
                "conflict_opened": None,
                "linked_object_id": old["object_id"],
                "backfilled_links": 0,
                "duplicate": True,
            }

        cand = self.db.execute(
            """SELECT * FROM facts WHERE subject_id=? AND predicate=?
               AND valid_to IS NULL AND pending=1 AND object_text=?
               ORDER BY recorded_at DESC LIMIT 1""",
            (sid, predicate, object_text),
        ).fetchone()
        if cand:
            conf = self.db.execute(
                "SELECT id FROM conflicts WHERE new_fact_id=? AND status='open'",
                (cand["id"],),
            ).fetchone()
            return {
                "fact_id": cand["id"],
                "superseded_fact_id": None,
                "conflict_opened": conf["id"] if conf else None,
                "linked_object_id": cand["object_id"],
                "backfilled_links": 0,
                "duplicate": True,
                "pending": True,
            }

        self._bump()
        ep_id = self._add_episode(source_agent, episode or f"{subject} {predicate} {object_text}", source_ref) \
            if (episode or source_ref) else None

        superseded = None
        conflict_id = None
        pending = 0
        if old and old["object_text"] != object_text:
            if old["source_agent"] != source_agent:
                # Cross-agent challenge: the standing value keeps
                # currency; the challenger waits as a pending candidate.
                pending = 1
                cur = self.db.execute(
                    """INSERT INTO conflicts(subject, predicate, old_fact_id, new_fact_id,
                       detail, created_at) VALUES (?,?,?,?,?,?)""",
                    (
                        subject,
                        predicate,
                        old["id"],
                        None,  # filled below after insert of the new fact
                        f"{old['source_agent']} recorded '{old['object_text']}'; "
                        f"{source_agent} proposes '{object_text}'. The standing "
                        f"value stays current until the owner rules.",
                        _now(),
                    ),
                )
                conflict_id = cur.lastrowid
            else:
                # Same agent correcting its own value: latest wins.
                self.db.execute(
                    "UPDATE facts SET valid_to=?, expired_at=? WHERE id=?",
                    (valid_from, _now(), old["id"]),
                )
                self._unindex(old["fts_rowid"])
                superseded = old["id"]

        # Auto-link the object: when the value is exactly an existing
        # entity's name or alias, store the edge (object_id) with the fact.
        oid = None
        if isinstance(object_text, str) and len(self._norm(object_text)) <= 80:
            cand = self._resolve_entity_id(object_text)
            if cand is not None and cand != sid:
                oid = cand

        cur = self.db.execute(
            """INSERT INTO facts(subject_id, predicate, object_text, object_id, valid_from,
               recorded_at, source_agent, episode_id, confidence, pending)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (sid, predicate, object_text, oid, valid_from, _now(), source_agent, ep_id, confidence, pending),
        )
        new_id = cur.lastrowid
        if conflict_id:
            self.db.execute(
                "UPDATE conflicts SET new_fact_id=? WHERE id=?", (new_id, conflict_id)
            )
        # A brand-new subject entity joins its natural path: earlier facts
        # that named it as their object now link to it.
        backfilled = self._backfill_object_links(sid) if subject_created else 0
        if not pending:
            # Pending candidates are NOT indexed: recall must not surface
            # an unruled challenger as if it were standing knowledge.
            fts_rowid = self._index("fact", f"{subject} {predicate}", object_text)
            self.db.execute(
                "UPDATE facts SET fts_rowid=? WHERE id=?", (fts_rowid, new_id))
        self.db.commit()
        return {
            "fact_id": new_id,
            "superseded_fact_id": superseded,
            "conflict_opened": conflict_id,
            "linked_object_id": oid,
            "backfilled_links": backfilled,
            "pending": bool(pending),
        }

    def current_fact(self, subject: str, predicate: str) -> dict | None:
        subject = self.resolve_subject(subject) or subject
        row = self.db.execute(
            """SELECT f.*, e.name AS subject FROM facts f
               JOIN entities e ON e.id = f.subject_id
               WHERE e.name=? AND f.predicate=? AND f.valid_to IS NULL
               AND f.pending=0
               ORDER BY f.recorded_at DESC LIMIT 1""",
            (subject, predicate),
        ).fetchone()
        return self._fact_dict(row) if row else None

    def fact_at(self, subject: str, predicate: str, at: float) -> dict | None:
        """What was true at time `at` (world time), per current knowledge.
        Resolves the subject through aliases/normalization like every
        other query — it silently skipped resolution before, so asking
        by an alias or a diacritics variant returned None for data that
        exists. Pending challengers never count as having been true."""
        subject = self.resolve_subject(subject) or subject
        row = self.db.execute(
            """SELECT f.*, e.name AS subject FROM facts f
               JOIN entities e ON e.id = f.subject_id
               WHERE e.name=? AND f.predicate=? AND f.valid_from<=?
               AND (f.valid_to IS NULL OR f.valid_to>?) AND f.pending=0
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

    def subject_brief(self, subject: str) -> dict:
        """Everything known about one subject, in one call: standing
        facts, pending challengers, open conflicts, the decisions that
        involve it, and its full timeline. Until now an agent answering
        'what do we know about X?' had to stitch current_fact (one
        predicate at a time), timeline, why and open_conflicts together
        by hand — and usually skipped the conflicts."""
        canonical = self.resolve_subject(subject) or subject
        eid = self._resolve_entity_id(canonical)
        if eid is None:
            return {"subject": canonical, "found": False,
                    "current_facts": [], "pending_candidates": [],
                    "open_conflicts": [], "decisions": [], "timeline": []}
        erow = self.db.execute(
            "SELECT name, type FROM entities WHERE id=?", (eid,)).fetchone()
        history = self.timeline(canonical)
        decs = []
        for r in self.db.execute(
                """SELECT d.id FROM decisions d
                   JOIN decision_entities de ON de.decision_id = d.id
                   WHERE de.entity_id=? ORDER BY d.made_at""",
                (eid,)).fetchall():
            d = self.get_decision(r["id"])
            if d:
                decs.append(d)
        return {
            "subject": erow["name"],
            "type": erow["type"],
            "found": True,
            "current_facts": [f for f in history if f["currently_valid"]],
            "pending_candidates": [f for f in history if f["pending"]],
            "open_conflicts": [c for c in self.open_conflicts()
                               if c["subject"] == erow["name"]],
            "decisions": decs,
            "timeline": history,
        }

    def _fact_dict(self, row: sqlite3.Row) -> dict:
        pending = bool(row["pending"]) if "pending" in row.keys() else False
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
            "pending": pending,
            "currently_valid": row["valid_to"] is None and not pending,
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
        """One transaction, like remember_fact: a failure anywhere in
        the unit (decision row, items, entity links, index) rolls the
        whole decision back instead of leaving a partial unit behind."""
        try:
            return self._record_decision_impl(
                title, made_by, description=description, reasons=reasons,
                evidence=evidence,
                alternatives_rejected=alternatives_rejected,
                effects=effects, entities=entities, status=status,
                supersedes=supersedes, episode=episode)
        except Exception:
            self.db.rollback()
            raise

    def _record_decision_impl(
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
        ep_id = self._add_episode(made_by, episode or f"Decision: {title}") if episode else None
        cur = self.db.execute(
            """INSERT INTO decisions(title, description, status, made_by, made_at,
               episode_id, supersedes_id) VALUES (?,?,?,?,?,?,?)""",
            (title, description, status, made_by, _now(), ep_id, supersedes),
        )
        did = cur.lastrowid
        if supersedes:
            srow = self.db.execute(
                "SELECT fts_rowid FROM decisions WHERE id=?",
                (supersedes,),
            ).fetchone()
            self.db.execute(
                "UPDATE decisions SET status='superseded', superseded_by_id=? WHERE id=?",
                (did, supersedes),
            )
            if srow:
                self._unindex(srow["fts_rowid"])
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
            eid = self._entity(name)
            self.db.execute(
                "INSERT OR IGNORE INTO decision_entities(decision_id, entity_id) VALUES (?,?)",
                (did, eid),
            )
        fts_rowid = self._index(
            "decision",
            title,
            " ".join([description] + [t for _, t, _ in items]),
        )
        self.db.execute(
            "UPDATE decisions SET fts_rowid=? WHERE id=?", (fts_rowid, did))
        self.db.commit()
        return did

    def record_outcome(self, decision_id: int, outcome: str, recorded_by: str) -> None:
        if not self.db.execute(
                "SELECT 1 FROM decisions WHERE id=?",
                (decision_id,)).fetchone():
            raise ValueError(f"no decision with id {decision_id}")
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
        """Answer 'why did we choose X?' — decisions matching the topic,
        ranked by how much of the question they answer: an exact-phrase
        match weighs most, then each matched topic word (in the title,
        description, items, or linked entity names, aliases resolved).
        Each result carries its full chain (reasons, evidence, rejected
        alternatives, outcomes, supersedes links).

        The old implementation tried the WHOLE topic as one LIKE phrase
        first: a multi-word question almost never matches a title
        verbatim, and the 50-byte LIKE cap truncated the phrase to a
        prefix — so most real questions fell through two fallback stages
        before matching anything."""
        scores: dict[int, int] = {}
        made: dict[int, float] = {}

        def add(decision_id: int, weight: int, ts: float) -> None:
            scores[decision_id] = scores.get(decision_id, 0) + weight
            made[decision_id] = ts

        phrase = self._like_pattern(topic)
        if phrase is not None:
            for r in self.db.execute(
                """SELECT DISTINCT d.id, d.made_at FROM decisions d
                   LEFT JOIN decision_items di ON di.decision_id = d.id
                   WHERE d.title LIKE ? ESCAPE '\\'
                      OR d.description LIKE ? ESCAPE '\\'
                      OR di.text LIKE ? ESCAPE '\\'""",
                (phrase, phrase, phrase),
            ).fetchall():
                add(r["id"], 3, r["made_at"])
        for tok in self._topic_tokens(topic):
            like = self._like_pattern(tok)
            if like is None:
                continue
            for r in self.db.execute(
                """SELECT DISTINCT d.id, d.made_at FROM decisions d
                   LEFT JOIN decision_items di ON di.decision_id = d.id
                   LEFT JOIN decision_entities de ON de.decision_id = d.id
                   LEFT JOIN entities e ON e.id = de.entity_id
                   WHERE d.title LIKE ? ESCAPE '\\'
                      OR d.description LIKE ? ESCAPE '\\'
                      OR di.text LIKE ? ESCAPE '\\'
                      OR e.name LIKE ? ESCAPE '\\'""",
                (like, like, like, like),
            ).fetchall():
                add(r["id"], 1, r["made_at"])
            canonical = self.resolve_subject(tok)
            if canonical:
                for r in self.db.execute(
                    """SELECT d.id, d.made_at FROM decisions d
                       JOIN decision_entities de ON de.decision_id = d.id
                       JOIN entities e ON e.id = de.entity_id
                       WHERE e.name = ?""",
                    (canonical,),
                ).fetchall():
                    add(r["id"], 1, r["made_at"])
        ordered = sorted(scores, key=lambda i: (-scores[i], made[i]))
        return [self.get_decision(i) for i in ordered]

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

    def resolve_conflict(self, conflict_id: int, resolution: str,
                         winner: str = "standing") -> dict:
        """Apply the owner's ruling — the ruling CHANGES what is current,
        it is not just a note on the conflict. winner="standing" keeps
        the standing value and closes the challenger into history;
        winner="challenger" closes the standing value's window and
        promotes the challenger (indexing it for the first time).
        Either way both values stay in the timeline; nothing is deleted.
        One transaction: the fact changes and the conflict closing
        commit together or not at all."""
        if winner not in ("standing", "challenger"):
            raise ValueError(
                f"winner must be 'standing' or 'challenger', got {winner!r}")
        conf = self.db.execute(
            "SELECT * FROM conflicts WHERE id=?", (conflict_id,)).fetchone()
        if conf is None:
            raise ValueError(f"no conflict with id {conflict_id}")
        if conf["status"] != "open":
            raise ValueError(f"conflict {conflict_id} is already resolved")
        try:
            self._bump()
            old = self.db.execute(
                "SELECT * FROM facts WHERE id=?",
                (conf["old_fact_id"],)).fetchone()
            new = self.db.execute(
                "SELECT * FROM facts WHERE id=?",
                (conf["new_fact_id"],)).fetchone()
            if winner == "challenger" and new is not None:
                if old is not None and old["valid_to"] is None \
                        and not old["pending"]:
                    self.db.execute(
                        "UPDATE facts SET valid_to=?, expired_at=? WHERE id=?",
                        (new["valid_from"], _now(), old["id"]))
                    self._unindex(old["fts_rowid"])
                self.db.execute(
                    "UPDATE facts SET pending=0 WHERE id=?", (new["id"],))
                if new["fts_rowid"] is None:
                    subject = self._entity_name(new["subject_id"])
                    rid = self._index(
                        "fact", f"{subject} {new['predicate']}",
                        new["object_text"])
                    self.db.execute(
                        "UPDATE facts SET fts_rowid=? WHERE id=?",
                        (rid, new["id"]))
            elif winner == "standing" and new is not None and new["pending"]:
                # The challenger goes to history as a rejected proposal:
                # window closed, never indexed, still on the timeline.
                # (Guarded on pending: a pre-v3 legacy conflict's
                # challenger already took currency under the old
                # latest-wins rule — ruling "standing" there just closes
                # the record, it must not close the live value.)
                self.db.execute(
                    "UPDATE facts SET valid_to=?, expired_at=?, pending=0 "
                    "WHERE id=?",
                    (_now(), _now(), new["id"]))
            self.db.execute(
                "UPDATE conflicts SET status='resolved', resolution=?, "
                "resolved_at=? WHERE id=?",
                (resolution, _now(), conflict_id))
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        current = self.current_fact(conf["subject"], conf["predicate"])
        return {
            "conflict_id": conflict_id,
            "winner": winner,
            "standing_fact_id": conf["old_fact_id"],
            "challenger_fact_id": conf["new_fact_id"],
            "current_value": current["object"] if current else None,
        }

    # ---------- recall (hybrid: FTS + graph neighbours) ----------

    def recall(self, query: str, limit: int = 10) -> dict:
        toks = self._topic_tokens(query)
        fts_q = " OR ".join(f'"{t}"' for t in toks) if toks else f'"{query[:200]}"'
        hits = self.db.execute(
            """SELECT kind, title, body FROM search_index
               WHERE search_index MATCH ? ORDER BY rank LIMIT ?""",
            (fts_q, limit),
        ).fetchall() if query.strip() else []
        canonical = self.resolve_subject(query)
        terms = list(dict.fromkeys([query, canonical, *toks]))
        pats = [p for t in terms if (p := self._like_pattern(t))]
        facts = []
        if pats:
            cond = " OR ".join(
                "(e.name LIKE ? ESCAPE '\\' OR f.object_text LIKE ? ESCAPE '\\'"
                " OR f.predicate LIKE ? ESCAPE '\\')"
                for _ in pats
            )
            binds: list = []
            for p in pats:
                binds += [p, p, p]
            facts = self.db.execute(
                f"""SELECT f.*, e.name AS subject FROM facts f JOIN entities e ON e.id=f.subject_id
                   WHERE ({cond}) AND f.pending=0 ORDER BY f.recorded_at DESC LIMIT ?""",
                (*binds, limit),
            ).fetchall()
        result = {
            "query": query[:200],
            "text_hits": [{"kind": h["kind"], "title": h["title"], "body": h["body"]} for h in hits],
            "facts": [self._fact_dict(f) for f in facts],
            "decisions": self.why(query),
        }
        # Uncertainty is part of the answer: if the query touches an
        # open conflict — by subject, or by either of the disputed
        # values named in its detail — say so here instead of letting
        # the caller treat the standing value as settled knowledge.
        subjects = {f["subject"] for f in facts}
        if canonical:
            subjects.add(canonical)
        needles = {self._norm(t) for t in [query, canonical, *toks] if t}
        needles = {n for n in needles if len(n) >= 2}
        open_c = []
        for c in self.open_conflicts():
            if c["subject"] in subjects or \
                    any(n in self._norm(c["detail"]) for n in needles):
                open_c.append(c)
        if open_c:
            result["conflicts"] = open_c
        if len(query) > 200:
            result["query_truncated"] = True
        return result

    # ---------- Markdown mirror ----------

    # ---------- graph snapshot (for the live viewer) ----------

    def _all_decisions(self) -> list[dict]:
        """Every decision with its full chain, assembled from three bulk
        queries (decisions, items, entity links) instead of one
        get_decision() round-trip per decision."""
        rows = self.db.execute("SELECT * FROM decisions ORDER BY id").fetchall()
        by_id: dict[int, dict] = {}
        for row in rows:
            by_id[row["id"]] = {
                "id": row["id"],
                "title": row["title"],
                "description": row["description"],
                "status": row["status"],
                "made_by": row["made_by"],
                "made_at": fmt_ts(row["made_at"]),
                "supersedes": row["supersedes_id"],
                "superseded_by": row["superseded_by_id"],
                "entities": [],
                "reasons": [],
                "evidence": [],
                "alternatives_rejected": [],
                "effects": [],
                "outcomes": [],
            }
        for it in self.db.execute(
                "SELECT decision_id, kind, text, ref, recorded_by "
                "FROM decision_items ORDER BY id"):
            d = by_id.get(it["decision_id"])
            if d is None:
                continue
            if it["kind"] == "reason":
                d["reasons"].append(it["text"])
            elif it["kind"] == "evidence":
                d["evidence"].append({"text": it["text"], "ref": it["ref"]})
            elif it["kind"] == "alternative":
                d["alternatives_rejected"].append(it["text"])
            elif it["kind"] == "effect":
                d["effects"].append(it["text"])
            elif it["kind"] == "outcome":
                d["outcomes"].append(
                    {"text": it["text"], "by": it["recorded_by"]})
        for en in self.db.execute(
                """SELECT de.decision_id AS did, e.name FROM decision_entities de
                   JOIN entities e ON e.id = de.entity_id"""):
            if en["did"] in by_id:
                by_id[en["did"]]["entities"].append(en["name"])
        return [by_id[r["id"]] for r in rows]

    def graph_snapshot(self, light: bool = False) -> dict:
        """The whole memory as nodes + edges (+ per-node details unless
        light). Built from a fixed set of bulk queries — the previous
        version ran three queries PER ENTITY plus one get_decision per
        decision, and the viewer polls this every 1.2s, so cost grew
        linearly with memory size on every single poll. With light=True
        the details dict is omitted entirely; the viewer fetches one
        node's detail on click via graph_node_detail()."""
        nodes: list[dict] = []
        edges: list[dict] = []
        details: dict[str, dict] = {}
        ent_node: dict[str, str] = {}

        entities = self.db.execute(
            "SELECT id, name, type FROM entities ORDER BY id").fetchall()
        current_facts = self.db.execute(
            """SELECT subject_id, predicate, object_text, source_agent,
                      valid_from FROM facts
               WHERE valid_to IS NULL AND pending=0
               ORDER BY recorded_at""").fetchall()
        facts_by_subject: dict[int, list] = {}
        for f in current_facts:
            facts_by_subject.setdefault(f["subject_id"], []).append(f)
        dec_titles: dict[int, list[str]] = {}
        for r in self.db.execute(
                """SELECT de.entity_id AS eid, d.title FROM decisions d
                   JOIN decision_entities de ON de.decision_id = d.id
                   ORDER BY d.made_at"""):
            dec_titles.setdefault(r["eid"], []).append(r["title"])
        timeline_counts = {
            r["subject_id"]: r["c"] for r in self.db.execute(
                "SELECT subject_id, COUNT(*) AS c FROM facts "
                "GROUP BY subject_id")
        }

        for e in entities:
            nid = f"ent:{e['id']}"
            ent_node[e["name"]] = nid
            nodes.append({"id": nid, "kind": "entity", "label": e["name"],
                          "sub": e["type"]})
            if not light:
                details[nid] = {
                    "kind": "entity", "title": e["name"], "sub": e["type"],
                    "facts": [dict(f) for f in facts_by_subject.get(e["id"], [])],
                    "decisions": dec_titles.get(e["id"], []),
                    "timeline_count": timeline_counts.get(e["id"], 0),
                }

        all_decisions = self._all_decisions()
        for d in all_decisions:
            nid = f"dec:{d['id']}"
            nodes.append({"id": nid, "kind": "decision", "label": d["title"],
                          "sub": d["status"]})
            if not light:
                details[nid] = {"kind": "decision", **d}
            for name in d["entities"]:
                edges.append({"source": nid,
                              "target": ent_node[name],
                              "label": "involves", "kind": "decision"})
            if d["supersedes"]:
                edges.append({"source": nid,
                              "target": f"dec:{d['supersedes']}",
                              "label": "supersedes", "kind": "decision"})

        ent_by_id = {e["id"]: f"ent:{e['id']}" for e in entities}
        val_node: dict[str, str] = {}
        for f in self.db.execute(
            """SELECT f.*, e.name AS subject FROM facts f
               JOIN entities e ON e.id = f.subject_id
               WHERE f.valid_to IS NULL AND f.pending=0
               ORDER BY f.recorded_at"""):
            # Prefer the stored object_id link: the exact-name match
            # below misses values written via an alias, which the
            # auto-linker resolved to the entity long ago.
            target = ent_by_id.get(f["object_id"]) if f["object_id"] else None
            if target is None:
                target = ent_node.get(f["object_text"])
            if target is None:
                if f["object_text"] not in val_node:
                    vid = f"val:{len(val_node) + 1}"
                    val_node[f["object_text"]] = vid
                    nodes.append({"id": vid, "kind": "value",
                                  "label": f["object_text"], "sub": "value"})
                    if not light:
                        details[vid] = {"kind": "value",
                                        "title": f["object_text"],
                                        "facts": []}
                target = val_node[f["object_text"]]
                if not light:
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
        snap = {
            "version": self.graph_version(),
            "nodes": nodes, "edges": edges,
            "open_conflicts": open_conflicts,
            "stats": {**self.stats(),
                      "nodes": len(nodes), "edges": len(edges)},
        }
        if not light:
            snap["details"] = details
        return snap

    def graph_node_detail(self, node_id: str) -> dict | None:
        """One node's story, on demand — the detail half of the old
        graph_snapshot payload, fetched only when a viewer clicks.
        Value nodes return None: their story is derivable from the
        snapshot's edges, which the viewer already holds."""
        kind, _, rest = (node_id or "").partition(":")
        try:
            num = int(rest)
        except ValueError:
            return None
        if kind == "ent":
            row = self.db.execute(
                "SELECT id, name, type FROM entities WHERE id=?",
                (num,)).fetchone()
            if not row:
                return None
            facts = self.db.execute(
                """SELECT predicate, object_text, source_agent, valid_from
                   FROM facts WHERE subject_id=? AND valid_to IS NULL
                   AND pending=0
                   ORDER BY recorded_at""", (num,)).fetchall()
            decs = self.db.execute(
                """SELECT d.title FROM decisions d
                   JOIN decision_entities de ON de.decision_id = d.id
                   WHERE de.entity_id=? ORDER BY d.made_at""",
                (num,)).fetchall()
            timeline_count = self.db.execute(
                "SELECT COUNT(*) c FROM facts WHERE subject_id=?",
                (num,)).fetchone()["c"]
            return {
                "kind": "entity", "title": row["name"], "sub": row["type"],
                "facts": [dict(f) for f in facts],
                "decisions": [d["title"] for d in decs],
                "timeline_count": timeline_count,
            }
        if kind == "dec":
            d = self.get_decision(num)
            return {"kind": "decision", **d} if d else None
        return None

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
               WHERE f.valid_to IS NULL AND f.pending=0 ORDER BY e.name, f.predicate"""
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
        def q(sql):
            return self.db.execute(sql).fetchone()[0]
        return {
            "entities": q("SELECT COUNT(*) FROM entities"),
            "facts_total": q("SELECT COUNT(*) FROM facts"),
            "facts_current": q("SELECT COUNT(*) FROM facts WHERE valid_to IS NULL AND pending=0"),
            "facts_pending": q("SELECT COUNT(*) FROM facts WHERE valid_to IS NULL AND pending=1"),
            "decisions": q("SELECT COUNT(*) FROM decisions"),
            "episodes": q("SELECT COUNT(*) FROM episodes"),
            "open_conflicts": q("SELECT COUNT(*) FROM conflicts WHERE status='open'"),
        }

    def version_info(self) -> dict:
        """Identity of the running build, so a client can verify which
        deployment it is actually talking to (added 0.2.3 after a tester
        could not confirm a fix had reached the hosted copy)."""
        import os
        version = None
        try:
            from importlib.metadata import version as _pkg_version
            version = _pkg_version("mirsad-mcp")
        except Exception:
            pass
        if not version:
            from . import __version__ as version  # type: ignore
        return {
            "package_version": version,
            "commit": os.environ.get("MIRSAD_COMMIT") or "unknown",
            "deployed_at": os.environ.get("MIRSAD_DEPLOYED_AT"),
            "schema_version": SCHEMA_VERSION,
            "runtime": "local",
        }
