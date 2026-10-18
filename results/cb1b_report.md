# CooperBench cb1b: result (pre-registered, blind, analyzed once)

Pre-registration: `results/preregistration_cb1b.md` (code frozen at 595bf2f). Plan: `coop/plans/test1b.csv`.
- 45 pairs × 3 conditions = 135 runs.
- Claude Sonnet 5.5, `--effort low`, light Python repos (click 2956; jinja 1465 / 1559 / 1621; dirty-equals 43).
- All 270 agents submitted, with 0 infrastructure failures, so no relaunches and no dropped pairs.
- Evaluated once with CooperBench's evaluator (`63b9d44`); analyzed once with `coop/analyze_cb.py`
  → `cb1b_primary.txt`.
- Independent recompute: `verify/cb1b_breakdown.py` → `verify/cb1b_breakdown.txt`. It matches.

Conditions:
- **coop:** CooperBench's own chat;
- **protocol:** the same claim / hand-off procedure carried out over chat messages;
- **graph:** the same procedure through the shared FalkorDB graph (atomic claims, server-side status and waits).

| condition | merge success (coordinated) | both features pass | agent turns | output tokens | agent seconds |
|---|---|---|---|---|---|
| coop | 19/45 (42%) | 16/45 (36%) | 9.6 | 4,221 | 117 |
| protocol | 22/45 (49%) | 16/45 (36%) | 11.6 | 4,611 | 313 |
| **graph** | **35/45 (78%)** | **23/45 (51%)** | 19.3 | 5,171 | 185 |

## Pre-registered tests (exact two-sided paired sign test, Holm within each family of 2)
| family | comparison | better / worse | Holm p | |
|---|---|---|---|---|
| primary | graph vs protocol, merge success | 14 / 1 | **0.002** | significant |
| primary | graph vs protocol, pass | 10 / 3 | 0.092 | not significant |
| secondary | graph vs coop, merge success | 17 / 1 | **0.0003** | significant |
| secondary | graph vs coop, pass | 11 / 4 | 0.118 | not significant |

**Reading:**
- The graph makes two agents' work combine far more often, against both the same procedure run over chat and
  plain coop.
- On whether both features end up passing, graph is ahead (23 vs 16), but the difference is not significant at
  this sample size. We do not claim it.
- The procedure alone, over chat, barely moved merge success compared with coop (22 vs 19). The gain comes from the
  graph mechanism (atomic ownership plus server-side state), not from the instructions.

## Exploratory (not pre-registered, `verify/cb1b_breakdown.txt`)
- **Merge status:**
  - graph: 19 identical, 16 clean, 10 conflicts;
  - protocol: 2 identical, 20 clean, 23 conflicts;
  - coop: 19 clean, 26 conflicts.
- **No "identical" merge is empty.** An identical merge with an empty diff would make the merge-success
  outcome trivially true; this rules that out.
- **Merge success goes with passing:**

  | condition | both pass when merged | both pass when not merged |
  |---|---|---|
  | coop | 14/19 | 2/26 |
  | protocol | 14/22 | 2/23 |
  | graph | 22/35 | 1/10 |

- Per-feature passes in graph: 31 and 26 of 45. Protocol: 20 and 18. Coop: 17 and 18.

## Checks
- Tool logs (`verify/cb1b_tool_log_agents.txt`): 246 tool calls in 45 files, all from `agent1` or `agent2`. No
  agent used another identity (see `verify/hardening.md`, items 1–2).
- Comparability: published CooperBench conflict rates came from an older merge policy with a union fallback, so our
  coop numbers aren't comparable to them (`coop/merge_probe.py`).

## Limitations
- One run per pair per condition.
- Light repos only (5 tasks); the heavy repos may differ.
- One model, at low effort.
- The Claude Code version inside the containers isn't pinned.
- The graph uses more turns (19 vs 12 for protocol) but less agent time than protocol (185 s vs 313 s). The protocol
  procedure waits with `sleep 30` polls, while graph waits server-side (an inference from the procedure text;
  not measured).
- During the batch, I read the launcher logs (agent status, steps and cost, which contain no pass or merge
  information) to check for usage-limit errors. Nothing else was opened before the analysis.
