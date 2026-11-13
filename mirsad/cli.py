"""MIRSAD command line — the same operations the MCP tools expose.

Examples:
    python -m mirsad.cli --db ~/mirsad.db fact "Project X" uses_cache Redis --agent agent-a
    python -m mirsad.cli --db ~/mirsad.db why Redis
    python -m mirsad.cli --db ~/mirsad.db export > DECISIONS.md
"""

from __future__ import annotations

import argparse
import json

from .core import Mirsad


def main() -> None:
    p = argparse.ArgumentParser(prog="mirsad")
    p.add_argument("--db", default="mirsad.db")
    sub = p.add_subparsers(dest="cmd", required=True)

    f = sub.add_parser("fact")
    f.add_argument("subject")
    f.add_argument("predicate")
    f.add_argument("value")
    f.add_argument("--agent", default="user")

    c = sub.add_parser("current")
    c.add_argument("subject")
    c.add_argument("predicate")

    t = sub.add_parser("timeline")
    t.add_argument("subject")

    w = sub.add_parser("why")
    w.add_argument("topic")

    r = sub.add_parser("recall")
    r.add_argument("query")

    sub.add_parser("conflicts")
    sub.add_parser("stats")
    sub.add_parser("export")

    args = p.parse_args()
    m = Mirsad(args.db)

    if args.cmd == "fact":
        out = m.remember_fact(args.subject, args.predicate, args.value, source_agent=args.agent)
    elif args.cmd == "current":
        out = m.current_fact(args.subject, args.predicate)
    elif args.cmd == "timeline":
        out = m.timeline(args.subject)
    elif args.cmd == "why":
        out = m.why(args.topic)
    elif args.cmd == "recall":
        out = m.recall(args.query)
    elif args.cmd == "conflicts":
        out = m.open_conflicts()
    elif args.cmd == "stats":
        out = m.stats()
    elif args.cmd == "export":
        print(m.export_markdown())
        return
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
