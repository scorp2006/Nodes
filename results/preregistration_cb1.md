# Pre-registration: CooperBench batch `cb1`

Written and committed **before** any test-pair run. Nothing below may change after results are seen.

## Frozen code
- Our code: branch `next`, commit `8b91f6f95b4180ae877904dedf7a89ea8a0a0ac5` (everything under `coop/`).
- CooperBench: commit `63b9d44` (MIT), files unmodified. At runtime the launcher (`coop/run_cb_graph.py`) changes only:
  (1) container cleanup, replacing a Linux-only shell command so containers are removed on Windows (all conditions);
  (2) for the protocol and graph conditions, it appends their prompt text and adds `--mcp-config`.
- Agents: CooperBench's `claude_code` adapter, model `claude-haiku-5-5`, auth by `CLAUDE_CODE_OAUTH_TOKEN`.
  Claude Code inside the containers is installed by CooperBench's setup script as npm `latest` at run time.
  We record the version each run reports; it is not pinned.
- Graph database: FalkorDB v4.22.1 (the same pinned image as the HiddenBench study). Messages use Redis database 1.

## Pairs
- The 40 test pairs in `coop/test_sample_40.json`: 3 random pairs from each of the 14 Python-only test tasks
  (`huggingface_datasets_task/7309` has only 1 pair), drawn with `random.Random(20261019)` before any test run.
- The 6 dev tasks (`coop/splits.json`) were the only ones used for development: click 2068 and dspy 8394.
  No test pair has been run.

## Settings (identical in every condition)
`cooperbench run --setting coop --git --backend docker -a claude_code -m anthropic/claude-haiku-5-5`, one run per
pair per condition, `--no-auto-eval`, evaluated once at the end by CooperBench's own evaluator (`coop/eval_all.ps1`).
Every run goes through the same launcher (`coop/run_pair.ps1` → `coop/run_cb_graph.py`).

## Conditions (3)
| Condition | Added to each agent's instruction | Tools |
|---|---|---|
| `coop` | nothing (CooperBench as published, with `--git`) | none |
| `protocol` | the coordination procedure (`PROCEDURE` in `run_cb_graph.py`) + how to claim, announce and wait over CooperBench's chat (`coop-send` / `coop-recv`) | 5 private tools that share nothing: `impact_of`, `record_plan`, `my_plan`, `write_note`, `read_notes` |
| `graph` | the **same** procedure, word for word + how to claim, announce and wait through shared tools | 5 shared tools over the repo's world graph: `declare_plan`, `claim` (atomic first-wins), `mark_committed`, `coordination_status` (server-side wait), `impact_of` |

The procedure: settle ownership of overlapping functions (first claim wins). The owner implements its whole
feature, commits, pushes and announces. The builder doesn't edit owned functions, merges the owner's commit, builds
on top, pushes and announces. The owner then `git merge --ff-only` to the builder's branch. Protocol and graph get
an identical procedure; only the mechanism for claiming, announcing and waiting differs. Neither our prompts nor our
tools mention scoring. The only scoring information is CooperBench's own `--git` text, which all conditions get.
`coop` does not get the procedure, so comparisons against `coop` mix procedure and tools (secondary only).

## Outcomes
- **Pass** (primary metric): CooperBench's `both_passed`, meaning both features' tests pass on the evaluated tree.
- **Coordinated** (secondary): the two submissions combine without conflict (merge status `identical` or `clean`).

## Comparisons
Exact two-sided paired sign test on pairs where the two conditions disagree; threshold **p < 0.05**.
- **Primary:** `graph` vs `protocol` on pass.
- **Secondary** (Holm-Bonferroni across these three; a secondary result is claimed only if it survives Holm):
  1. `graph` vs `coop` on pass
  2. `protocol` vs `coop` on pass
  3. `graph` vs `protocol` on coordinated
- Everything else (feature-level passes, turns, tokens, time, tool use) is descriptive only.

## Failures
- An **infrastructure failure** is a run where any agent errored with 0 turns (for example a container start
  timeout or an auth error), or where CooperBench's evaluator reports an error.
- Failed runs are retried by relaunching (up to 2 relaunches). A pair that still has a failed run in any condition
  is **dropped from all three conditions**. `coop/analyze_cb.py` enforces this.

## No peeking
Runs use `--no-auto-eval`, so no pass/fail exists until the end. Progress is checked only by counting finished
runs (`<name>.wall.txt`). Nobody opens patches, transcripts or eval files before the end.
After every run (and relaunch) is done: `coop/eval_all.ps1 -Plan coop/plans/test1.csv`, then, once:
```
python coop/analyze_cb.py coop/plans/test1.csv runs/cooperbench
```

## Logged per run
Turns, output and cache tokens, agent seconds, wall-clock seconds, peak container RAM, messages, and every tool call.

## Scheduling (does not change any single run)
Each pair's three conditions run back to back, in an order shuffled by `random.Random(20261020)` (`coop/plans/test1*.csv`).
Light repos (click, jinja, dirty-equals: about 0.4 GB per container) run up to 4 pairs in parallel.
Heavy repos (dspy, outlines, datasets, llama-index: up to 13.6 GB per container seen in dev) run one pair at a time.

## Prediction written before any test run (from the 2 dev pairs)
On both dev pairs, protocol and graph each produced identical trees (coordinated) while coop conflicted, but every
condition failed CooperBench's pass metric because one feature's own tests failed (a coding limit, not
coordination). So on the primary (graph vs protocol on pass) we expect **no significant difference**, and we expect
both procedure conditions to beat coop on **coordinated**. A null primary result will be reported as such.

## What needs approval before launch
- About **24 GB** of task-image downloads (14 images, `akhatua/cooperbench-*`, 0.26–5.4 GB each); 192 GB free.
- 120 runs. Estimated **6–11 hours** wall-clock (the heavy repos are the bottleneck), about 4,000 model turns,
  about 2.5 M output tokens, on the subscription. Optional speed-up: raise Docker's memory limit above 15 GB so two
  heavy pairs can run at once (a Docker Desktop setting; your decision).
