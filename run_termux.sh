#!/data/data/com.termux/files/usr/bin/bash
# MIRSAD server launcher for Termux (Android) / Linux.
cd "$(dirname "$0")"
if [ ! -d .venv ]; then
    echo "Creating virtual environment..."
    python -m venv .venv
    .venv/bin/pip install -r requirements.txt
fi
export MIRSAD_DB="${MIRSAD_DB:-$HOME/mirsad.db}"
echo "MIRSAD database: $MIRSAD_DB"
echo "Server will listen on http://127.0.0.1:8765/mcp"
exec .venv/bin/python -m mirsad.mcp_server
