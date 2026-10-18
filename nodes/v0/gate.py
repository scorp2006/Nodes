"""The action gate: every agent action passes through the graph before it happens.

  before_acting(agent, functions, evidence, description) ->
    1. provenance  each piece of cited evidence ({text, source}) is checked against its source file
    2. claim       all-or-nothing atomic claim of the functions the action changes; a finished owner's
                   functions are handed over in the same query
    3. impact      teammates' claimed functions within 3 calls (informational only: in our pre-registered test,
                   multi-hop paths did not predict merge failures, so they never drive the decision)
    4. decision    escalate | defer | hand_off | proceed, with the graph path that explains it

  escalate  cited evidence isn't in its source, or waiting would close a wait-for cycle (deadlock)
  defer     a needed function is owned by a teammate who is still working; recorded as (me)-[:WAITS_FOR]->(owner)
  hand_off  the owner has finished (mark_done): ownership moves to the caller, who builds on the owner's commit
  proceed   evidence checks out and every function is now the caller's

Every decision is stored as (:Agent)-[:REQUESTED]->(:Action)-[:TOUCHES]->(:Function) and
(:Action)-[:CITES]->(:Fact), so any decision can be traced back to its sources.
"""
from __future__ import annotations

import uuid
from pathlib import Path

from . import core

CLAIM_ALL_OR_NOTHING = """
UNWIND $found AS q
MATCH (f:Function {qualname: q})
OPTIONAL MATCH (o:Agent) WHERE o.id = f.claimed_by
WITH f, (f.claimed_by IS NULL OR f.claimed_by = $me OR o.commit IS NOT NULL) AS available
WITH collect(f) AS fs, collect(available) AS av
WITH fs, all(x IN av WHERE x) AS ok
UNWIND fs AS f
WITH f, ok, f.claimed_by AS prev
SET f.claimed_by = CASE WHEN ok THEN $me ELSE f.claimed_by END,
    f.version = CASE WHEN ok AND (prev IS NULL OR prev <> $me) THEN f.version + 1 ELSE f.version END
RETURN f.qualname, prev, ok
"""

IMPACT = """
UNWIND $found AS q
MATCH (f:Function {qualname: q})
MATCH p = (f)-[:CALLS*1..3]-(g:Function)
WHERE g.claimed_by IS NOT NULL AND g.claimed_by <> $me
RETURN g.claimed_by, f.qualname, g.qualname, length(p), [n IN nodes(p) | n.qualname]
ORDER BY length(p) LIMIT 20
"""


def _impact(g, agent, found):
    seen, out = set(), []
    for owner, mine, theirs, hops, via in g.ro_query(IMPACT, {"found": found, "me": agent}).result_set:
        if (owner, mine, theirs) not in seen:
            seen.add((owner, mine, theirs))
            out.append({"owner": owner, "mine": mine, "theirs": theirs, "hops": hops, "via": via})
    return out[:5]


def _cycle(g, owner, agent):
    """A path owner -WAITS_FOR*-> agent means agent waiting on owner would close a cycle."""
    rows = g.ro_query("MATCH p = (:Agent {id: $o})-[:WAITS_FOR*1..10]->(:Agent {id: $a}) "
                      "RETURN [n IN nodes(p) | n.id] LIMIT 1", {"o": owner, "a": agent}).result_set
    return rows[0][0] if rows else None


def _record(g, agent, found, facts, out):
    aid = f"{agent}-{uuid.uuid4().hex[:8]}"
    g.query("""
        MATCH (a:Agent {id: $a})
        CREATE (a)-[:REQUESTED]->(x:Action {id: $id, description: $d, decision: $dec, explanation: $e, t: timestamp()})
        WITH x
        UNWIND $found AS q
        MATCH (f:Function {qualname: q})
        MERGE (x)-[:TOUCHES]->(f)
        """, {"a": agent, "id": aid, "d": out["description"][:300], "dec": out["decision"],
              "e": " ".join(out["explanation"]), "found": found})
    if facts:
        g.query("MATCH (x:Action {id: $id}) UNWIND $f AS fid MATCH (f:Fact {id: fid}) MERGE (x)-[:CITES]->(f)",
                {"id": aid, "f": facts})
    out["action_id"] = aid
    return out


