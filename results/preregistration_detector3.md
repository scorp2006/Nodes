# Pre-registration: offline conflict-detector test v3 (all Python CooperBench repos)

Committed **before** any v3 patch is downloaded, any edited-function set is computed, or any test is run.
No model calls are involved at any step.

## Question
Does a call-graph path between two agents' edits, with or without shared-state edges, predict that their
separately-working patches break when merged, for pairs that edit **different** functions? This extends v2
(`results/preregistration_detector2.md`, `results/detector2_report.md`), which found no signal on 34 light-repo
pairs with only 4 failures.

## Frozen code (no tuning)
- **Graph:** `coop/world_graph_v2.py`, unchanged since c72f338.
- **Edited functions and files:** `coop/detector_eval.py` (`changed_lines`, `function_spans`, `touched_functions`),
  unchanged.
- **Predictor definitions and scoring:** exactly as in v2 (`coop/score_detector2.py`: `auc`, `boot_ci`, distance
  up to 6 hops, no path = 7, flag at ≤ 3 hops).
- **Ground truth:** CooperBench's evaluator at `63b9d44` (`run_patch_test`, `test_merged` with naive merge only),
  with the same eligibility and interaction-failure definitions as `coop/gt_interaction.py`.
- New code may only orchestrate: download, the static filter, ordering, the cut-off, and reading and writing
  rows. The graph is loaded into the separate `falkordb-scale` container (port 6390), never the main database.

## Data
- **New (heavy repos):** every Python task with published Claude k1 coop patches.
  - outlines 1371 / 1655 / 1706;
  - dspy 8394 / 8563 / 8587 / 8635;
  - datasets 3997 / 6252 / 7309;
  - llama_index 17070 / 17244 / 18813.

  That is 199 listed pairs. Only the published patches are used (HuggingFace `CooperBench/trajectories`, `coop/`).
  These tasks were never used to build or tune the graph (v2 was tuned only on click 2068 and 2800).
- **Pooled (light repos):** the v2 held-out rows (click 2956, jinja 1465 / 1559 / 1621, dirty-equals 43), reused
  as computed (`results/detector2_rows.json`). Click 2068 and 2800 stay excluded, because they tuned the graph.

## Procedure (speed rules)
1. **Static filter first.** For each new pair, map both patches to edited functions on the task's base commit.
   A pair is a **disjoint pair** if its two edited-function sets don't intersect (the v2 definition). Pairs with
   a missing patch, or whose repo fails to parse, are excluded and listed.
2. **Ground-truth tests only on disjoint pairs,** as in v2:
   - patch A alone, then patch B alone;
   - the merged tree only if both pass alone (eligible).
   - An eligible pair has an **interaction failure** if the naive merge conflicts, or the merged tree fails either
     feature's tests.
3. **Order:** repos in a seeded random order (`random.Random(20261025)`), and the pairs within each repo
   shuffled with the same generator.
   - One heavy repo at a time.
   - At most 2 test containers at once; drop to 1 if Docker memory use passes 75%.
   - Only the images for tasks that have disjoint pairs are pulled.
4. **Cut-off: 12:30 IST, 2026-10-18.** Rows finished by then are analyzed. Unfinished and untested pairs are
   listed with counts.
5. **No peeking.** Progress is reported as counts only (pairs done / total), never eligibility or failure. The
   analysis runs **once**, at completion or at the cut-off, whichever comes first.

## Analysis
- **Primary:** eligible **disjoint** pairs, pooled (new heavy + v2 light).
  - Report n and the number of interaction failures.
  - For each predictor (same file; CALLS distance; CALLS + SHARES_STATE distance), report AUC with a 95%
    bootstrap CI (2000 resamples, seed 20261023), plus flag precision and recall.
  - **Does multi-hop beat same file?** Answer it from the AUCs and their CIs. No significance claims beyond the
    CIs.
- **Secondary:** the same on the heavy-only disjoint pairs.
- **Reporting rule:** fewer than 10 interaction failures in the primary subset is too few to conclude anything.
  The report will say so plainly and present the numbers as descriptive only.
