"""End-to-end MCP protocol test: starts the real MIRSAD MCP server over
streamable HTTP and talks to it with the official MCP client, the same way
real MCP clients would. Fails loudly on any mismatch."""

import asyncio
import os
import subprocess
import sys
import tempfile
import time

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


def no_proxy_factory(headers=None, timeout=None, auth=None):
    # The sandbox exports proxy env vars that httpx mis-parses; localhost
    # must be reached directly anyway.
    return httpx.AsyncClient(headers=headers, timeout=timeout, auth=auth,
                             trust_env=False)

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = 8871
URL = f"http://127.0.0.1:{PORT}/mcp"


async def main() -> int:
    db = os.path.join(tempfile.mkdtemp(), "mcp.db")
    env = dict(os.environ, MIRSAD_DB=db, MIRSAD_PORT=str(PORT))
    proc = subprocess.Popen(
        [os.path.join(HERE, ".venv", "bin", "python"), "-m", "mirsad.mcp_server"],
        cwd=HERE, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        # wait for the server to accept connections
        import urllib.request
        for _ in range(60):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{PORT}/mcp", timeout=1)
                break
            except Exception:
                if proc.poll() is not None:
                    print("FAIL  server process died during startup")
                    return 1
                time.sleep(0.5)

        async with streamablehttp_client(URL, httpx_client_factory=no_proxy_factory) as (read, write, _):
            async with ClientSession(read, write) as s:
                await s.initialize()
                tools = await s.list_tools()
                names = {t.name for t in tools.tools}
                expected = {"remember_fact", "current_fact", "timeline", "record_decision",
                            "record_outcome", "why", "recall", "open_conflicts", "add_alias",
                            "resolve_conflict", "memory_stats"}
                print(("PASS" if expected <= names else "FAIL") +
                      f"  tool list exposes all {len(expected)} tools"
                      + ("" if expected <= names else f" — missing {expected - names}"))
                if not expected <= names:
                    return 1

                import json as _json
                def rj(res):
                    assert not res.isError, res.content
                    return _json.loads(res.content[0].text)

                r = await s.call_tool("record_decision", {
                    "title": "Use Redis for caching", "agent": "agent-a",
                    "reasons": ["Cut response latency"],
                    "alternatives_rejected": ["Memcached"],
                    "entities": ["Redis", "Project"],
                })
                did = rj(r)["decision_id"]
                print(f"PASS  record_decision via MCP -> id {did}")

                await s.call_tool("remember_fact", {
                    "subject": "Project", "predicate": "cache_backend",
                    "value": "Redis", "agent": "agent-b",
                })
                r = await s.call_tool("current_fact", {"subject": "Project", "predicate": "cache_backend"})
                val = rj(r)["object"]
                wrote_by = rj(r)["source_agent"]
                ok = val == "Redis" and wrote_by == "agent-b"
                print(("PASS" if ok else "FAIL") +
                      f"  cross-agent read: agent-b wrote, another client reads '{val}' (by {wrote_by})")
                if not ok:
                    return 1

                r = await s.call_tool("why", {"topic": "Redis"})
                chain = rj(r)["decisions"]
                ok = len(chain) == 1 and chain[0]["reasons"] == ["Cut response latency"]
                print(("PASS" if ok else "FAIL") + "  why() via MCP returns reason chain")
                return 0 if ok else 1
    finally:
        proc.terminate()
        proc.wait(timeout=5)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
