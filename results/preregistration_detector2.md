# Pre-registration: offline conflict-detector test v2

Committed **before** any ground truth or predictor score is computed. No model calls are involved.

## Why v2
v1 (`results/detector_eval.txt`) used the published Dec-2025 merge reports as ground truth. Those reports came from
CooperBench's older merge policy (`naive → union → solo`), and under the current evaluator the same published patches
conflict (5/5 tested on click 2068; `coop/merge_probe.py`). v1's "textual conflict" ground truth is therefore
**retracted**. v1 also scored multi-hop against a target ("merged tree fails") that mostly measures each agent's own
bugs.

## Data
CooperBench's published coop trajectories (Claude, k1), using each agent's final patch.
- **Dev tasks** (used only to build and tune the graph): click 2068, click 2800.
- **Held-out tasks** (used only for the final scores): click 2956, jinja 1465, jinja 1559, jinja 1621, dirty-equals 43.
  These are 199 pairs.

## Ground truth: interaction failure
Run with CooperBench's **current** evaluator (`63b9d44`, Docker):
- A pair is **eligible** if patch 1 alone passes feature 1's tests **and** patch 2 alone passes feature 2's tests
  (`run_patch_test`).
- An eligible pair has an **interaction failure** if the naive merge of the two patches conflicts, or the merged tree
  fails either feature's tests (`test_merged` with naive merge only; the agent1-alone fallback does not count).
- Ineligible pairs (either patch fails its own tests alone) are excluded: their failures aren't interactions.

## Focus subset
Eligible held-out pairs whose two patches edit **disjoint sets of functions**, where "same function edited" can't
help by definition. This subset is the primary analysis; all eligible held-out pairs are reported as secondary.

## Graph improvements (built and checked on dev tasks only, frozen before held-out scoring)
1. Call resolution: `self.method()` / `cls.method()` through **inheritance** (base classes defined in the repo),
   `obj.method()` where `obj` is a local, a parameter with a repo-class annotation, or `self.attr` assigned a repo
   class instance, and `super().method()`.
2. Shared-state edges: `(:Function)-[:SHARES_STATE]-(:Function)` when two functions read or write the same class
   attribute (`self.x`, scoped to the class and its subclasses) or the same module global.
The call-site resolution rate on dev tasks is reported before and after, in the same units as before (resolved call
sites / attempted call sites, and excluding Python builtins).

## Predictors (scored on held-out tasks only)
1. **Same file**: 1 if the patches share an edited Python source file (binary).
2. **Call graph, multi-hop**: shortest path between any edited function of agent 1 and any of agent 2 over `CALLS`
   (undirected), up to 6 hops. Score = −distance (no path = −7). Flag at ≤ 3 hops.
3. **Call graph + shared state**: the same over `CALLS | SHARES_STATE`. Flag at ≤ 3 hops.
For each predictor: precision and recall of the flag, and **AUC** of the score (ties count half; a binary predictor's
AUC equals its balanced accuracy).

## Reporting
Primary: AUC of each predictor on the focus subset. Secondary: the same on all eligible held-out pairs. Results are
reported whichever way they come out, including if no predictor beats chance (AUC 0.5). n and the number of positives
are always shown, because few positives make AUC noisy. No significance claims are made beyond 95% bootstrap CIs on
AUC (2000 resamples, seed 20261023).
