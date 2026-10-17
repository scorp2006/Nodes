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


def serve(graph: str, agent: str, log: str | None = None, mode: str = "board") -> None:
    import json
    import time

    from mcp.server.mcpserver import MCPServer

    g = connect(graph)
    server = MCPServer("board")

    def logged(tool: str, result: str, **kw) -> str:
        if log:
            with open(log, "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"t": time.time(), "agent": agent, "tool": tool, **kw}) + "\n")
        return result

    if mode == "notepad":
        @server.tool(name="write_note")
        def _write(text: str) -> str:
            """Save a private note. Only you can ever see your notes."""
            return logged("write_note", write_note(g, agent, text))

        @server.tool(name="read_notes")
        def _read() -> str:
            """Show all of your private notes, oldest first."""
            return logged("read_notes", read_notes(g, agent))

        @server.tool(name="review_my_facts")
        def _mine() -> str:
            """List your own numbered facts from your briefing."""
            return logged("review_my_facts", my_facts(g, agent))
    elif mode == "claims":
        @server.tool(name="post_claim")
        def _claim(text: str) -> str:
            """Post a claim to the shared board, in your own words. Claims are not checked."""
            return logged("post_claim", post_claim(g, agent, text))

        @server.tool(name="shared_claims")
        def _claims() -> str:
            """List every claim posted to the board so far, with who posted it."""
            return logged("shared_claims", shared_claims(g))

        @server.tool(name="who_has_not_posted")
        def _who_claims() -> str:
            """Name the participants who hold facts nobody else has and have not posted anything."""
            return logged("who_has_not_posted", who_has_not_posted(g))
    else:
        @server.tool(name="share_fact")
        def _share(fact_number: int) -> str:
            """Post one of your own numbered facts to the shared board. It is marked source-checked."""
            return logged("share_fact", share_fact(g, agent, fact_number), fact_number=fact_number)

        @server.tool(name="shared_facts")
        def _shared() -> str:
            """List every fact posted to the board so far, with who posted it."""
            return logged("shared_facts", shared_facts(g))

        @server.tool(name="who_has_not_shared")
        def _who() -> str:
            """Name the participants who hold facts nobody else has and have not posted them."""
            return logged("who_has_not_shared", who_has_not_shared(g))

    server.run()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--graph", required=True)
    ap.add_argument("--agent", required=True)
    ap.add_argument("--mode", choices=["board", "claims", "notepad"], default="board")
    ap.add_argument("--log", help="append one JSON line per tool call")
    args = ap.parse_args()
    serve(args.graph, args.agent, args.log, args.mode)
