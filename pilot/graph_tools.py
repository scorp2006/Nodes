"""Shared evidence board for HiddenBench agents, stored in FalkorDB.

One graph per scenario run:
  (:Agent {name})-[:KNOWS {num}]->(:Fact {id, text})   what each agent was briefed with
  (:Agent)-[:SHARED {seq}]->(:Fact)                    board mode: facts posted by number
  (:Agent)-[:CLAIMED {seq}]->(:Claim {text})           claims mode: free-text posts, unchecked
  (:Agent)-[:WROTE {seq}]->(:Note {text})              notepad mode: private notes

Run as an MCP server (one process per agent call):
  python graph_tools.py --graph <name> --agent "Person 1" [--mode board|claims|notepad]

  board   : posts are an agent's own briefing facts, picked by number, so every post
            is "source-checked" (it really is in that agent's briefing). This says
            where a fact came from, never whether it is true, and never uses the answer.
  claims  : same board, but agents post in their own words and nothing is checked.
  notepad : placebo. Same mechanics, but private to the agent and adds no information.

In every mode, nothing an agent has not chosen to post is ever shown to anyone else.
"""
from __future__ import annotations

import argparse

from falkordb import FalkorDB
from mcp.server.mcpserver import Context, MCPServer
from redis.exceptions import ResponseError


def connect(graph: str):
    return FalkorDB(host="localhost", port=6379).select_graph(graph)


def seed(graph: str, task, fact_assignments: list[dict]) -> None:
    """Start a fresh board for one run: wipe the graph, then write who-knows-what."""
    g = connect(graph)
    try:
        g.delete()
    except ResponseError:  # graph doesn't exist yet
        pass
    ids = {text: f"s{i}" for i, text in enumerate(task.shared_information)}
    ids |= {text: f"h{i}" for i, text in enumerate(task.hidden_information)}
    rows = [
        {"agent": a["agent"], "num": n, "id": ids[text], "text": text}
        for a in fact_assignments
        for n, text in enumerate(a["visible_facts"], start=1)
    ]
    g.query(
        """
        UNWIND $rows AS row
        MERGE (a:Agent {name: row.agent})
        MERGE (f:Fact {id: row.id}) ON CREATE SET f.text = row.text
        MERGE (a)-[k:KNOWS]->(f) SET k.num = row.num
        """,
        {"rows": rows},
    )


# --- board mode: source-checked posts -------------------------------------------------

def share_fact(g, agent: str, fact_number: int) -> str:
    """Post the agent's own fact by number. Replies only yes/no, never any fact wording."""
    res = g.query(
        """
        MATCH (a:Agent {name: $agent})-[k:KNOWS {num: $num}]->(f:Fact)
        OPTIONAL MATCH ()-[prev:SHARED]->()
        WITH a, f, count(prev) AS seq
        MERGE (a)-[s:SHARED]->(f) ON CREATE SET s.seq = seq
        RETURN f.id
        """,
        {"agent": agent, "num": fact_number},
    ).result_set
    if not res:
        return f"Source-checked: no. You have no Fact {fact_number}, so nothing was posted."
    return f"Source-checked: yes. Your Fact {fact_number} is now on the board."


def shared_facts(g) -> str:
    rows = g.ro_query(
        """
        MATCH (a:Agent)-[s:SHARED]->(f:Fact)
        RETURN a.name, f.text ORDER BY s.seq
        """
    ).result_set
    if not rows:
        return "The board is empty. Nobody has posted a fact yet."
    lines = [f"- {who} (source-checked: this is in their briefing): {text}" for who, text in rows]
    return "Facts on the board:\n" + "\n".join(lines)


def who_has_not_shared(g) -> str:
    return _unique_holders_without(g, "SHARED", match_fact=True)


# --- claims mode: unchecked free-text posts ----------------------------------------------

