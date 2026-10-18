# v1 sprint report

Branch `v1`; main is still frozen. All numbers come from the files in this folder.

## Replay check (the standing gate)

`verify/replay_equivalence.py` on 804d786 (dashboard, ordinal regions, unique ids, co-change fix):
4916/4916 replies identical, 566/566 recorded fields, 0 leaks, 0 mismatches.
The later commits only touch the hook identity path (product only) and the token-mint event, which the replayed runs
don't use. I'll rerun it once more right before the merge into main.

## What works

| Area | Status | Evidence |
|---|---|---|
| Multi-language units (14 languages, tree-sitter) | works | `ingestion_test.txt`: coverage 100% on 7 repos, spans 20/20 |
| Git import: commits, PRs, branches, co-change, blame ownership, rationale | works | co-change and ownership match raw `git log` / `git blame` on every repo |
| Incremental re-index | works | re-init identical on 7/7 repos; one commit changes 2-3 units |
| Hook identity (no token): sessions, subagents (SPAWNED), worktrees → one workspace | works | `subagent_test_run2.txt`; live runs below |
| Sessions named after their worktree branch (alice, bob) | works | screenshots |
| `nodes up` / `nodes init`, LAN token gate | works | `nodes/test_product.py` (LAN test), clean install below |
| Opt-in `import-sessions` (redacted, `extracted` trust) | works | `nodes/test_v1.py` |
| Live dashboard: SSE, story feed, lanes, focused graph, facts with trust badges, counters, timeline, raw call + Cypher drawer | works | screenshots below |
| `nodes replay` (recorded run → `<ws>-replay` at original pace) | works | checked on the recorded `shop` run |
| Trace redaction | works | no tokens in any trace or result file (scanned) |

Tests: `nodes/test_v1.py` + `nodes/test_product.py`, 15 passed.

## Live 2-agent run (two headless Claude Code agents, Sonnet, low effort, v1 local mode, no tokens)

| | run 1 | run 2 (named lanes) |
|---|---|---|
| wall time | 176 s | 178 s |
| gate decisions | 7 proceed, 6 hand-off | 8 proceed, 5 hand-off |
| both branches merged cleanly | yes | yes |
| both features present, merged tests | yes, 5 passed | yes, 3 passed |

Screenshots (run 2): `screenshots/live_1_both_working.jpg` (lanes, holdings, graph colored by holder),
`screenshots/live_2_handoffs.jpg` (alice finishes, Nodes hands her code to bob),
`screenshots/live_3_raw_call_and_cypher.jpg` (click on a story line: the raw hook response and every FalkorDB query
behind it, with timings).

## What is approximate

- Calls outside Python packages are name matches (`resolved_by: 'name-match'`, confidence 0.3/0.5), never cross-language.
- Function counts vs ctags: Go identical (ctags' extra count is struct fields); JavaScript 3-25% lower (ctags also tags nested
  functions, object-literal methods, callbacks); TypeScript 9% higher (arrow functions assigned to consts); Python 0-7% lower
  (nested functions). Details in `ingestion_test.txt`.
- The replay workspace's graph is the source graph's final state; only the event stream is re-timed.
- Runs recorded before tracing existed (e.g. the old `shop` run) have no raw call to show; the drawer says so.
- Graph layout is a small force layout: readable at ~30 nodes, crowded beyond that.

## Clean install (fresh clone, new folder, new venv)

Each failure is recorded as found; the fixes are in v1.

1. No Python 3.12 on the machine (my dev venv came from uv). Python 3.11 works; SETUP.md says 3.11 or 3.12.
2. `ModuleNotFoundError: tree_sitter_language_pack`: missing from requirements.txt. Fixed (88ea325).
3. The project MCP server shows "pending approval" until Claude Code is run interactively once in the repo. Documented.
4. In an untrusted folder Claude Code ignores the repo's `permissions.allow` and doesn't load `.mcp.json`, so the
   Nodes tools are missing (hooks still gate edits). Documented: open `claude` once and accept trust + the server.
   For headless runs, pass `--mcp-config .mcp.json` with the prompt on stdin (`--mcp-config` swallows a prompt argument).
5. The old `docker-compose.yml` / `Dockerfile` started the v0 server with flags v1 no longer has. Compose is now
   FalkorDB-only (port 6380) and the Dockerfile is removed: the server must run on the host to read the repo and be
   reachable by the hooks.

After the fixes: init → up → a real Claude Code session in the fresh repo, with its edit gated (lease granted),
`declare_intent` traced, and nothing token-like in the trace.

## Bugs found by the tests this sprint (all fixed)

- Line-number region ids renamed regions on unrelated edits → ordinal ids.
- Same symbol twice in one file (property setters, try/except fallbacks) shared one uid, so re-init wasn't idempotent → `Name@2`.
- Co-change capped commit size on indexed files only → it now uses the full commit.
- The edit hook was Python-only, then crashed on repos without a package (it failed open) → any indexed language, no package needed.
- `/api/graph` used `EXISTS {}`, which FalkorDB 4.22 doesn't support → pattern predicates.
- The demo minted tokens that v1 local mode never used, so sessions showed as `main-xxxx` → branch names, no tokens.

## Not done yet

- Step 7, the native 6-tool check (20 HiddenBench dev tasks + 2 CooperBench dev pairs on `--profile nodes`): needs a new
  runner condition (briefing facts posted through `post_fact` with a briefing source, read back through `status` /
  `context_for`), then a full bench run. Not started, to protect usage limits.
- Merge v1 into main: after the final replay check.
- The README still describes the v0 setup (ports 8781, docker compose); SETUP.md is current. I left the README alone, as asked.
