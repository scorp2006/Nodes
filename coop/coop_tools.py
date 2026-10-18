"""Tool servers for CooperBench agents, over the FalkorDB world graph of the pair's repo.

Two long-lived HTTP MCP servers with the same mechanics and five tools each:
  graph   (port 8774): shared coordination state
      declare_plan(functions)   record what you intend to change; reports overlap with your partner
      claim(functions)          atomic compare-and-set: the first agent to claim a function owns it
      mark_committed(commit)    owner: say the functions you own are committed and pushed
      coordination_status()     overlaps, who owns what, whether the owner has committed, what to merge
      impact_of(function)       callers / callees within 2 calls
  placebo (port 8775): the same server and number of tools, all private, sharing nothing
      impact_of(function)       identical to the graph version (code facts only)
      record_plan(functions)    private plan, visible only to you
      my_plan()                 read your private plan
      write_note(text) / read_notes()   private notes

Each agent's MCP config sends X-World-Graph, X-Agent and X-Log headers (written by the launcher).
The agents never write Cypher.
"""
from __future__ import annotations

import asyncio
import json
import re
import sys
import time
from pathlib import Path

from falkordb import FalkorDB
from mcp.server.mcpserver import Context, MCPServer

sys.path.insert(0, str(Path(__file__).parent))
from world_graph import CONFLICTS  # noqa: E402

PORTS = {"graph": 8774, "placebo": 8775}


def code_fingerprint() -> str:
    """Hash of the server's code (this file + the shared query), to detect stale servers."""
    import hashlib

    here = Path(__file__).parent
    return hashlib.sha256(b"".join((here / f).read_bytes() for f in ("coop_tools.py", "world_graph.py"))).hexdigest()[:16]


def normalize(name: str) -> str:
    """'src/click/termui.py:edit' or 'termui.edit()' -> dotted 'click.termui.edit' style."""
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


def _problems_note(problems: dict) -> str:
    if not problems:
        return ""
    return "\nNot recorded (no unique match): " + "; ".join(
        f"{k} -> {', '.join(v) if v else 'no such function'}" for k, v in problems.items())


# ---------------------------------------------------------------- shared (graph) -------

def set_plan(g, agent: str, found: list[str]) -> None:
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


def overlaps(g, agent: str) -> list:
    return g.ro_query(CONFLICTS, {"me": agent}).result_set


def claim(g, agent: str, found: list[str]) -> tuple[list[str], dict[str, str]]:
    """One atomic query: take every requested function that nobody owns yet (compare-and-set on
    claimed_by + version). Returns (functions you now own, {function: owner} for ones others own)."""
    g.query("""
        UNWIND $found AS q
        MATCH (f:Function {qualname: q})
        WHERE f.claimed_by IS NULL
        SET f.claimed_by = $agent, f.version = f.version + 1
        """, {"agent": agent, "found": found})
    rows = g.ro_query("UNWIND $found AS q MATCH (f:Function {qualname: q}) RETURN q, f.claimed_by",
                      {"found": found}).result_set
    mine = [q for q, owner in rows if owner == agent]
    others = {q: owner for q, owner in rows if owner and owner != agent}
    return mine, others


def mark_committed(g, agent: str, commit: str) -> int:
    """Record that `agent` has committed and pushed its work at `commit`. Returns how many owned functions
    that covers (0 is fine for a builder)."""
    g.query("MATCH (a:Agent {id: $a}) SET a.commit = $c, a.committed_at = timestamp()", {"a": agent, "c": commit})
    rows = g.query("MATCH (f:Function {claimed_by: $a}) SET f.committed = true, f.commit = $c RETURN count(f)",
                   {"a": agent, "c": commit}).result_set
    return rows[0][0] if rows else 0


def planners(g) -> list[str]:
    return [r[0] for r in g.ro_query("MATCH (a:Agent)-[:PLANS]->(:Step) RETURN a.id ORDER BY a.id").result_set]


