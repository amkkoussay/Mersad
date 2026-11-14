"""MIRSAD MCP server — the bridge every assistant connects to.

Run (HTTP, for remote MCP connectors):
    MIRSAD_DB=~/mirsad.db python -m mirsad.mcp_server
Run (stdio, for local MCP clients):
    MIRSAD_TRANSPORT=stdio MIRSAD_DB=~/mirsad.db python -m mirsad.mcp_server

Every write tool takes `agent`: the name of the assistant writing
("agent-a", "agent-b", "user"...). In production this identity
should come from the OAuth login, not from the caller — see README.
"""

from __future__ import annotations

import os

from mcp.server.fastmcp import FastMCP

from pathlib import Path

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response

from .core import Mirsad
from .viewer import VIEWER_HTML

DB_PATH = os.environ.get("MIRSAD_DB", os.path.expanduser("~/mirsad.db"))
m = Mirsad(DB_PATH)
mcp = FastMCP("mirsad", host="0.0.0.0", port=int(os.environ.get("MIRSAD_PORT", "8765")))


@mcp.tool()
def remember_fact(subject: str, predicate: str, value: str, agent: str,
                  source_ref: str = "") -> dict:
    """Store a fact about a subject. If another value is currently recorded
    for the same subject+predicate, the old one is closed (not deleted) and,
    when a different agent overwrites it, a conflict is opened for the user.
    Always pass your own assistant name as `agent`."""
    return m.remember_fact(subject, predicate, value, source_agent=agent,
                           source_ref=source_ref or None)


@mcp.tool()
def current_fact(subject: str, predicate: str) -> dict | None:
    """The currently valid value for subject+predicate, with provenance."""
    return m.current_fact(subject, predicate)


@mcp.tool()
def timeline(subject: str) -> dict:
    """Every value ever recorded for a subject, with validity windows —
    including superseded ones. Use it to answer 'what changed and when?'"""
    return {"facts": m.timeline(subject)}


@mcp.tool()
def record_decision(title: str, agent: str, description: str = "",
                    reasons: list[str] | None = None,
                    evidence: list[dict] | None = None,
                    alternatives_rejected: list[str] | None = None,
                    effects: list[str] | None = None,
                    entities: list[str] | None = None) -> dict:
    """Record a decision as ONE unit: the decision, why it was taken,
    the evidence behind it, which alternatives were rejected, and what it
    affects (files, functions, plans). Returns the decision id."""
    did = m.record_decision(title, made_by=agent, description=description,
                            reasons=reasons, evidence=evidence,
                            alternatives_rejected=alternatives_rejected,
                            effects=effects, entities=entities)
    return {"decision_id": did}


@mcp.tool()
def add_alias(entity: str, alias: str) -> dict:
    """Register another name for an entity (another language, spelling or
    script) so future lookups in that name resolve to the same entity."""
    m.add_alias(entity, alias)
    return {"ok": True, "entity": entity, "alias": alias}


@mcp.tool()
def record_outcome(decision_id: int, outcome: str, agent: str) -> dict:
    """Attach a later outcome/result to an existing decision."""
    m.record_outcome(decision_id, outcome, recorded_by=agent)
    return {"ok": True}


@mcp.tool()
def why(topic: str) -> dict:
    """Answer 'why did we choose X?' — full decision chains matching the
    topic: reasons, evidence, rejected alternatives, outcomes, and what
    superseded what."""
    return {"decisions": m.why(topic)}


@mcp.tool()
def recall(query: str) -> dict:
    """Hybrid recall across facts, decisions and raw episodes."""
    return m.recall(query)


@mcp.tool()
def open_conflicts() -> dict:
    """Disagreements between agents that are waiting for the user's ruling."""
    return {"conflicts": m.open_conflicts()}


@mcp.tool()
def resolve_conflict(conflict_id: int, resolution: str) -> dict:
    """Close a conflict with the user's ruling, kept on record."""
    m.resolve_conflict(conflict_id, resolution)
    return {"ok": True}


@mcp.tool()
def memory_stats() -> dict:
    """Counts: entities, facts (total/current), decisions, open conflicts."""
    return m.stats()


@mcp.custom_route("/", methods=["GET"])
async def viewer_page(request: Request) -> HTMLResponse:
    """The live memory map: watch the shared memory as a network while
    agents write to it, and click any node to read its story."""
    return HTMLResponse(VIEWER_HTML)


@mcp.custom_route("/logo.svg", methods=["GET"])
async def logo_svg(request: Request) -> Response:
    """The MIRSAD mark (observatory tower + lamp light + constellation)."""
    path = Path(__file__).resolve().parent.parent / "assets" / "logo-mark.svg"
    return Response(path.read_text(encoding="utf-8"), media_type="image/svg+xml")


@mcp.custom_route("/graph", methods=["GET"])
async def graph_json(request: Request) -> JSONResponse:
    """The whole memory as nodes + edges, for the viewer (polled live)."""
    return JSONResponse(m.graph_snapshot())


def main() -> None:
    transport = os.environ.get("MIRSAD_TRANSPORT", "streamable-http")
    mcp.run(transport=transport)


if __name__ == "__main__":
    main()
