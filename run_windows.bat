@echo off
REM MIRSAD server launcher for Windows — double-click or run from cmd.
cd /d %~dp0
if not exist .venv (
    echo Creating virtual environment...
    py -m venv .venv
    .venv\Scripts\pip install -r requirements.txt
)
if not defined MIRSAD_DB set MIRSAD_DB=%USERPROFILE%\mirsad.db
echo MIRSAD database: %MIRSAD_DB%
echo Server will listen on http://127.0.0.1:8765/mcp
.venv\Scripts\python -m mirsad.mcp_server
pause
