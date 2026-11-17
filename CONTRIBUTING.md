# Contributing to MIRSAD

Thanks for considering a contribution. MIRSAD is maintained by one
person in the evenings, so small, focused pull requests get reviewed
fastest.

## How to contribute

1. **Open an issue first** for anything beyond a typo fix, so the change
   can be discussed before you invest time in it.
2. **Fork and branch** — work on a topic branch, not on `main`.
3. **Open a pull request** against `main` describing what changed and
   why. Link the issue it addresses.

## Requirements for acceptable contributions

- **Tests for new functionality.** When you add or change behaviour, add
  or update tests in the same pull request. This is the project's
  standing policy, not a suggestion: recent features (tool annotations,
  the recall fixes, the server hardening) all landed together with their
  test suites, and CI runs every suite on every push.
- **All checks green.** CI must pass: the full test suite plus the
  packaging smoke test (a clean `pip install .` whose `mirsad-mcp`
  command must answer an MCP handshake).
- **Lint clean.** The project lints with ruff's default ruleset
  (`select = ["E4", "E7", "E9", "F"]` in `pyproject.toml`). Run
  `ruff check mirsad/ demo.py test_*.py` before pushing.
- **No private data.** Never commit a real memory database, tokens, or
  personal information. Test fixtures use invented data only.

## Running the tests

```bash
python3 -m venv .venv && .venv/bin/pip install "mcp[cli]<2"
.venv/bin/python demo.py
.venv/bin/python test_graph.py
.venv/bin/python test_mcp.py
.venv/bin/python test_hardening.py
.venv/bin/python test_recall_like.py
.venv/bin/python test_recall_echo.py
.venv/bin/python test_annotations.py
```

## Reporting bugs and security issues

- Bugs and feature requests: GitHub Issues.
- Security vulnerabilities: **privately**, per [SECURITY.md](SECURITY.md)
  — never as a public issue.
