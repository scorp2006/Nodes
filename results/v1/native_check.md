# Sprint step 7: native six-tool check (EXPLORATORY)

**Exploratory, not pre-registered.** One stochastic run per task. No claim rests on these numbers. The question is
whether the benchmarks run through the product's own tools (`--profile nodes`: context_for, declare_intent,
before_acting, post_fact, coordinate, status) rather than the harness tool names.

Branch `native-check`. Server: `python -m nodes up --profile nodes --port 8792`, run from this worktree, on
FalkorDB :6380 with workspaces `native-*`. Analysis: `verify/native_check.py` → `results/v1/native_check.txt`.

## Product code: unchanged
The nodes profile already does what the bench needs. `post_fact(text, source_ref={kind: "briefing", ref: "<n>"})`
source-checks against the `BRIEFED {num}` edge written by `harness.seed_board`, and stores the briefing text. Both
`status()` and `context_for()` list posted facts with poster and status. So no product change was made. The replay
check (`verify/replay_equivalence.py`, unchanged) still gives **4916/4916 replies identical, 566/566 recorded fields,
0 leaks, 0 mismatches**.

## HiddenBench: 20 dev tasks, graph condition, through the native tools
| | Stage 2 (`--profile harness-board`) | native (`--profile nodes`) |
|---|---|---|
| groups right (strict majority) | 13/20 | **12/20** (inside the 9–16 bar) |
| post_fact / share_fact calls | 306 (15.3 per task) | 242 (12.1 per task) |
| source-checked / unverified | 306 / 0 | **242 / 0**, and the product verdict agrees with the rule on 242/242 |
| reads per task (status+context_for vs shared_facts+who_has_not_shared) | 27.4 | **3.4** |
| board posts | 171 | 138 |
| leakage (unposted briefing fact in any status/context_for/post_fact reply) | 0 in 549 | **0 in 309** |
| replies cut by the 1500-token budget | — | 0 |
| cost (list price) / summed wall time | — | $2.34 / 210 min |

Rule for the verdict check: a post is source-checked only if `kind` is briefing and the ref is a fact number the
agent holds. The leakage scan follows every trace in order. A briefing fact counts as posted once its holder has
posted it source-checked, or once any agent has submitted its text itself. Any other briefing fact appearing in a
reply would count as a leak. There were none.

Per task, comparing Stage 2 with native: 10 tasks right in both, 5 wrong in both, 2 flipped to right (t12, t33),
and 3 flipped to wrong (t22, t34, t52). The per-task table, with post_fact and read counts, is in
`native_check.txt`.

**Main behavioral difference: agents rarely read the board.** 8 of 20 tasks had zero status/context_for calls.
Agents posted their facts but mostly did not pull everyone else's. In Stage 2, `shared_facts` was a dedicated
board read and `who_has_not_shared` nudged holders. The native tools have no counterpart to `who_has_not_shared`,
and `status` reads as a generic "what changed" tool. declare_intent, before_acting and coordinate went almost unused
(2 before_acting calls in total), which is expected for a discussion task.

## CooperBench: 2 dev pairs, native tools (small adapter)
Adapter: `--condition native` in `coop/run_cb_graph.py` and `coop/run_pair.ps1`. It reuses the Stage 2 product
path, keeps the PROCEDURE text identical, and changes only the tools paragraph (claim = before_acting proceed,
announce = coordinate(op="done", text=<commit>), wait = status(wait_seconds≤60)). The MCP server is named `nodes`.

| pair | Stage 2 (harness-coop) | native | native tool calls |
|---|---|---|---|
| click 2068 f1+f2 | clean merge, both pass: no | **merge conflicts**, tests not run | agent2 declare_intent ×1; agent1 used no Nodes tool |
| dspy 8394 f1+f2 | identical, both pass: yes | **merge conflicts**, tests not run | each agent: declare_intent ×1, before_acting ×1 (both proceed) |

Wall time: 93 s and 164 s. Cost: $0.17 and $0.23. Neither pair called `coordinate(done)` or `status`, so the
owner/builder hand-off never happened.

**Product gap found (dspy):** agent1 leased the function `Cache.get`, and agent2 leased the whole file
`dspy/clients/cache.py` (and its intent touched the class `Cache`). Both got `proceed`, and declare_intent reported no
overlap. A lease on a file or class does not block leases on the functions it contains, and the reverse is also
true. The dry run with function-level targets on both sides behaves correctly (overlap, proceed, defer, done,
handoff, checked without an LLM). So the failure is the granularity mismatch, not the protocol.

## Deviations from Stage 2
- Tools and prompt: the six native tools replace the three board tools. The prompt paragraph (`NATIVE_BOARD` in
  `pilot/run_hiddenbench.py`) explains post_fact and status/context_for. Nothing replaces `who_has_not_shared`.
  The NUDGE sentence is unchanged.
- Server: nodes profile on :8792 (Stage 2 used harness-board on :8791). Identity is a bearer token in both, read
  from the header by the nodes profile, which has no token verifier.
- Interpreter: the server runs on the v1 `.venv`, because the main `.venv` has no `tree_sitter_language_pack`. The
  runner runs on the main `.venv`, as before.
- CooperBench indexing is Python-only (`all_languages=False`) because `.venv-cb` has no tree-sitter. This matches
  the indexer Stage 2 used.
- CooperBench messaging Redis: `redis://localhost:6380/1` instead of `6379/1`, to keep off the research FalkorDB.
- Model, rounds, seed, profile, split, duplications, effort and workers are all identical (see the txt). CooperBench
  uses `claude-sonnet-5-5` at `--effort low`, as in Stage 2.
- No rate or usage limit was hit. No run was retried.
