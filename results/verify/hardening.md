# Architecture review and hardening

The study server (`coop/coop_tools.py`) is frozen while the CooperBench batch runs, because its code hash guards
against stale servers. Every fix below went into the product server `nodes/` (`core.py`, `server.py`). Each item
gives the problem, the fix, the test (in `nodes/test_nodes.py`, 9 tests, all passing) and the measured impact.
Measurements: `verify/scale_test.py` on the CPython 3.12 stdlib (15,713 nodes, 24,617 edges), results in
`results/verify/scale_test.txt` (study server) and `results/verify/scale_test_nodes.txt` (nodes).

| # | Problem (study server) | Fix (nodes) | Test | Impact |
|---|---|---|---|---|
| 1 | A client-sent `X-Log` header picks the file the server appends to, so any client that reaches the port can write any path | The log location is server-side (`<workspace>/.nodes/tools.jsonl`); the header is ignored | `test_mcp_over_http_end_to_end` (a hostile `X-Log` path is never created) | Security |
| 2 | A client-sent `X-World-Graph` header picks the graph, so a client can read or modify any team's graph in the database | The server is started for one team graph; clients can't choose it | structural (the header is never read) | Security |
| 3 | Name resolution uses `qualname = $n OR ENDS WITH $s`, a label scan on every call | Indexed exact match first; the suffix scan runs only for names that miss | `test_resolve_exact_suffix_and_ambiguous` | 28.4 ms → 1.4 ms for 5 names |
| 4 | The status digest lists every claim in the team: 16,012 characters once 300 functions are claimed, all of it re-read by the model on every status call | The digest is scoped to the caller's plan, claims and overlaps; the team list is capped at 30 | `test_digest_stays_small_as_the_team_grows` | 16,012 → 562 characters |
| 5 | Per-agent claim lookups (`claimed_by`) aren't indexed | Index on `Function.claimed_by` | measured | digest 431 ms → 12.7 ms with 100 agents |
| 6 | Claim is a compare-and-set query plus a separate read-back; claims can never be released | One query does the compare-and-set and returns owners; `release` added (owner only) | `test_claim_race_has_one_winner_per_function` (20 threads), `test_release_lets_a_teammate_claim` | correctness kept |
| 7 | Agent names aren't validated | 1–64 characters from `[A-Za-z0-9_.:-]`, checked at every entry point | `test_agent_names_are_validated` | Input validation |
| 8 | (new feature) Board facts quote a source file, and a path could escape the workspace | The path is resolved and must be inside the workspace; otherwise the fact is marked "not checked" | `test_fact_source_check` (`../`, absolute paths) | Security |
| 9 | (new feature) Web view static files | The resolved path must be inside `nodes/web/` | `test_web_state_and_no_path_traversal` | Security |
| 10 | The study's HTTP test binds the batch's own port 8774, so running the tests during a batch would collide with it | The nodes tests use free ephemeral ports and their own graphs | all nodes tests | Ops safety |

## Concurrency, before and after (real MCP sessions, 10 rounds of claim + status per agent, contested pool of 300 functions)

| agents | study server calls/s | nodes calls/s | claim p50 (study → nodes) | status p50 (study → nodes) | double-claims |
|---|---|---|---|---|---|
| 10 | 25 | 42 | 251 → 78 ms | 227 → 91 ms | 0 / 0 |
| 50 | 40 | 119 | 1,064 → 252 ms | 1,065 → 262 ms | 0 / 0 |
| 100 | 44 | 125 | 2,094 → 629 ms | 1,986 → 617 ms | 0 / 0 |

Zero double-claims is checked three ways at every level:
- no function is reported as won by two agents;
- the database owner equals the reported winner;
- no function's version went above 1.

The conflict query itself takes milliseconds at every depth (p50 2.4–3.7 ms, p99 ≤ 32 ms at depths 1–4), so the
remaining latency comes from the single-process Python server, not the graph.

An intermediate result is kept for the record. With fixes 3, 4 and 6 but without index 5, the nodes server got
slower at 100 agents (16 calls/s). The scoped digest's per-agent claim lookups were unindexed. Adding index 5 fixed it.

## Known limitations (not fixed)
- **Identity is trusted.** `X-Agent` isn't authenticated, so the server is for one trusted team on localhost.
  Per-agent tokens are the upgrade path.
- **Claims never expire.** An agent that crashes keeps its claims until someone calls `release` on its behalf.
  A lease with a TTL is the upgrade path.
- **The call graph is static.** Call-site resolution was 29% (v1) and 36–37% (v2) on the dev tasks.
  Dynamic dispatch isn't seen.
- **p99 is about 2 s even at 10 agents** for both servers. This was not investigated; it is likely session/stream
  setup in the MCP HTTP transport rather than the graph.
- **One server process, and FalkorDB serializes writes.** Measured throughput is about 125 calls/s.
- **Batch exposure to items 1–2.** Batch agents run in containers with a shell, so they could in principle send
  their own headers to the study server. After the batch, the tool logs will be checked for any agent name outside
  the expected pair.
