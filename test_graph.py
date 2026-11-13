"""Acceptance checks for the live graph snapshot + viewer (can fail).

Scenario mirrors what the map must show: an entity with a fact, a decision
about it, a live cross-agent conflict flag, and a change counter that
moves on every write so the page knows to redraw.
"""

import os
import tempfile

from mirsad.core import Mirsad
from mirsad.viewer import VIEWER_HTML

failures = []


def check(name, cond):
    print(("PASS  " if cond else "FAIL  ") + name)
    if not cond:
        failures.append(name)


db = os.path.join(tempfile.mkdtemp(), "graph.db")
m = Mirsad(db)

v0 = m.graph_version()
m.remember_fact("Sfax", "weather", "rainy", source_agent="agent-a")
v1 = m.graph_version()
check("change counter moves on a write", v1 > v0)

d = m.record_decision(
    title="Pick Sfax for the field test",
    made_by="owner",
    description="Weather window matters.",
    reasons=["Rain expected this week"],
    entities=["Sfax"],
)
g = m.graph_snapshot()
kinds = {}
for n in g["nodes"]:
    kinds.setdefault(n["kind"], []).append(n["label"])
check("entity node exists", "Sfax" in kinds.get("entity", []))
check("value node exists for a non-entity object", "rainy" in kinds.get("value", []))
check("decision node exists", "Pick Sfax for the field test" in kinds.get("decision", []))
check("fact edge carries the predicate",
      any(e["label"] == "weather" and e["kind"] == "fact" for e in g["edges"]))
check("decision edge links decision to entity",
      any(e["label"] == "involves" and e["kind"] == "decision" for e in g["edges"]))
sfax = next(n for n in g["nodes"] if n["label"] == "Sfax")
det = g["details"][sfax["id"]]
check("entity story lists the current fact",
      any(f["object_text"] == "rainy" for f in det["facts"]))
check("entity story lists the decision it took part in",
      "Pick Sfax for the field test" in det["decisions"])
dec_node = next(n for n in g["nodes"] if n["kind"] == "decision")
ddet = g["details"][dec_node["id"]]
check("decision story carries its reason",
      ddet["reasons"] and ddet["reasons"][0] == "Rain expected this week")

m.remember_fact("Sfax", "weather", "sunny", source_agent="agent-b")
g2 = m.graph_snapshot()
sfax2 = next(n for n in g2["nodes"] if n["label"] == "Sfax")
check("cross-agent overwrite flags the node as conflicted",
      sfax2.get("conflict") is True)
check("snapshot reports the open conflict", len(g2["open_conflicts"]) == 1)
check("stats counts nodes and edges",
      g2["stats"]["nodes"] == len(g2["nodes"]) and g2["stats"]["edges"] == len(g2["edges"]))
check("viewer page ships a canvas and polls /graph",
      "<canvas" in VIEWER_HTML and "fetch('/graph')" in VIEWER_HTML)

print()
if failures:
    print(f"RESULT: {len(failures)} FAILED")
    raise SystemExit(1)
print("RESULT: ALL PASS")