def role(g, agent: str) -> tuple[str, str | None]:
    """('owner' | 'builder' | 'independent', partner). The owner is whoever claimed overlapping functions;
    the builder merges the owner's commit and builds on top; the owner then fast-forwards to the builder."""
    ov = overlaps(g, agent)
    partner = ov[0][0] if ov else next((p for p in planners(g) if p != agent), None)
    if not ov:
        return "independent", partner
    shared = sorted({x for _, m, t, _, _ in ov for x in (m, t)})
    owners = {r[0] for r in g.ro_query("UNWIND $s AS q MATCH (f:Function {qualname: q}) "
                                       "WHERE f.claimed_by IS NOT NULL RETURN DISTINCT f.claimed_by",
                                       {"s": shared}).result_set}
    if agent in owners:
        return "owner", partner
    if owners:
        return "builder", partner
    return "unclaimed", partner


def commit_of(g, agent: str | None) -> str | None:
    if not agent:
        return None
    rows = g.ro_query("MATCH (a:Agent {id: $a}) RETURN a.commit", {"a": agent}).result_set
    return rows[0][0] if rows else None


def next_step(g, agent: str) -> tuple[str, str]:
    """(state key, instruction). The state key changes exactly when the caller's next step changes."""
    r, partner = role(g, agent)
    mine, theirs = commit_of(g, agent), commit_of(g, partner)
    if r == "independent":
        return "independent", "No overlap with your partner: finish your feature and follow the git merge sequence above."
    if r == "unclaimed":
        return "unclaimed", "You overlap with your partner but nobody has claimed the shared functions yet: claim() them now."
    if r == "owner":
        if not mine:
            return "owner-work", (f"You own the shared functions. Implement your WHOLE feature now, commit, "
                                  f"`git push team {agent}`, then mark_committed(<commit hash>).")
        if not theirs:
            return "owner-wait", (f"Done on your side. {partner} is building on top of your commit. Call "
                                  f"coordination_status(wait_seconds=110) until {partner} has pushed the combined tree.")
        return "owner-final", (f"{partner} pushed the combined tree at {theirs}. FINAL: `git fetch team && git merge "
                               f"--ff-only team/{partner}` then `git diff team/main..HEAD > patch.txt`. Do not resolve "
                               f"conflicts or merge any other way; if --ff-only fails, message {partner}.")
    if not theirs:
        return "builder-wait", (f"{partner} owns the shared functions: do NOT edit them. Implement and commit your other "
                                f"parts, then call coordination_status(wait_seconds=110) until {partner} has committed.")
    if not mine:
        return "builder-work", (f"{partner} committed at {theirs}. Now: commit your work, `git fetch team && git merge "
                                f"--no-edit team/{partner}`, add your changes to the shared functions on top, commit, "
                                f"`git push team {agent}`, then mark_committed(<commit hash>).")
    return "builder-final", ("You pushed the combined tree. FINAL: `git diff team/main..HEAD > patch.txt`. "
                             f"{partner} will fast-forward to your branch.")


def status_text(g, agent: str) -> str:
    lines = []
    plan = g.ro_query("MATCH (:Agent {id: $a})-[:PLANS]->(:Step)-[:TOUCHES]->(f:Function) RETURN f.qualname "
                      "ORDER BY f.qualname", {"a": agent}).result_set
    lines.append("Your plan: " + (", ".join(r[0] for r in plan) or "not declared yet"))
    owned = g.ro_query("MATCH (f:Function) WHERE f.claimed_by IS NOT NULL RETURN f.claimed_by, f.qualname "
                       "ORDER BY f.claimed_by, f.qualname").result_set
    if owned:
        lines.append("Ownership: " + "; ".join(f"{o} owns {q}" for o, q in owned))
    ov = overlaps(g, agent)
    if ov:
        lines.append("Overlaps with your partner's plan: " + "; ".join(
            f"your {m} vs {other}'s {t} ({hops} call(s) apart)" for other, m, t, hops, _ in ov))
    else:
        lines.append("No overlap with your partner's plan within 3 calls.")
    r, _ = role(g, agent)
    lines.append(f"Your role: {r}. Next step: {next_step(g, agent)[1]}")
    return "\n".join(lines)


async def wait_until_changed(g, agent: str, seconds: int) -> None:
    """Wait (server side, no model turns) until the caller's next step changes, or `seconds` pass.
    Async so one agent's wait never blocks the other agent's calls."""
    start_key, deadline = next_step(g, agent)[0], time.time() + min(max(seconds, 0), 110)
    while time.time() < deadline and next_step(g, agent)[0] == start_key:
        await asyncio.sleep(2)


