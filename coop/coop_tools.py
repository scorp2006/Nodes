"""Code-coordination tools for CooperBench agents, over the FalkorDB world graph.

One long-lived HTTP MCP server (port 8774). Each agent's MCP config sends:
  X-World-Graph : the pair's world graph (loaded by world_graph.py)
  X-Agent       : the CooperBench agent id (agent1, agent2)
  X-Log         : host path of a JSONL file that records every tool call

Tools (the agent never writes Cypher):
  declare_plan(functions)  record which functions you intend to change; returns overlaps
                           with other agents' plans within 3 calls (Q3)
  check_conflicts()        re-run the overlap check against everyone's current plans
  impact_of(function)      callers and callees within 2 calls of a function
"""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

from falkordb import FalkorDB
from mcp.server.mcpserver import Context, MCPServer

sys.path.insert(0, str(Path(__file__).parent))
from world_graph import CONFLICTS  # noqa: E402

PORT = 8774


def normalize(name: str) -> str:
    """'src/click/termui.py:edit' or 'termui.edit()' -> 'click.termui.edit' style dotted path."""
    n = name.strip().rstrip("()").replace("\\", "/")
    n = re.sub(r"\.py(?=[:/.]|$)", "", n)
    n = n.replace("::", ".").replace(":", ".").replace("/", ".")
    return n.removeprefix("src.")


def resolve_names(g, names: list[str]) -> tuple[list[str], dict[str, list[str]]]:
    """Exact qualname, else unique suffix match. Returns (resolved, {name: candidates or []})."""
    found, problems = [], {}
    for raw in names:
        n = normalize(raw)
        rows = g.ro_query("MATCH (f:Function) WHERE f.qualname = $n OR f.qualname ENDS WITH $s "
                          "RETURN f.qualname ORDER BY size(f.qualname) LIMIT 6",
                          {"n": n, "s": "." + n}).result_set
        exact = [r[0] for r in rows if r[0] == n]
        if exact or len(rows) == 1:
            found.append((exact or [rows[0][0]])[0])
        else:
            problems[raw] = [r[0] for r in rows]
    return found, problems


def conflicts_text(g, agent: str) -> str:
    rows = g.ro_query(CONFLICTS, {"me": agent}).result_set
    if not rows:
        return "No overlap: none of your planned functions is within 3 calls of another agent's plan."
    lines = [f"- {other} plans {theirs}; your {mine} is {hops} call(s) away "
             f"(path: {' -> '.join(v for v in via[:-2])})" for other, mine, theirs, hops, via in rows]
    return "Possible conflicts with other agents' plans:\n" + "\n".join(lines)


def build_server(db: FalkorDB) -> MCPServer:
    server = MCPServer("code_graph")

    def who(ctx: Context):
        h = ctx.headers or {}
        return db.select_graph(h["x-world-graph"]), h["x-agent"], h.get("x-log")

    def logged(log, agent, tool, result, **kw):
        if log:
            with open(log, "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"t": time.time(), "agent": agent, "tool": tool, **kw}) + "\n")
        return result

    @server.tool(name="declare_plan")
    def declare_plan(functions: list[str], ctx: Context) -> str:
        """Record the functions you intend to change (e.g. ["click.termui.edit"]). Replaces your previous
        plan. Returns any overlap with other agents' plans within 3 calls."""
        g, agent, log = who(ctx)
        found, problems = resolve_names(g, functions)
        g.query("""
            MERGE (a:Agent {id: $agent})
            MERGE (s:Step {id: $agent + '-plan'}) SET s.status = 'planned'
            MERGE (a)-[:PLANS]->(s)
            WITH s
            OPTIONAL MATCH (s)-[old:TOUCHES]->()
            DELETE old
            WITH DISTINCT s
            UNWIND $found AS q
            MATCH (fn:Function {qualname: q})
            MERGE (s)-[:TOUCHES]->(fn)
            """, {"agent": agent, "found": found})
        note = ""
        if problems:
            note = "\nNot recorded (no unique match): " + "; ".join(
                f"{k} -> {', '.join(v) if v else 'no such function'}" for k, v in problems.items())
        result = f"Plan recorded: {', '.join(found) or 'nothing'}." + note + "\n" + conflicts_text(g, agent)
        return logged(log, agent, "declare_plan", result, functions=functions, resolved=found,
                      conflict="Possible conflicts" in result)

    @server.tool(name="check_conflicts")
    def check_conflicts(ctx: Context) -> str:
        """Re-check your current plan against every other agent's current plan."""
        g, agent, log = who(ctx)
        result = conflicts_text(g, agent)
        return logged(log, agent, "check_conflicts", result, conflict="Possible conflicts" in result)

    @server.tool(name="impact_of")
    def impact_of(function: str, ctx: Context) -> str:
        """List the functions that call, or are called by, `function` within 2 calls."""
        g, agent, log = who(ctx)
        found, problems = resolve_names(g, [function])
        if not found:
            cands = problems.get(function) or []
            return logged(log, agent, "impact_of", f"No unique function named {function}. "
                          + (f"Candidates: {', '.join(cands)}" if cands else "Nothing matches."), function=function)
        rows = g.ro_query("""
            MATCH (f:Function {qualname: $q})
            OPTIONAL MATCH (caller:Function)-[:CALLS*1..2]->(f)
            WITH f, collect(DISTINCT caller.qualname) AS callers
            OPTIONAL MATCH (f)-[:CALLS*1..2]->(callee:Function)
            RETURN callers, collect(DISTINCT callee.qualname)
            """, {"q": found[0]}).result_set
        callers, callees = rows[0] if rows else ([], [])
        result = (f"{found[0]}\n  called by (<=2 calls): {', '.join(callers[:25]) or 'nothing found'}\n"
                  f"  calls (<=2 calls): {', '.join(callees[:25]) or 'nothing found'}\n"
                  "Note: calls made through attributes, parameters or inheritance are not tracked.")
        return logged(log, agent, "impact_of", result, function=function)

    return server


if __name__ == "__main__":
    from mcp.server.transport_security import TransportSecuritySettings

    # Containers reach us as host.docker.internal; keep DNS-rebinding protection on, allow only these hosts.
    hosts = [f"{h}:{PORT}" for h in ("127.0.0.1", "localhost", "host.docker.internal")]
    security = TransportSecuritySettings(enable_dns_rebinding_protection=True, allowed_hosts=hosts, allowed_origins=[])
    build_server(FalkorDB(host="localhost", port=6379)).run(
        "streamable-http", host="127.0.0.1", port=PORT, transport_security=security)
