"""Read-only graph reasoning tools for agents: multi-hop impact, shortest dependency path, central code, module
clusters, and raw read-only Cypher. Every query runs as GRAPH.RO_QUERY on the caller's workspace graph with a timeout
and a row cap, and every result carries the Cypher it ran, so the dashboard can show the traversal.

Edges walked: (fn)-[:DEPENDS_ON {kind:'calls'}]->(fn) for calls, (file)-[:IMPORTS]->(file) for imports.
"""
from __future__ import annotations

import re

from . import ops

TIMEOUT_MS = 2000
ROW_CAP = 200
WRITE = re.compile(r"\b(CREATE|MERGE|SET|DELETE|DETACH|REMOVE|DROP|LOAD\s+CSV)\b|\bCALL\s+db\.(idx|create)", re.I)


def _q(store, name: str, cypher: str, params: dict | None = None) -> list:
    return store.q(name, cypher, params or {}, ro=True, timeout=TIMEOUT_MS)[:ROW_CAP]


def _one(store, target: str) -> str | None:
    uids, _ = ops.resolve_targets(store, [target])
    return uids[0] if uids else None


def _done(store, sess, tool: str, data: dict, cypher: list[str], units: list[str], summary: str, nxt: str) -> dict:
    store.event("query", sess["name"], tool=tool, summary=summary, units=units[:40])
    return ops.envelope(data | {"cypher": cypher}, nxt)


IMPACT = """
MATCH (u:Unit {{uid: $u}})
OPTIONAL MATCH p = (c:Unit)-[:DEPENDS_ON*1..{h}]->(u) WHERE c <> u
WITH u, c, p ORDER BY length(p)
WITH u, c, collect(p)[0] AS p WHERE c IS NOT NULL
RETURN 'caller' AS dir, c.uid AS unit, length(p) AS hops, [n IN nodes(p) | n.uid] AS via, c.lease_owner AS owner
UNION
MATCH (u:Unit {{uid: $u}})
OPTIONAL MATCH p = (u)-[:DEPENDS_ON*1..{h}]->(c:Unit) WHERE c <> u
WITH u, c, p ORDER BY length(p)
WITH u, c, collect(p)[0] AS p WHERE c IS NOT NULL
RETURN 'callee' AS dir, c.uid AS unit, length(p) AS hops, [n IN nodes(p) | n.uid] AS via, c.lease_owner AS owner
UNION
MATCH (u:Unit {{uid: $u}})
OPTIONAL MATCH (f:Unit {{kind: 'file', path: u.path}})
OPTIONAL MATCH p = (c:Unit {{kind: 'file'}})-[:IMPORTS*1..{h}]->(f) WHERE c <> f
WITH c, p ORDER BY length(p)
WITH c, collect(p)[0] AS p WHERE c IS NOT NULL
RETURN 'importer' AS dir, c.uid AS unit, length(p) AS hops, [n IN nodes(p) | n.uid] AS via, c.lease_owner AS owner"""


def impact_of(store, sess: dict, unit: str, max_hops: int = 3) -> dict:
    uid = _one(store, unit)
    if not uid:
        return ops.error("NOT_FOUND", f"no unit matches {unit!r}", "Use a uid, a qualname, a path or 'path:10-20'.")
    h = max(1, min(int(max_hops), 4))
    cypher = IMPACT.format(h=h)
    rows = _q(store, "tool.impact_of", cypher, {"u": uid})
    owners = dict(store.q("tool.impact_owners", "UNWIND $s AS id MATCH (s:Session {uid: id}) RETURN s.uid, s.name",
                          {"s": list({r[4] for r in rows if r[4]})}, ro=True)) if rows else {}
    hits = sorted(({"direction": d, "unit": c, "hops": n, "path": p, "held_by": owners.get(o)} for d, c, n, p, o in rows),
                  key=lambda x: (x["hops"], x["direction"]))
    held = [x for x in hits if x["held_by"] and x["held_by"] != sess["name"]]
    summary = f"impact of {uid}: {len(hits)} unit(s) within {h} hops" + (f", {len(held)} held by teammates" if held else "")
    return _done(store, sess, "impact_of", {"unit": uid, "max_hops": h, "impact": hits, "held_by_teammates": held},
                 [cypher], [uid] + [x["unit"] for x in hits[:30]], summary,  # everything it reached: the live view draws rays
                 "Units held by teammates are where your change could reach their work: coordinate before editing.")


