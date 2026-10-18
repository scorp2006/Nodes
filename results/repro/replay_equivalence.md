# Reproduction Stage 1: replay equivalence (zero LLM calls). PASS

**Question.** Does the Nodes product, on its own code path, behave exactly like the research harness that produced
the published results?

**Method: differential replay.** Every recorded tool call from the harness runs was fed, in recorded order, into
**both** the frozen harness code and the product's harness profile, and their replies were compared:
- **Frozen harness:** `pilot/graph_tools.py` and `coop/coop_tools.py`, on FalkorDB :6379.
- **Product:** `nodes/harness.py` on the product schema (Session, Unit leases, Intent, Fact + Source), on its own
  FalkorDB container :6380.
- **Workspaces:** a fresh one per run.

On top of the side-by-side comparison, every field the original runs recorded was checked against the replay.

Script: `verify/replay_equivalence.py`. Output: `results/repro/replay_equivalence.txt`.

| | runs | tool calls | result |
|---|---|---|---|
| HiddenBench board (dev1, test1, Sonnet replication) | 83 | 4,670 | — |
| CooperBench cb1b graph | 45 | 246 | — |
| **reply text identical (harness vs product)** | | **4,916 / 4,916** | **100%** |
| **recorded fields matched** | | **566 / 566** | **100%** |
| **leaks** (an unposted briefing fact appears in a product reply) | | **0** | |
| mismatches | | 0 | |

The recorded fields checked:
- **HiddenBench:** the final board posts, for each run (recorded vs harness replay, and recorded vs product).
- **CooperBench:**
  - names resolved by `declare_plan` and its conflict flag;
  - claim winners and losers (who owns what);
  - owned-function counts on `mark_committed`;
  - the role step and conflict flag on `coordination_status`.

## The one modeling decision (documented in the script)
The first replay matched every reply text, but 24 recorded fields disagreed. All 24 were the conflict flag on the
**first** `declare_plan` of a CooperBench run. Harness and product agreed with each other, but both disagreed with
the recording.

**Cause: the log is in response order.** In the live runs, the first agent's `declare_plan` waited on the server,
up to 60 s, for the partner's plan before answering. So that answer already saw the partner's plan, even though its
log line comes first.

**Fix.** The replay now models that wait. When the caller is alone and the partner's `declare_plan` is logged within
60 s, the partner's plan is applied first, in both systems. This was applied 45 times, once per run, and nothing else
is reordered. With it, all 566 recorded fields match.

## What this covers, and what it doesn't
- **Covered:**
  - the exact tool replies agents saw;
  - first-claim-wins ownership;
  - source-check verdicts;
  - the no-leak rule;
  - role and hand-off text.

  These run on the product's data model, its atomic lease query and its bearer-token sessions.
- **Not covered:**
  - **Model behavior:** that is Stage 2, a live regression on dev tasks (`results/repro/preregistration_stage2.md`).
  - **Server-side waits on the wall clock:** they are modeled, not re-timed.
  - **5 early CooperBench dev logs:** they used an older tool set (`check_conflicts`) and were not replayed.
  - **The product-native profile** (the 6 tools): Stage 3, exploratory.
