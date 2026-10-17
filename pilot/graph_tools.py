"""Shared evidence board for HiddenBench agents, stored in FalkorDB.

One graph per scenario run:
  (:Agent {name})-[:KNOWS {num}]->(:Fact {id, text})   what each agent was briefed with
  (:Agent)-[:SHARED {seq}]->(:Fact)                    what each agent posted to the board

Run as an MCP server (one process per agent call):
  python graph_tools.py --graph <name> --agent "Person 1"

The board never reveals a fact nobody has posted. It only verifies facts that an
agent chose to share, and names who still holds unique facts.
"""
from __future__ import annotations

import argparse

from falkordb import FalkorDB


def connect(graph: str):
    return FalkorDB(host="localhost", port=6379).select_graph(graph)


def seed(graph: str, task, fact_assignments: list[dict]) -> None:
    """Write who-knows-what for one run. Idempotent."""
    ids = {text: f"s{i}" for i, text in enumerate(task.shared_information)}
    ids |= {text: f"h{i}" for i, text in enumerate(task.hidden_information)}
    rows = [
        {"agent": a["agent"], "num": n, "id": ids[text], "text": text}
        for a in fact_assignments
        for n, text in enumerate(a["visible_facts"], start=1)
    ]
    connect(graph).query(
        """
        UNWIND $rows AS row
        MERGE (a:Agent {name: row.agent})
        MERGE (f:Fact {id: row.id}) ON CREATE SET f.text = row.text
        MERGE (a)-[k:KNOWS]->(f) SET k.num = row.num
        """,
        {"rows": rows},
    )


def share_fact(g, agent: str, fact_number: int) -> str:
    res = g.query(
        """
        MATCH (a:Agent {name: $agent})-[k:KNOWS {num: $num}]->(f:Fact)
        OPTIONAL MATCH ()-[prev:SHARED]->()
        WITH a, f, count(prev) AS seq
        MERGE (a)-[s:SHARED]->(f) ON CREATE SET s.seq = seq
        RETURN f.text
        """,
        {"agent": agent, "num": fact_number},
    ).result_set
    if not res:
        return f"You have no Fact {fact_number}."
    return f"Posted to the board as verified: {res[0][0]}"


def shared_facts(g) -> str:
    rows = g.ro_query(
        """
        MATCH (a:Agent)-[s:SHARED]->(f:Fact)
        RETURN a.name, f.text ORDER BY s.seq
        """
    ).result_set
    if not rows:
        return "The board is empty. Nobody has posted a fact yet."
    lines = [f"- {who} (verified: this fact is really in their briefing): {text}" for who, text in rows]
    return "Verified facts on the board:\n" + "\n".join(lines)


def who_has_not_shared(g) -> str:
    rows = g.ro_query(
        """
        MATCH (holder:Agent)-[:KNOWS]->(f:Fact)
        WITH f, collect(holder) AS holders
        WHERE size(holders) = 1
        WITH f, holders[0] AS holder
        OPTIONAL MATCH (holder)-[s:SHARED]->(f)
        WITH holder, count(s) AS posted
        WHERE posted = 0
        RETURN DISTINCT holder.name ORDER BY holder.name
        """
    ).result_set
    if not rows:
        return "Everyone has posted the facts that only they hold."
    names = ", ".join(r[0] for r in rows)
    return f"These participants hold facts nobody else has and have not posted them yet: {names}"


def serve(graph: str, agent: str) -> None:
    from mcp.server.mcpserver import MCPServer

    g = connect(graph)
    server = MCPServer("board")

    @server.tool(name="share_fact")
    def _share(fact_number: int) -> str:
        """Post one of your own numbered facts to the shared board. It is marked verified."""
        return share_fact(g, agent, fact_number)

    @server.tool(name="shared_facts")
    def _shared() -> str:
        """List every fact posted to the board so far, with who posted it."""
        return shared_facts(g)

    @server.tool(name="who_has_not_shared")
    def _who() -> str:
        """Name the participants who hold facts nobody else has and have not posted them."""
        return who_has_not_shared(g)

    server.run()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--graph", required=True)
    ap.add_argument("--agent", required=True)
    args = ap.parse_args()
    serve(args.graph, args.agent)
