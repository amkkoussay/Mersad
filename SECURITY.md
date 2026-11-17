# Security policy

## Supported versions

Only the latest release of `mirsad-mcp` is supported with security fixes.
Fixes are published as new patch releases on PyPI and GitHub Releases.

## Reporting a vulnerability

Please report vulnerabilities **privately** — do not open a public issue
for a security problem.

- Preferred: GitHub's private vulnerability reporting — the
  **"Report a vulnerability"** button under this repository's
  **Security** tab.
- Or by email: **koussaymehdouani@gmail.com**

You should receive an initial response within a few days. (The maintainer
is a student and works on this project in the evenings — responses may be
slower during exam periods, but every report is read.)

Please include, as far as you can:

- the affected version (`version_info` output helps),
- a minimal reproduction or proof of concept,
- the impact as you understand it.

## Scope notes

MIRSAD is local-first: the server stores its data in a single SQLite file
and, by default, the installed `mirsad-mcp` command speaks stdio to the
client that launched it. If you serve it over HTTP
(`MIRSAD_TRANSPORT=streamable-http`), you are responsible for putting it
behind your own authentication and network controls — the HTTP transport
ships without built-in authentication, and the default bind is loopback
(`127.0.0.1`) for that reason.