def path_between(store, sess: dict, a: str, b: str) -> dict:
    ua, ub = _one(store, a), _one(store, b)
    if not ua or not ub:
        return ops.error("NOT_FOUND", f"no unit matches {a if not ua else b!r}", "Use a uid, a qualname or a path.")
    # undirected: FalkorDB's shortestPath() is directed-only, algo.SPpaths takes relDirection 'both'
    cypher = ("MATCH (a:Unit {uid: $a}), (b:Unit {uid: $b}) "
              "CALL algo.SPpaths({sourceNode: a, targetNode: b, relTypes: ['DEPENDS_ON', 'IMPORTS', 'CONTAINS'], "
              "relDirection: 'both', maxLen: 8, pathCount: 1}) YIELD path "
              "RETURN [n IN nodes(path) | n.uid], [r IN relationships(path) | type(r)]")
    rows = _q(store, "tool.path_between", cypher, {"a": ua, "b": ub})
    nodes, rels = rows[0] if rows and rows[0][0] else ([], [])
    summary = f"path {ua} to {ub}: " + (f"{len(rels)} hop(s)" if nodes else "not connected within 8 hops")
    return _done(store, sess, "path_between", {"from": ua, "to": ub, "path": nodes, "relationships": rels,
                                               "connected": bool(nodes)}, [cypher], nodes or [ua, ub], summary,
                 "A short path means a change at one end can reach the other.")


def hotspots(store, sess: dict, limit: int = 15) -> dict:
    cypher = ("CALL algo.pageRank('Unit', 'DEPENDS_ON') YIELD node, score WHERE node.kind <> 'file' "
              "OPTIONAL MATCH (c:Unit)-[:DEPENDS_ON]->(node) WITH node, score, count(c) AS callers "
              "RETURN node.uid, node.symbol, node.path, score, callers ORDER BY score DESC LIMIT $k")
    rows = _q(store, "tool.hotspots", cypher, {"k": max(1, min(int(limit), 50))})
    data = [{"unit": u, "symbol": s, "path": p, "pagerank": round(sc, 4), "callers": c} for u, s, p, sc, c in rows]
    return _done(store, sess, "hotspots", {"hotspots": data}, [cypher], [d["unit"] for d in data[:10]],
                 f"hotspots: top {len(data)} by PageRank on the call graph",
                 "Central units affect the most code: change them with care and tell teammates.")


def communities(store, sess: dict, limit: int = 12) -> dict:
    cypher = ("CALL algo.labelPropagation({nodeLabels: ['Unit'], relationshipTypes: ['DEPENDS_ON', 'IMPORTS', 'CONTAINS']}) "
              "YIELD node, communityId WITH node, communityId WHERE node.kind IN ['function', 'class'] "
              "WITH communityId, collect(node) AS ns WHERE size(ns) > 1 "
              "RETURN communityId, size(ns), reduce(fs = [], n IN ns | CASE WHEN n.path IN fs THEN fs ELSE fs + n.path END)[..8], "
              "[n IN ns | n.uid][..6] ORDER BY size(ns) DESC LIMIT $k")
    rows = _q(store, "tool.communities", cypher, {"k": max(1, min(int(limit), 40))})
    data = [{"community": c, "size": n, "files": fs, "sample_units": us} for c, n, fs, us in rows]
    return _done(store, sess, "communities", {"communities": data}, [cypher],
                 [u for d in data[:3] for u in d["sample_units"][:3]],
                 f"communities: {len(data)} clusters (label propagation over calls, imports, containment)",
                 "Clusters that don't touch each other are good units to split work across parallel agents.")


def _plain(v):
    if hasattr(v, "labels") and hasattr(v, "properties"):  # node
        return {"id": v.id, "labels": list(v.labels), "properties": v.properties}
    if hasattr(v, "relation"):  # edge
        return {"id": v.id, "type": v.relation, "src": v.src_node, "dst": v.dest_node, "properties": v.properties}
    if hasattr(v, "nodes") and callable(v.nodes):  # path
        return {"nodes": [_plain(n) for n in v.nodes()], "edges": [_plain(e) for e in v.edges()]}
    if isinstance(v, list):
        return [_plain(x) for x in v]
    if isinstance(v, dict):
        return {k: _plain(x) for k, x in v.items()}
    return v


def query_graph(store, sess: dict, cypher: str) -> dict:
    if WRITE.search(cypher):
        return ops.error("READ_ONLY", "query_graph is read-only: CREATE, MERGE, SET, DELETE, REMOVE and index calls "
                         "are refused", "Use before_acting, post_fact or coordinate to change the graph.")
    try:
        rows = _q(store, "tool.query_graph", cypher)
    except Exception as e:  # noqa: BLE001  syntax errors, timeouts, write attempts the regex missed
        return ops.error("QUERY_FAILED", str(e)[:300], f"Fix the Cypher; it runs read-only with a {TIMEOUT_MS} ms timeout.")
    out = [_plain(r) for r in rows]
    units = [x for r in out for x in (r if isinstance(r, list) else [r]) if isinstance(x, str) and ":" in x and "#" in x]
    return _done(store, sess, "query_graph", {"rows": out, "row_count": len(out), "capped_at": ROW_CAP}, [cypher],
                 units[:20], f"query_graph: {len(out)} row(s)", "Read-only; rows are capped.")
