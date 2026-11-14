# Installing MIRSAD (for AI agents)

MIRSAD is a local-first MCP memory server published on PyPI as `mirsad-mcp`.
It needs Python 3.11+ and no API keys. Memory is stored in a local SQLite
file — nothing leaves the machine.

## 1. Install / run

No permanent install is needed; run it with `uvx`:

```bash
uvx mirsad-mcp
```

or install it:

```bash
pip install mirsad-mcp
```

## 2. Configure your MCP client (stdio)

The server speaks MCP over stdio when `MIRSAD_TRANSPORT=stdio` is set.
Add this to the client's MCP configuration (Claude Desktop, Cline, Cursor
and most other clients use this shape):

```json
{
  "mcpServers": {
    "mirsad": {
      "command": "uvx",
      "args": ["mirsad-mcp"],
      "env": {
        "MIRSAD_TRANSPORT": "stdio"
      }
    }
  }
}
```

Optional environment variables:

- `MIRSAD_DB` — path of the SQLite database (default: `~/mirsad.db`).
  Point several clients at the same file and they share one memory.

## 3. Verify

After connecting, call the `memory_stats` tool. A fresh install answers
with an empty memory (0 entities, 0 facts). Then `remember_fact` can store
the first fact and `recall` can find it again.

## Alternative: HTTP mode

Without `MIRSAD_TRANSPORT`, the server starts in streamable-HTTP mode on
port 8765 (`MIRSAD_PORT` overrides it), serving `/mcp` plus a live graph
viewer at `/`.
