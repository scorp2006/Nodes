"""Nodes: a shared FalkorDB graph that a team of coding agents coordinates through. No MCP here, just graph operations.

Graph model (one graph per team):
  (:File)-[:CONTAINS]->(:Function)-[:CALLS]->(:Function)          the code, parsed from the repo
  (:Agent)-[:PLANS]->(:Step)-[:TOUCHES]->(:Function)               what each agent intends to change
  (:Function {claimed_by, version})                                ownership, set by an atomic compare-and-set
  (:Agent)-[:POSTED {seq}]->(:Fact {text, source, checked})        the board: facts checked against their source

Hardened over the experiment server (coop/coop_tools.py, kept frozen for the running study):
  - names resolve by indexed exact match first; the suffix scan runs only for names that miss
  - the status digest is scoped to the caller (its plan, claims, overlaps), so it stays small as the team grows
  - claim is one query (compare-and-set and read-back together), and claims can be released
  - agent names and fact sources are validated; a fact's source must be a file inside the workspace
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "coop"))
from world_graph_v2 import parse_repo  # noqa: E402

NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")
MAX_HOPS = 3

CONFLICTS = """
MATCH (me:Agent {id: $me})-[:PLANS]->(:Step)-[:TOUCHES]->(f:Function)
MATCH p = (f)-[:CALLS*0..3]-(g:Function)<-[:TOUCHES]-(:Step)<-[:PLANS]-(other:Agent)
WHERE other <> me
RETURN other.id, f.qualname, g.qualname, length(p) - 2 AS hops, [n IN nodes(p) | coalesce(n.qualname, n.id)]
ORDER BY hops LIMIT $limit
"""


def check_name(agent: str) -> str:
    if not isinstance(agent, str) or not NAME.match(agent):
        raise ValueError("agent name must be 1-64 letters, digits, '_', '.', ':' or '-'")
    return agent


def load_repo(g, package_dir: Path, batch: int = 1000) -> dict:
    """Parse a Python package and (re)load it as the team's code graph. Returns parser stats."""
    funcs, calls, _, stats = parse_repo(package_dir, shared_state=False)
    g.query("MATCH (n) DETACH DELETE n")
    # claimed_by is indexed: the digest looks up each agent's claims (431 ms -> 13 ms per digest at 100 agents)
    for q in ("CREATE INDEX FOR (f:Function) ON (f.qualname)", "CREATE INDEX FOR (f:Function) ON (f.claimed_by)",
              "CREATE INDEX FOR (f:File) ON (f.path)", "CREATE INDEX FOR (a:Agent) ON (a.id)"):
        try:
            g.query(q)
        except Exception:  # noqa: BLE001  index already exists
            pass
    for i in range(0, len(funcs), batch):
        g.query("UNWIND $rows AS r MERGE (file:File {path: r.file}) "
                "MERGE (f:Function {qualname: r.qualname}) SET f.file = r.file, f.line = r.line, f.version = 0 "
                "MERGE (file)-[:CONTAINS]->(f)", {"rows": funcs[i:i + batch]})
    for i in range(0, len(calls), batch):
        g.query("UNWIND $rows AS r MATCH (a:Function {qualname: r.src}), (b:Function {qualname: r.dst}) "
                "MERGE (a)-[:CALLS]->(b)", {"rows": calls[i:i + batch]})
    return stats


def normalize(name: str) -> str:
    """'src/click/termui.py:edit' or 'termui.edit()' -> 'click.termui.edit' style."""
    n = name.strip().rstrip("()").replace("\\", "/")
    n = re.sub(r"\.py(?=[:/.]|$)", "", n)
    return n.replace("::", ".").replace(":", ".").replace("/", ".").removeprefix("src.")


def resolve(g, names: list[str]) -> tuple[list[str], dict[str, list[str]]]:
    """Exact qualname (indexed), else a unique suffix match. Returns (resolved, {name: candidates or []})."""
    norm = {raw: normalize(raw) for raw in names}
    exact = {r[0] for r in g.ro_query("UNWIND $n AS q MATCH (f:Function {qualname: q}) RETURN f.qualname",
                                      {"n": list(set(norm.values()))}).result_set}
    found, problems = [], {}
    for raw, n in norm.items():
        if n in exact:
            found.append(n)
            continue
        rows = [r[0] for r in g.ro_query("MATCH (f:Function) WHERE f.qualname ENDS WITH $s RETURN f.qualname "
                                         "ORDER BY size(f.qualname) LIMIT 6", {"s": "." + n}).result_set]
        if len(rows) == 1:
            found.append(rows[0])
        else:
            problems[raw] = rows
    return found, problems