async def wait_for_partner_plan(g, team_size: int, seconds: int = 60) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if len(planners(g)) >= team_size:
            return True
        await asyncio.sleep(1)
    return False


# ---------------------------------------------------------------- private (placebo) ----

def record_private_plan(g, agent: str, found: list[str]) -> None:
    g.query("MERGE (n:PrivatePlan {agent: $a}) SET n.functions = $f", {"a": agent, "f": found})


def private_plan(g, agent: str) -> list[str]:
    rows = g.ro_query("MATCH (n:PrivatePlan {agent: $a}) RETURN n.functions", {"a": agent}).result_set
    return rows[0][0] if rows else []


def write_note(g, agent: str, text: str) -> None:
    g.query("MATCH (a:Agent {id: $a}) OPTIONAL MATCH (a)-[w:WROTE]->() WITH a, count(w) AS seq "
            "CREATE (a)-[:WROTE {seq: seq}]->(:Note {text: $t})", {"a": agent, "t": text})


def read_notes(g, agent: str) -> list[str]:
    return [r[0] for r in g.ro_query("MATCH (:Agent {id: $a})-[w:WROTE]->(n:Note) RETURN n.text ORDER BY w.seq",
                                     {"a": agent}).result_set]


# ---------------------------------------------------------------- shared code facts ----

def impact_text(g, function: str) -> str:
    found, problems = resolve_names(g, [function])
    if not found:
        cands = problems.get(function) or []
        return f"No unique function named {function}. " + (f"Candidates: {', '.join(cands)}" if cands else "Nothing matches.")
    rows = g.ro_query("""
        MATCH (f:Function {qualname: $q})
        OPTIONAL MATCH (caller:Function)-[:CALLS*1..2]->(f)
        WITH f, collect(DISTINCT caller.qualname) AS callers
        OPTIONAL MATCH (f)-[:CALLS*1..2]->(callee:Function)
        RETURN f.file, f.line, callers, collect(DISTINCT callee.qualname)
        """, {"q": found[0]}).result_set
    file, line, callers, callees = rows[0]
    return (f"{found[0]} ({file}:{line})\n  called by (<=2 calls): {', '.join(callers[:25]) or 'nothing found'}\n"
            f"  calls (<=2 calls): {', '.join(callees[:25]) or 'nothing found'}\n"
            "Note: calls made through attributes, parameters or inheritance are not tracked.")