def post_claim(g, agent: str, text: str) -> str:
    g.query(
        """
        MATCH (a:Agent {name: $agent})
        OPTIONAL MATCH ()-[prev:CLAIMED]->()
        WITH a, count(prev) AS seq
        CREATE (a)-[:CLAIMED {seq: seq}]->(:Claim {text: $text})
        """,
        {"agent": agent, "text": text},
    )
    return "Your claim is now on the board."


def shared_claims(g) -> str:
    rows = g.ro_query(
        "MATCH (a:Agent)-[c:CLAIMED]->(cl:Claim) RETURN a.name, cl.text ORDER BY c.seq"
    ).result_set
    if not rows:
        return "The board is empty. Nobody has posted a claim yet."
    lines = [f"- {who} (not checked): {text}" for who, text in rows]
    return "Claims on the board:\n" + "\n".join(lines)


def who_has_not_posted(g) -> str:
    return _unique_holders_without(g, "CLAIMED", match_fact=False)


def _unique_holders_without(g, rel: str, match_fact: bool) -> str:
    """Name agents who hold a fact nobody else has and haven't posted it (board) or anything (claims)."""
    target = "(f)" if match_fact else "()"
    rows = g.ro_query(
        f"""
        MATCH (holder:Agent)-[:KNOWS]->(f:Fact)
        WITH f, collect(holder) AS holders
        WHERE size(holders) = 1
        WITH f, holders[0] AS holder
        OPTIONAL MATCH (holder)-[s:{rel}]->{target}
        WITH holder, count(s) AS posted
        WHERE posted = 0
        RETURN DISTINCT holder.name ORDER BY holder.name
        """
    ).result_set
    if not rows:
        return "Everyone who holds facts nobody else has has posted."
    names = ", ".join(r[0] for r in rows)
    return f"These participants hold facts nobody else has and have not posted yet: {names}"


# --- notepad mode: private placebo -------------------------------------------------------

def write_note(g, agent: str, text: str) -> str:
    g.query(
        """
        MATCH (a:Agent {name: $agent})
        OPTIONAL MATCH (a)-[prev:WROTE]->()
        WITH a, count(prev) AS seq
        CREATE (a)-[:WROTE {seq: seq}]->(:Note {text: $text})
        """,
        {"agent": agent, "text": text},
    )
    return "Note saved. Only you can see it."


def read_notes(g, agent: str) -> str:
    rows = g.ro_query(
        "MATCH (:Agent {name: $agent})-[w:WROTE]->(n:Note) RETURN n.text ORDER BY w.seq",
        {"agent": agent},
    ).result_set
    return "Your notes:\n" + "\n".join(f"- {r[0]}" for r in rows) if rows else "You have no notes yet."


def my_facts(g, agent: str) -> str:
    rows = g.ro_query(
        "MATCH (:Agent {name: $agent})-[k:KNOWS]->(f:Fact) RETURN k.num, f.text ORDER BY k.num",
        {"agent": agent},
    ).result_set
    return "Your facts:\n" + "\n".join(f"Fact {n}: {t}" for n, t in rows)


DIGEST = {
    "board": lambda g, agent: shared_facts(g) + "\n" + who_has_not_shared(g),
    "claims": lambda g, agent: shared_claims(g) + "\n" + who_has_not_posted(g),
    "notepad": lambda g, agent: read_notes(g, agent),
}


def digest(graph: str, agent: str, mode: str) -> str:
    """What gets pushed into each turn: exactly what the mode's read tools would return."""
    return DIGEST[mode](connect(graph), agent)


