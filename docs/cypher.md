# How agents query the graph

Every MCP tool is a short series of Cypher queries against the repo's graph. The queries below are copied from
the code; each section links to it. All reads run as `GRAPH.RO_QUERY` with a timeout.

## The gate: `before_acting` ([nodes/ops.py](../nodes/ops.py#L236))
Called by the hook before every Edit, Write and file-writing Bash command. Four steps, in order:

1. **Facts the action relies on must be source-checked** (else `escalate`):
   ```cypher
   UNWIND $f AS id OPTIONAL MATCH (x:Fact {uid: id}) RETURN id, x.status
   ```
2. **Atomic lease claim, all-or-nothing** ([`LEASE`](../nodes/ops.py#L166)). FalkorDB runs writes one at a time,
   so two agents can never both get the same unit. A live lease on any overlapping unit in the same file (a class
   around a method, a region around a function) blocks it too. Abridged:
   ```cypher
   UNWIND $uids AS id MATCH (u:Unit {uid: id})
   OPTIONAL MATCH (o:Session {uid: u.lease_owner})
   OPTIONAL MATCH (v:Unit {path: u.path})              // overlapping code held by someone else
   WHERE v <> u AND v.lease_owner <> $me AND v.lease_expires >= $now
     AND (u.start IS NULL OR v.start IS NULL OR (v.start <= u.end AND u.start <= v.end))
   ...
   WITH us, ns, all(x IN av WHERE x) AS ok             // all targets free, or none are taken
   SET u.lease_epoch = CASE WHEN ok AND (prev IS NULL OR prev <> $me) THEN coalesce(u.lease_epoch, 0) + 1 ELSE u.lease_epoch END,
       u.lease_owner = CASE WHEN ok THEN $me ELSE u.lease_owner END,
       u.lease_expires = CASE WHEN ok THEN $until ELSE u.lease_expires END
   FOREACH (_ IN CASE WHEN ok AND (prev IS NULL OR prev <> $me) THEN [1] ELSE [] END |
       CREATE (s)-[:HELD {epoch: u.lease_epoch, from: $now, until: $until}]->(u))
   ```
   The epoch makes a write ticket from before a hand-off stale (`STALE_EPOCH`).
3. **If granted: what else does this change reach?** ([`REACH`](../nodes/ops.py#L196)). Units 1–3 call or import
   hops away that another live agent holds or plans to change. Advisory: the agent must look, then acknowledge.
   ```cypher
   UNWIND $uids AS id MATCH (u:Unit {uid: id})
   MATCH p = (u)-[:DEPENDS_ON|IMPORTS*1..3]-(v:Unit)
   WHERE NOT v.uid IN $uids AND v.lease_owner IS NOT NULL AND v.lease_owner <> $me AND v.lease_expires >= $now
   MATCH (o:Session {uid: v.lease_owner}) WHERE o.status <> 'done'
   RETURN u.uid AS mine, v.uid AS theirs, o.name AS owner, length(p) AS hops
   UNION ...  // the same walk to units another agent DECLARED an Intent on
   ```
4. **If refused: is this a deadlock?** A wait-for cycle back to me means `escalate`; otherwise record the wait
   and `defer`.
   ```cypher
   MATCH p = (:Session {uid: $owner})-[:WAITS_FOR*1..10]->(:Session {uid: $me})
   RETURN [n IN nodes(p) | n.name] LIMIT 1
   ```
   ```cypher
   MATCH (a:Session {uid: $me}), (o:Session {uid: $owner})
   MERGE (a)-[w:WAITS_FOR {unit: $u}]->(o) SET w.since = $now
   ```
   If the owner has finished, the unit is handed over instead (`handoff`), after a graph snapshot.

## Posting a fact: `post_fact` ([nodes/ops.py](../nodes/ops.py#L359))
The server checks the quote against the source (file lines, a commit, a test run) and stores the result:
```cypher
MERGE (src:Source {uid: $src}) ON CREATE SET src.kind = $kind, src.ref = $ref
... CREATE (:Fact {uid: $fid, text: $text, source_uid: $src, status: $status, recorded_at: $now})
MERGE (f)-[:SOURCED_FROM {quote: $quote}]->(src)
```

## Other tools
| tool | what it asks the graph | code |
|---|---|---|
| `context_for` | the units, owners, facts and history behind a plain-English task | [ops.py](../nodes/ops.py#L467) |
| `declare_intent` | creates `Intent -TOUCHES-> Unit`; returns same-unit overlaps and who declared first | [ops.py](../nodes/ops.py#L116) |
| `coordinate` | done (releases leases, notifies waiters), request, commit, answer | [ops.py](../nodes/ops.py#L401) |
| `impact_of` | everything 1–3 hops away along `DEPENDS_ON`/`IMPORTS`, with owners | [graphtools.py](../nodes/graphtools.py#L53) |
| `path_between` | the shortest call/import path between two units (`algo.SPpaths`) | [graphtools.py](../nodes/graphtools.py#L71) |
| `hotspots` | central units by PageRank (`algo.pageRank`) | [graphtools.py](../nodes/graphtools.py#L88) |
| `communities` | clusters of code (`algo.labelPropagation`) | [graphtools.py](../nodes/graphtools.py#L99) |
| `query_graph` | any read-only Cypher, with a timeout | [graphtools.py](../nodes/graphtools.py#L127) |