def touch(g, agent: str) -> None:
    g.query("MERGE (:Agent {id: $a})", {"a": check_name(agent)})


def set_plan(g, agent: str, found: list[str]) -> None:
    g.query("""
        MERGE (a:Agent {id: $agent})
        MERGE (s:Step {id: $agent + '-plan'})
        MERGE (a)-[:PLANS]->(s)
        WITH s
        OPTIONAL MATCH (s)-[old:TOUCHES]->()
        DELETE old
        WITH DISTINCT s
        UNWIND $found AS q
        MATCH (fn:Function {qualname: q})
        MERGE (s)-[:TOUCHES]->(fn)
        """, {"agent": check_name(agent), "found": found})


def claim(g, agent: str, found: list[str]) -> tuple[list[str], dict[str, str]]:
    """Atomic compare-and-set in one query: take every requested function nobody owns yet.
    Returns (functions the agent owns among those asked, {function: owner} for ones others own)."""
    rows = g.query("""
        UNWIND $found AS q
        MATCH (f:Function {qualname: q})
        WITH f, f.claimed_by IS NULL AS free
        SET f.claimed_by = CASE WHEN free THEN $agent ELSE f.claimed_by END,
            f.version = CASE WHEN free THEN f.version + 1 ELSE f.version END
        RETURN f.qualname, f.claimed_by
        """, {"agent": check_name(agent), "found": found}).result_set
    return [q for q, o in rows if o == agent], {q: o for q, o in rows if o != agent}


def release(g, agent: str, found: list[str]) -> list[str]:
    rows = g.query("UNWIND $found AS q MATCH (f:Function {qualname: q, claimed_by: $a}) "
                   "SET f.claimed_by = NULL RETURN f.qualname", {"a": check_name(agent), "found": found}).result_set
    return [r[0] for r in rows]


def mark_done(g, agent: str, commit: str) -> int:
    g.query("MATCH (a:Agent {id: $a}) SET a.commit = $c, a.done_at = timestamp()", {"a": check_name(agent), "c": commit})
    return g.ro_query("MATCH (f:Function {claimed_by: $a}) RETURN count(f)", {"a": agent}).result_set[0][0]


def conflicts(g, agent: str, limit: int = 10) -> list:
    return g.ro_query(CONFLICTS, {"me": agent, "limit": limit}).result_set


def digest(g, agent: str) -> str:
    """The caller's view, bounded in size: its plan and claims, its overlaps, who owns what it plans or overlaps."""
    plan = [r[0] for r in g.ro_query("MATCH (:Agent {id: $a})-[:PLANS]->(:Step)-[:TOUCHES]->(f:Function) "
                                     "RETURN f.qualname ORDER BY f.qualname LIMIT 50", {"a": agent}).result_set]
    mine = g.ro_query("MATCH (f:Function {claimed_by: $a}) RETURN count(f)", {"a": agent}).result_set[0][0]
    ov = conflicts(g, agent)
    related = sorted(set(plan) | {x for _, m, t, _, _ in ov for x in (m, t)})
    owners = g.ro_query("UNWIND $r AS q MATCH (f:Function {qualname: q}) WHERE f.claimed_by IS NOT NULL "
                        "AND f.claimed_by <> $a RETURN f.qualname, f.claimed_by", {"r": related, "a": agent}).result_set
    team = g.ro_query("MATCH (a:Agent) OPTIONAL MATCH (f:Function {claimed_by: a.id}) "
                      "RETURN a.id, a.commit, count(f) ORDER BY a.id LIMIT 30").result_set
    lines = [f"Your plan: {', '.join(plan) or 'not declared yet'}", f"You own {mine} function(s)."]
    lines.append("Overlaps: " + ("; ".join(f"your {m} vs {o}'s {t} ({h} call(s) apart)" for o, m, t, h, _ in ov)
                                 if ov else f"none within {MAX_HOPS} calls."))
    if owners:
        lines.append("Owned by others (do not edit): " + "; ".join(f"{q} ({o})" for q, o in owners))
    lines.append("Team: " + "; ".join(f"{a} owns {n}" + (f", done at {c}" if c else "") for a, c, n in team))
    return "\n".join(lines)


