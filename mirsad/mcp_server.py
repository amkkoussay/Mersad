"""MIRSAD MCP server — the bridge every assistant connects to.

Run (HTTP, for Claude custom connectors / ChatGPT plugins):
    MIRSAD_DB=~/mirsad.db python -m mirsad.mcp_server
Run (stdio, for local MCP clients):
    MIRSAD_TRANSPORT=stdio MIRSAD_DB=~/mirsad.db python -m mirsad.mcp_server

Every write tool takes `agent`: the name of the assistant writing
("claude", "chatgpt", "muse", "user"...). In production this identity
should come from the OAuth login, not from the caller — see README.
"""

from __future__ import annotations

import os

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from pathlib import Path

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response

from .core import Mirsad
from .viewer import VIEWER_HTML

DB_PATH = os.environ.get("MIRSAD_DB", os.path.expanduser("~/mirsad.db"))
m = Mirsad(DB_PATH)
# Bind loopback by default: these tools read and write the user's whole
# memory with no authentication layer, so listening on every interface
# would hand the memory to anyone on the LAN who finds the port. The
# hosted edition is the one built for remote access (behind its token).
# Set MIRSAD_HOST=0.0.0.0 only behind your own auth/reverse proxy.
mcp = FastMCP("mirsad", host=os.environ.get("MIRSAD_HOST", "127.0.0.1"),
              port=int(os.environ.get("MIRSAD_PORT", "8765")))

# Tool annotations, declared honestly per handler behaviour so hosts can
# warn users accurately (and directories that require them accept the
# server). Nothing here talks to the outside world: openWorldHint is
# False everywhere. "Destructive" means the call can end the standing
# state of an existing record — remember_fact closes the fact it
# replaces (history is kept, currency is not), resolve_conflict closes
# an open conflict.
_READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False,
                             idempotentHint=True, openWorldHint=False)
_WRITE_NEW = ToolAnnotations(readOnlyHint=False, destructiveHint=False,
                             idempotentHint=False, openWorldHint=False)
_WRITE_REPLACE = ToolAnnotations(readOnlyHint=False, destructiveHint=True,
                                 idempotentHint=False, openWorldHint=False)
_WRITE_IDEMPOTENT = ToolAnnotations(readOnlyHint=False,
                                    destructiveHint=False,
                                    idempotentHint=True, openWorldHint=False)


@mcp.tool(annotations=_WRITE_REPLACE)
def remember_fact(subject: str, predicate: str, value: str, agent: str,
                  source_ref: str = "") -> dict:
    """Store a fact about a subject. If another value is currently recorded
    for the same subject+predicate, the old one is closed (not deleted) and,
    when a different agent overwrites it, a conflict is opened for the user.
    Always pass your own assistant name as `agent`."""
    return m.remember_fact(subject, predicate, value, source_agent=agent,
                           source_ref=source_ref or None)


@mcp.tool(annotations=_READ_ONLY)
def current_fact(subject: str, predicate: str) -> dict | None:
    """The currently valid value for subject+predicate, with provenance."""
    return m.current_fact(subject, predicate)


@mcp.tool(annotations=_READ_ONLY)
def timeline(subject: str) -> dict:
    """Every value ever recorded for a subject, with validity windows —
    including superseded ones. Use it to answer 'what changed and when?'"""
    return {"facts": m.timeline(subject)}


@mcp.tool(annotations=_WRITE_NEW)
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


@mcp.tool(annotations=_WRITE_IDEMPOTENT)
def add_alias(entity: str, alias: str) -> dict:
    """Register another name for an entity (another language, spelling or
    script) so future lookups in that name resolve to the same entity."""
    m.add_alias(entity, alias)
    return {"ok": True, "entity": entity, "alias": alias}


@mcp.tool(annotations=_WRITE_NEW)
def record_outcome(decision_id: int, outcome: str, agent: str) -> dict:
    """Attach a later outcome/result to an existing decision."""
    m.record_outcome(decision_id, outcome, recorded_by=agent)
    return {"ok": True}


@mcp.tool(annotations=_READ_ONLY)
def why(topic: str) -> dict:
    """Answer 'why did we choose X?' — full decision chains matching the
    topic: reasons, evidence, rejected alternatives, outcomes, and what
    superseded what."""
    return {"decisions": m.why(topic)}


@mcp.tool(annotations=_READ_ONLY)
def recall(query: str) -> dict:
    """Hybrid recall across facts, decisions and raw episodes."""
    return m.recall(query)


@mcp.tool(annotations=_READ_ONLY)
def open_conflicts() -> dict:
    """Disagreements between agents that are waiting for the user's ruling."""
    return {"conflicts": m.open_conflicts()}


@mcp.tool(annotations=_WRITE_REPLACE)
def resolve_conflict(conflict_id: int, resolution: str) -> dict:
    """Close a conflict with the user's ruling, kept on record."""
    m.resolve_conflict(conflict_id, resolution)
    return {"ok": True}


@mcp.tool(annotations=_READ_ONLY)
def memory_stats() -> dict:
    """Counts: entities, facts (total/current), decisions, open conflicts."""
    return m.stats()


@mcp.tool(annotations=_READ_ONLY)
def version_info() -> dict:
    """Identity of this running build: package version, source commit,
    deployment timestamp, schema version and runtime — so a client can
    verify which deployment it is connected to."""
    return m.version_info()


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
    """The memory as nodes + edges, for the viewer (polled live).
    Light payload: per-node details are NOT included — the viewer
    fetches them per click from /graph/node/{id}. A poll that passes
    ?v=<last seen version> gets a tiny {"unchanged": true} reply when
    nothing was written since, instead of the full snapshot."""
    v = request.query_params.get("v")
    current = m.graph_version()
    if v is not None and v.isdigit() and int(v) == current:
        return JSONResponse({"version": current, "unchanged": True})
    return JSONResponse(m.graph_snapshot(light=True))


@mcp.custom_route("/graph/node/{node_id}", methods=["GET"])
async def graph_node_json(request: Request) -> JSONResponse:
    """One node's story (entity facts / full decision chain), fetched
    when a viewer clicks it. Value nodes are derived client-side from
    the snapshot's edges and return 404 here by design."""
    detail = m.graph_node_detail(request.path_params["node_id"])
    if detail is None:
        return JSONResponse({"error": "unknown node"}, status_code=404)
    return JSONResponse(detail)


def main(transport: str | None = None) -> None:
    """Console entry point — the `mirsad-mcp` command installed from PyPI
    (pyproject [project.scripts] points here).

    Defaults to stdio: that is how MCP clients spawn an installed
    server. Set MIRSAD_TRANSPORT=streamable-http to serve HTTP."""
    mcp.run(transport=transport
            or os.environ.get("MIRSAD_TRANSPORT", "stdio"))


if __name__ == "__main__":
    # Direct module runs keep the HTTP default the connectors use.
    main(os.environ.get("MIRSAD_TRANSPORT", "streamable-http"))
