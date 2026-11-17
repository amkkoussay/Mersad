"""Every tool must declare all four MCP annotations as explicit
booleans, matching the handler's real behaviour — directories
(e.g. OpenAI's) reject tools with missing or non-boolean hints."""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
os.environ.setdefault("MIRSAD_DB", ":memory:")

from mirsad.mcp_server import mcp  # noqa: E402

HINTS = ("readOnlyHint", "destructiveHint", "idempotentHint",
         "openWorldHint")
READ_ONLY_TOOLS = {"current_fact", "timeline", "why", "recall",
                   "open_conflicts", "memory_stats", "version_info"}


def test_all_tools_fully_annotated():
    tools = asyncio.run(mcp.list_tools())
    assert len(tools) == 12, [t.name for t in tools]
    for t in tools:
        a = t.annotations
        assert a is not None, t.name
        for h in HINTS:
            v = getattr(a, h)
            assert isinstance(v, bool), (t.name, h, v)
        assert a.openWorldHint is False, t.name  # local store only
        assert a.readOnlyHint is (t.name in READ_ONLY_TOOLS), t.name


if __name__ == "__main__":
    test_all_tools_fully_annotated()
    print("annotations: 12/12 tools fully declared")