def impact(g, function: str) -> str:
    found, problems = resolve(g, [function])
    if not found:
        cands = problems.get(function) or []
        return f"No unique function named {function}. " + (f"Candidates: {', '.join(cands)}" if cands else "Nothing matches.")
    file, line, callers, callees = g.ro_query("""
        MATCH (f:Function {qualname: $q})
        OPTIONAL MATCH (c:Function)-[:CALLS*1..2]->(f)
        WITH f, collect(DISTINCT c.qualname) AS callers
        OPTIONAL MATCH (f)-[:CALLS*1..2]->(d:Function)
        RETURN f.file, f.line, callers, collect(DISTINCT d.qualname)
        """, {"q": found[0]}).result_set[0]
    return (f"{found[0]} ({file}:{line})\n  called by (<=2 calls): {', '.join(callers[:25]) or 'nothing found'}\n"
            f"  calls (<=2 calls): {', '.join(callees[:25]) or 'nothing found'}")


# ------------------------------------------------------------------ board -------

def _norm_text(s: str) -> str:
    return " ".join(s.split()).lower()


def source_path(workspace: Path, source: str) -> Path | None:
    """'path/to/file.py' or 'path/to/file.py:10-20' inside the workspace, else None."""
    path = source.rsplit(":", 1)[0] if re.search(r":\d+(-\d+)?$", source) else source
    p = (workspace / path).resolve()
    return p if p.is_file() and p.is_relative_to(workspace.resolve()) else None


def check_source(workspace: Path, text: str, source: str) -> bool:
    """True if `text` appears (whitespace- and case-insensitive) in the source file, or in its line range."""
    p = source_path(workspace, source)
    if p is None:
        return False
    lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    m = re.search(r":(\d+)(?:-(\d+))?$", source)
    if m:
        a = int(m.group(1))
        lines = lines[a - 1:int(m.group(2) or a)]
    return _norm_text(text) in _norm_text("\n".join(lines))


def share_fact(g, workspace: Path, agent: str, text: str, source: str) -> bool:
    """Post a fact quoting a workspace file. It is marked source-checked only if the quote is in that file."""
    checked = check_source(workspace, text, source)
    g.query("""
        MERGE (a:Agent {id: $a})
        WITH a
        OPTIONAL MATCH ()-[prev:POSTED]->()
        WITH a, count(prev) AS seq
        CREATE (a)-[:POSTED {seq: seq}]->(:Fact {text: $t, source: $s, checked: $c})
        """, {"a": check_name(agent), "t": text[:2000], "s": source[:300], "c": checked})
    return checked


def facts(g, limit: int = 50) -> list:
    return g.ro_query("MATCH (a:Agent)-[p:POSTED]->(f:Fact) RETURN a.id, f.text, f.source, f.checked "
                      "ORDER BY p.seq DESC LIMIT $n", {"n": limit}).result_set


def state(g) -> dict:
    """Everything the web view shows."""
    agents = [{"id": a, "commit": c, "plan": p, "owns": o} for a, c, p, o in g.ro_query("""
        MATCH (a:Agent)
        OPTIONAL MATCH (a)-[:PLANS]->(:Step)-[:TOUCHES]->(f:Function)
        WITH a, collect(DISTINCT f.qualname) AS plan
        OPTIONAL MATCH (o:Function {claimed_by: a.id})
        RETURN a.id, a.commit, plan, collect(o.qualname) ORDER BY a.id""").result_set]
    paths = []
    for a in agents:
        for other, mine, theirs, hops, via in conflicts(g, a["id"], limit=5):
            if a["id"] < other:
                paths.append({"a": a["id"], "b": other, "mine": mine, "theirs": theirs, "hops": hops, "via": via})
    board = [{"agent": a, "text": t, "source": s, "checked": c} for a, t, s, c in facts(g)]
    size = g.ro_query("MATCH (f:Function) RETURN count(f)").result_set[0][0]
    return {"agents": agents, "conflicts": paths, "facts": board, "functions": size}
