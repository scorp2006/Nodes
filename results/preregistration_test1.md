# Pre-registration: held-out test batch `test1`

Written and committed **before** any test-split run was started. Nothing below may change after results are seen.

## Frozen code
- Code commit: `d3ceaf0da54b8d3db99dee4e2ae0f4934c517990` (every file under `pilot/` as of this commit)
- Agent runtime: `claude -p`, Claude Code 2.1.296, model `claude-haiku-5-5`, one stateless isolated call per agent turn
- Graph database: FalkorDB v4.22.1, image `falkordb/falkordb@sha256:b0bdcac2cbaff989e77cb0610439b7a9289e86de033ccbbd4368b66fb680ae7a`
- Benchmark: HiddenBench (Li, Naito & Shirado, ICML 2026; MIT license), commit `3be6ca1`, its own simulator and scorer, unmodified

## Tasks
- The 45 test tasks in `pilot/splits.json`, frozen before any dev run.
- **Primary analysis uses 42 tasks.** Tasks 1, 2 and 3 are excluded from all analyses below, because they are identical to the toolkit's 3-task practice set (`benchmark_short.json`). Task 2 was run repeatedly during development and tasks 1 and 3 share its scenario. They still run, and are reported separately as non-confirmatory.

## Protocol (identical for every condition)
Hidden profile, 15 rounds, HiddenBench default prompts and voting rules, final vote on the last round, one run per task per condition, base seed 2026 with HiddenBench's per-task seed scheme.

## Conditions (5)
| Condition | Added to every agent's prompt | Tools |
|---|---|---|
| `baseline` | nothing | none |
| `nudge` | one sentence: others may hold genuine facts you lack | none |
| `placebo` | nudge + private notepad description | `write_note`, `read_notes`, `review_my_facts` (private, add no information) |
| `graph_unverified` | nudge + shared board description | `post_claim` (free text, not checked), `shared_claims`, `who_has_not_posted` |
| `graph` | nudge + shared board description | `share_fact` (by fact number, marked *source-checked*), `shared_facts`, `who_has_not_shared` |

"Source-checked" means only that the posted fact is in the poster's own briefing. It never uses the answer key, and the reply to the poster is yes/no only. In every condition, nothing an agent has not chosen to post is ever shown to another agent (enforced by tests in `pilot/test_graph_tools.py`).

## Outcome
Group correct = a strict majority of final votes names the correct answer (HiddenBench majority rule). One 0/1 outcome per task per condition.

## Comparisons
Test for all: **exact two-sided paired sign test** on tasks where the two conditions disagree. Threshold **p < 0.05**.

- **Primary:** `graph` vs `placebo`.
- **Secondary** (Holm-Bonferroni across these four; a secondary result is claimed only if it survives Holm):
  1. `graph` vs `baseline`
  2. `graph` vs `nudge`
  3. `graph` vs `graph_unverified`
  4. `graph_unverified` vs `placebo`
- Everything else (individual accuracy, other pairs, tasks 1–3, tool-use patterns) is descriptive only.

## Failures
Each call retries 3 times with back-off. Failed runs are retried by relaunching the batch (up to 2 relaunches). A task that still fails in any condition is **dropped from all five conditions**. `pilot/summarize.py` enforces this.

## No peeking
Runs print progress counts only (`--no-peek`). Results are opened once, after all 225 runs have finished or the failure rule has been applied, by running:

```
python pilot/summarize.py runs/test1 --exclude t1_d0 t2_d0 t3_d0
```

## Logged per run
Calls, turns, output/input/cache tokens, model seconds, wall-clock seconds, list-price cost, and every tool call with agent and tool name.

## Deviations from the dev batch (`runs/dev1`), all decided before any test run
1. Board wording changed from "verified" to "source-checked"; `share_fact` now replies yes/no only (it used to echo the posted text back to the poster).
2. `graph_unverified` is new. It has no dev data and was smoke-tested only on practice task 2 (2 rounds).
3. Turns and wall-clock time are now logged per run.
4. Up to 6 runs per condition execute in parallel (30 at once). Each run is independent, so this does not affect any single run.
5. The summary now counts only tasks finished in every condition, and lists the pre-registered pairs.
