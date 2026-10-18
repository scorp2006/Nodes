# How Nodes uses FalkorDB (each feature, why, and the measured number)

FalkorDB v4.22.1, pinned by image digest (`sha256:b0bdcac2…7ae7a`). The product runs it in its own container
(`nodes-falkordb`, :6380), separate from the research harness's database.

Sources for the numbers:
- `verify/falkordb_usage_bench.py` → `results/repro/falkordb_usage_bench.txt`: the click 2068 workspace, 563 units.
- `results/demo/e2e_run2_latency.txt`: two live Claude Code agents.
- `results/verify/scale_test*.txt`: the CPython stdlib graph.

| Feature | What Nodes uses it for | Why it matters | Measured |
|---|---|---|---|
| **Property graph + Cypher** | Units (functions, imports and module blocks, files), Sessions, Intents, Decisions, Facts, Sources, and the edges between them | Ownership, provenance and decisions are relationships, and one query can join them | Indexing 563 units + 324 call edges: 0.25 s |
| **Per-query atomicity, serialized writes per graph** | The lease: an all-or-nothing compare-and-set over every target unit, plus the epoch increment and the `HELD` history edge, all in **one** query | No lost updates and no double ownership, without application locks | Lease query p50 1.19 ms, p99 5.41 ms. **50 concurrent agents → 1 winner**, epoch 1. 100 agents × 10 rounds: 0 double-claims (scale test) |
| **UNIQUE constraint** on `uid` (all 9 core labels) | Every node has one stable identity (`fn:repo:path#symbol`) | Duplicate nodes are impossible, enforced by the database rather than our code | 2/2 duplicate creates rejected. Indexed uid lookup p50 0.82 ms |
| **MANDATORY constraint** (`Fact.source_uid`, `Decision.reason`, `Decision.verdict`, `Session.principal`) | Provenance is a database guarantee: a fact without a source, or a decision without a reason, can't be stored | The proven ingredient (source-checked facts) can't be bypassed by a buggy writer | 2/2 writes without provenance rejected (+ `test_database_enforces_uniqueness_and_provenance`) |
| **Indexes** (uid, `lease_owner`, `path`, `qualname`, `token_hash`, …) | Every gate lookup is an index seek | Latency stays flat as the graph grows | Gate (resolve + lease + impact + Decision write) p50 9.2 ms, p99 28 ms. Live agents: all 212 queries p50 1.31 ms, p99 7.43 ms |
| **Graph per workspace** (`ws:{name}`) | Isolation between teams, runs and benchmark replays | One tenant can't see or block another; each replay starts clean | A second workspace sees 0 of the first's units. 128 replay workspaces in Stage 1 |
| **`GRAPH.COPY`** | A snapshot before every hand-off, and before a `git merge` seen by the hook | A dry-run and replay point for the exact state at hand-off time | 1,015-node graph: p50 98 ms. It shows up as the p99 of the live pre-edit hook (122 ms) |
| **Redis Streams** (`ws:{ws}:events`) | Append-only event log: joins, intents, decisions, hand-offs, facts | History is never overwritten; `nodes watch` tails it | On every write. Event + notify p50 0.55 ms (smoke run) |
| **Redis pub/sub** (`ws:{ws}:notify:{agent}`), same server | `status(wait_seconds)` blocks on a notification instead of polling | An agent waiting for a hand-off learns immediately and costs no model turns | Publish → receive p50 0.52 ms, p99 5.8 ms. The research harness polled every 2,000 ms (about 1 s average delay) |
| **Constraint states (`db.constraints()`)** | Setup waits until constraints are OPERATIONAL | Constraints are built asynchronously, and writes before that would go unchecked | Waited on every workspace setup |

## Things we learned about FalkorDB along the way
- **A MANDATORY property is checked when MERGE creates the node, before `ON CREATE SET` runs.** So a Fact has to be
  created with its `source_uid` already in the creating pattern. We use `OPTIONAL MATCH` + `FOREACH CREATE` in one
  query.
- **UNIQUE needs the exact-match index to exist first,** and constraint creation returns `PENDING` until it is built.
- **`shortestPath` is only allowed in `WITH`/`RETURN`, and undirected `shortestPath` isn't supported.** We use
  `algo.SPpaths(..., relDirection: 'both')` instead (detector studies).
- **`NOT ()-[]->(x)` after a `WITH` aggregation fails.** Use `OPTIONAL MATCH` + `count`.
- **The Python client decodes Redis replies to `str`.** Stream fields and hash keys arrive as text, not bytes.
- **`algo.pageRank` takes no seeds or weights.** Personalized ranking would have to be computed in the server
  (not built; see NODES-ARCHITECTURE.md §16).

## What we checked and do not use
- Vector index: no embeddings in v0.1.
- Graph algorithms beyond SPpaths, used only in the offline detector studies.
- Multi-hop overlap as a decision input: our pre-registered tests found it doesn't predict merge failures, so it's
  shown for information only.