def before_acting(g, workspace: Path, agent: str, functions: list[str], evidence: list[dict] | None = None,
                  description: str = "") -> dict:
    core.touch(g, agent)
    found, problems = core.resolve(g, functions)
    out = {"agent": agent, "description": description, "functions": found, "unresolved": problems}

    # 1. provenance: every cited quote must be in its source file (posted to the board either way)
    items, facts = [], []
    for ev in evidence or []:
        text, source = str(ev.get("text", "")), str(ev.get("source", ""))
        fid = uuid.uuid4().hex
        ok = core.share_fact(g, workspace, agent, text, source, fact_id=fid)
        facts.append(fid)
        items.append({"text": text, "source": source, "checked": ok})
    status = "none cited" if not items else "verified" if all(i["checked"] for i in items) else "unverified"
    out["provenance"] = {"status": status, "items": items}
    out["impact"] = _impact(g, agent, found)

    if status == "unverified":
        bad = next(i for i in items if not i["checked"])
        out |= {"decision": "escalate", "claim": {"won": [], "handed_from": {}, "blocked_by": {}},
                "explanation": [agent, "-CITES->", f'"{bad["text"][:60]}"', "-NOT_IN->", bad["source"]],
                "reason": "cited evidence is not in its source; a human should check it before anyone acts on it"}
        return _record(g, agent, found, facts, out)

    # 2. claim: all-or-nothing, so a deferred action holds nothing while it waits
    rows = g.query(CLAIM_ALL_OR_NOTHING, {"found": found, "me": agent}).result_set
    ok = all(r[2] for r in rows) if rows else True
    owners = {q: prev for q, prev, _ in rows if prev and prev != agent}
    if ok:
        commits = {o: c for o, c in g.ro_query("UNWIND $o AS o MATCH (a:Agent {id: o}) RETURN a.id, a.commit",
                                               {"o": list(set(owners.values()))}).result_set}
        g.query("MATCH (:Agent {id: $a})-[w:WAITS_FOR]->() DELETE w", {"a": agent})
        out["claim"] = {"won": found, "handed_from": owners, "blocked_by": {}}
        if owners:
            q, o = next(iter(owners.items()))
            out |= {"decision": "hand_off",
                    "explanation": [o, "-DONE_AT->", str(commits.get(o)), "-HANDS->", q, "-TO->", agent],
                    "reason": f"{o} finished; you now own {', '.join(owners)}. Merge {o}'s commit "
                              f"{commits.get(o)} first, then build on top"}
        else:
            out |= {"decision": "proceed", "explanation": [agent, "-OWNS->", ", ".join(found) or "(nothing to claim)"],
                    "reason": "evidence checks out and every function is yours"}
        return _record(g, agent, found, facts, out)

    blocked = {q: prev for q, prev, _ in rows if prev and prev != agent}
    working = {q: o for q, o in blocked.items()
               if g.ro_query("MATCH (a:Agent {id: $o}) RETURN a.commit", {"o": o}).result_set[0][0] is None}
    out["claim"] = {"won": [], "handed_from": {}, "blocked_by": working}
    q, owner = next(iter(working.items()))
    cycle = _cycle(g, owner, agent)
    if cycle:
        path = [agent, "-WAITS_FOR->"] + [x for n in cycle for x in (n, "-WAITS_FOR->")][:-1]
        out |= {"decision": "escalate", "explanation": path,
                "reason": f"deadlock: {' waits for '.join([agent] + cycle)}; a human or lead must break the cycle"}
        return _record(g, agent, found, facts, out)
    g.query("MATCH (a:Agent {id: $a}), (o:Agent {id: $o}) MERGE (a)-[w:WAITS_FOR]->(o) SET w.function = $q",
            {"a": agent, "o": owner, "q": q})
    out |= {"decision": "defer", "explanation": [agent, "-NEEDS->", q, "-OWNED_BY->", owner, "(still working)"],
            "reason": f"{owner} owns {q} and hasn't finished; wait for their mark_done, then ask again"}
    return _record(g, agent, found, facts, out)


def recent(g, limit: int = 20) -> list[dict]:
    rows = g.ro_query("MATCH (a:Agent)-[:REQUESTED]->(x:Action) OPTIONAL MATCH (x)-[:CITES]->(f:Fact) "
                      "RETURN a.id, x.description, x.decision, x.explanation, x.t, collect(f.source) "
                      "ORDER BY x.t DESC LIMIT $n", {"n": limit}).result_set
    return [{"agent": a, "description": d, "decision": dec, "explanation": e, "t": t, "cites": c}
            for a, d, dec, e, t, c in rows]