def build_server(mode: str, resolve):
    """One MCP server exposing only `mode`'s three tools.

    resolve(ctx) -> (graph handle, agent name, log path or None) for the calling agent.
    """
    import json
    import time



    server = MCPServer("board")

    def logged(agent, log, tool: str, result: str, **kw) -> str:
        if log:
            with open(log, "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"t": time.time(), "agent": agent, "tool": tool, **kw}) + "\n")
        return result

    if mode == "notepad":
        @server.tool(name="write_note")
        def _write(text: str, ctx: Context) -> str:
            """Save a private note. Only you can ever see your notes."""
            g, agent, log = resolve(ctx)
            return logged(agent, log, "write_note", write_note(g, agent, text))

        @server.tool(name="read_notes")
        def _read(ctx: Context) -> str:
            """Show all of your private notes, oldest first."""
            g, agent, log = resolve(ctx)
            return logged(agent, log, "read_notes", read_notes(g, agent))

        @server.tool(name="review_my_facts")
        def _mine(ctx: Context) -> str:
            """List your own numbered facts from your briefing."""
            g, agent, log = resolve(ctx)
            return logged(agent, log, "review_my_facts", my_facts(g, agent))
    elif mode == "claims":
        @server.tool(name="post_claim")
        def _claim(text: str, ctx: Context) -> str:
            """Post a claim to the shared board, in your own words. Claims are not checked."""
            g, agent, log = resolve(ctx)
            return logged(agent, log, "post_claim", post_claim(g, agent, text))

        @server.tool(name="shared_claims")
        def _claims(ctx: Context) -> str:
            """List every claim posted to the board so far, with who posted it."""
            g, agent, log = resolve(ctx)
            return logged(agent, log, "shared_claims", shared_claims(g))

        @server.tool(name="who_has_not_posted")
        def _who_claims(ctx: Context) -> str:
            """Name the participants who hold facts nobody else has and have not posted anything."""
            g, agent, log = resolve(ctx)
            return logged(agent, log, "who_has_not_posted", who_has_not_posted(g))
    else:
        @server.tool(name="share_fact")
        def _share(fact_number: int, ctx: Context) -> str:
            """Post one of your own numbered facts to the shared board. It is marked source-checked."""
            g, agent, log = resolve(ctx)
            return logged(agent, log, "share_fact", share_fact(g, agent, fact_number), fact_number=fact_number)

        @server.tool(name="shared_facts")
        def _shared(ctx: Context) -> str:
            """List every fact posted to the board so far, with who posted it."""
            g, agent, log = resolve(ctx)
            return logged(agent, log, "shared_facts", shared_facts(g))

        @server.tool(name="who_has_not_shared")
        def _who(ctx: Context) -> str:
            """Name the participants who hold facts nobody else has and have not posted them."""
            g, agent, log = resolve(ctx)
            return logged(agent, log, "who_has_not_shared", who_has_not_shared(g))
    return server


def serve(graph: str, agent: str, log: str | None = None, mode: str = "board") -> None:
    """stdio: one process per agent call, identity fixed by the command line (dev/test1 setup)."""
    g = connect(graph)
    build_server(mode, lambda ctx: (g, agent, log)).run()


HTTP_PORTS = {"board": 8771, "claims": 8772, "notepad": 8773}


def serve_http(mode: str) -> None:
    """One long-lived HTTP server per mode for a whole batch; one DB connection.

    Each agent's MCP config sends X-Board-Graph / X-Board-Agent / X-Board-Log headers.
    The harness writes those configs; agents never see or control them.
    """
    db = FalkorDB(host="localhost", port=6379)

    def resolve(ctx):
        h = ctx.headers or {}
        return db.select_graph(h["x-board-graph"]), h["x-board-agent"], h.get("x-board-log")

    build_server(mode, resolve).run("streamable-http", host="127.0.0.1", port=HTTP_PORTS[mode])


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["board", "claims", "notepad"], default="board")
    ap.add_argument("--http", action="store_true", help="long-lived HTTP server for this mode")
    ap.add_argument("--graph")
    ap.add_argument("--agent")
    ap.add_argument("--log", help="append one JSON line per tool call")
    args = ap.parse_args()
    if args.http:
        serve_http(args.mode)
    else:
        serve(args.graph, args.agent, args.log, args.mode)