def build_server(db: FalkorDB, mode: str) -> MCPServer:
    server = MCPServer("code_graph")

    def who(ctx: Context):
        h = ctx.headers or {}
        g = db.select_graph(h["x-world-graph"])
        agent = h["x-agent"]
        g.query("MERGE (:Agent {id: $a})", {"a": agent})
        return g, agent, h.get("x-log")

    def logged(log, agent, tool, result, **kw):
        if log:
            with open(log, "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"t": time.time(), "agent": agent, "tool": tool, **kw}) + "\n")
        return result

    @server.tool(name="impact_of")
    def _impact(function: str, ctx: Context) -> str:
        """List the functions that call, or are called by, `function` within 2 calls."""
        g, agent, log = who(ctx)
        return logged(log, agent, "impact_of", impact_text(g, function), function=function)

    if mode == "graph":
        @server.tool(name="declare_plan")
        async def _declare(functions: list[str], ctx: Context) -> str:
            """Record the functions you intend to change (e.g. ["click.termui.edit"]); replaces your previous
            plan. Waits up to 60 s for your partner's plan, then reports overlaps, ownership and your next step."""
            g, agent, log = who(ctx)
            found, problems = resolve_names(g, functions)
            set_plan(g, agent, found)
            team = int((ctx.headers or {}).get("x-team-size", "2"))
            await wait_for_partner_plan(g, team)
            text = status_text(g, agent)
            if len(planners(g)) < team:
                text = "Your partner has not declared a plan yet; call coordination_status again before editing.\n" + text
            return logged(log, agent, "declare_plan", f"Plan recorded: {', '.join(found) or 'nothing'}."
                          + _problems_note(problems) + "\n" + text,
                          functions=functions, resolved=found, conflict="Overlaps with" in text)

        @server.tool(name="claim")
        def _claim(functions: list[str], ctx: Context) -> str:
            """Claim functions so only you change them. Atomic: the first agent to claim a function owns it.
            Returns what you now own and who owns the rest."""
            g, agent, log = who(ctx)
            found, problems = resolve_names(g, functions)
            mine, others = claim(g, agent, found)
            text = ("You own: " + (", ".join(mine) or "nothing new") + "."
                    + ("".join(f"\n{q} is owned by {o}: do not edit it; merge their branch once they commit."
                               for q, o in others.items())) + _problems_note(problems))
            return logged(log, agent, "claim", text, functions=functions, won=mine, lost=others)

        @server.tool(name="mark_committed")
        def _committed(commit: str, ctx: Context) -> str:
            """After you commit AND push (git push team <your branch>), pass the commit hash so your partner
            knows your branch is ready."""
            g, agent, log = who(ctx)
            n = mark_committed(g, agent, commit)
            text = f"Recorded your commit {commit} ({n} owned function(s)).\n" + status_text(g, agent)
            return logged(log, agent, "mark_committed", text, commit=commit, functions=n)

        @server.tool(name="coordination_status")
        async def _status(ctx: Context, wait_seconds: int = 0) -> str:
            """Your role, overlaps, who owns what, and your exact next step. With wait_seconds (max 110), waits
            server-side until your next step changes (for example, your partner commits)."""
            g, agent, log = who(ctx)
            if wait_seconds:
                await wait_until_changed(g, agent, wait_seconds)
            text = status_text(g, agent)
            return logged(log, agent, "coordination_status", text, conflict="Overlaps with" in text,
                          step=next_step(g, agent)[0], waited=wait_seconds)
    else:
        @server.tool(name="record_plan")
        def _record(functions: list[str], ctx: Context) -> str:
            """Record the functions you intend to change, in a private plan only you can see."""
            g, agent, log = who(ctx)
            found, problems = resolve_names(g, functions)
            record_private_plan(g, agent, found)
            return logged(log, agent, "record_plan", f"Private plan recorded: {', '.join(found) or 'nothing'}."
                          + _problems_note(problems), functions=functions, resolved=found)

        @server.tool(name="my_plan")
        def _plan(ctx: Context) -> str:
            """Show your private plan."""
            g, agent, log = who(ctx)
            return logged(log, agent, "my_plan", "Your private plan: " + (", ".join(private_plan(g, agent)) or "empty"))

        @server.tool(name="write_note")
        def _write(text: str, ctx: Context) -> str:
            """Save a private note. Only you can ever see your notes."""
            g, agent, log = who(ctx)
            write_note(g, agent, text)
            return logged(log, agent, "write_note", "Note saved. Only you can see it.")

        @server.tool(name="read_notes")
        def _read(ctx: Context) -> str:
            """Show your private notes, oldest first."""
            g, agent, log = who(ctx)
            notes = read_notes(g, agent)
            return logged(log, agent, "read_notes", "Your notes:\n" + "\n".join(f"- {n}" for n in notes)
                          if notes else "You have no notes yet.")
    return server


if __name__ == "__main__":
    import argparse

    from mcp.server.transport_security import TransportSecuritySettings

    import os

    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=list(PORTS), required=True)
    mode = ap.parse_args().mode
    port = PORTS[mode]
    # Record which code this server runs, so the launcher never reuses a server started from older code.
    stamp = Path(__file__).with_name(f".server_{mode}.json")
    stamp.write_text(json.dumps({"pid": os.getpid(), "code": code_fingerprint()}), encoding="utf-8")
    # Containers reach us as host.docker.internal; keep DNS-rebinding protection on, allow only these hosts.
    hosts = [f"{h}:{port}" for h in ("127.0.0.1", "localhost", "host.docker.internal")]
    security = TransportSecuritySettings(enable_dns_rebinding_protection=True, allowed_hosts=hosts, allowed_origins=[])
    build_server(FalkorDB(host="localhost", port=6379), mode).run(
        "streamable-http", host="127.0.0.1", port=port, transport_security=security)
