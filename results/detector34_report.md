# Detectors v3 and v4: results (pre-registered, scored once, together)

Pre-registrations:
- v3: `results/preregistration_detector3.md` (6336f4d), with amendment 1 (375f559) and amendment 2 (3785d10).
- v4: `results/preregistration_detector4.md` (5811e98), with amendment 1 (cbcb6b1).

All were committed before any score was viewed. No model calls were made.

Inputs and outputs:
- Ground truth: `results/detector3_gt.jsonl` (3d144b4). Signals: `results/v4_signals/` (70b864b).
- Scores: `detector3_scores.txt`, `detector4_scores.txt`.
- Per-pair rows: `detector3_rows.json`, `detector4_rows.json`.
- Exploratory: `detector34_exploratory.txt` (`verify/detector34_exploratory.py`).

## What ran
- **Heavy repos:** 199 published pairs; 109 have disjoint edited functions. 52 were tested by the 12:30 cut-off
  (all 29 pairs where both patches edit a function, then 23 others); 57 were not tested. 0 infrastructure errors.
  17 tested pairs were eligible (each patch passes its own tests alone).
- **Pooled with the v2 light rows:** 51 eligible disjoint pairs, **14 interaction failures**. That clears the
  pre-registered threshold of 10 failures, so the primary analysis can be read.
- **Coverage (v4)** was available only for the light repos and outlines 1371. Heavy-repo installs failed (see
  v4 amendment 1).

## Primary: pooled eligible disjoint pairs (n = 51, 14 failures)
| predictor | AUC [95% CI] | flag precision | flag recall |
|---|---|---|---|
| same file | 0.500 [0.500–0.500] | 14/51 | 14/14 |
| CALLS distance (≤ 3 hops) | 0.405 [0.342–0.464] | 0/7 | 0/14 |
| CALLS + SHARES_STATE distance | 0.276 [0.170–0.398] | 2/23 | 2/14 |
| v4 same function (baseline) | 0.500 | nothing flagged | 0/14 |
| v4 coverage overlap | 0.303; vs same function −0.197 [−0.311 to −0.067] | 2/21 | 2/14 |
| v4 co-change | 0.486; vs same function −0.014 [−0.043 to 0.000] | 0/1 | 0/14 |
| v4 combined risk (fixed weights) | 0.288; vs same function −0.212 [−0.324 to −0.084] | 0/7 | 0/14 |

**Answers to the pre-registered questions:**
- **Multi-hop does not beat same file.** Both call-graph predictors score below chance, and their CIs exclude
  0.5.
- **No new v4 signal beats same function.** Coverage overlap and the combined risk score are significantly
  worse than chance. Co-change is indistinguishable from chance.
- **Secondary subsets.** Heavy-only: n = 17, 10 failures. Both-patches-edit-functions: n = 24, 2 failures.
  Coverage-only tasks: n = 34, 4 failures. These point the same way, and the last two have too few failures to
  conclude anything.

## Why below chance (exploratory, `detector34_exploratory.txt`)
- **Pairs where both patches edit functions:** 2 failures in 24 (8%).
- **Pairs where one or both patches edit no function:** 12 failures in 27 (44%). These include module-level
  type aliases, imports and alias lists.
- **All 14 failures are textual merge conflicts.** None merged cleanly and then failed its tests.

Graph signals (paths, shared tests, co-change) exist only between functions. So they're present mostly in the
pairs that rarely break, and absent in the pairs that do. That is what drives the AUC below 0.5. It says nothing
about graph connections *protecting* a pair.

**Line overlap.** "Both patches change lines within 0 of each other in a shared file" separates the failures
perfectly: AUC 1.000, 14/14 flagged, no false alarms. **That is near-circular and is not a finding about the
graph.** A textual merge conflict happens precisely when two patches' changed regions overlap, so this check is
just a dry-run of the merge. It does show what an agent team actually needs here: notice early that someone else
is editing the same region of the same file.

## What this means for the product
- On these benchmarks, every observed breakage between agents editing different functions was a **textual
  collision at a shared insertion point**. These are imports, alias lists, and the space between definitions.
  None was a semantic interaction carried through calls, shared state, shared tests or past co-changes.
- **What worked in the live CooperBench test (cb1b) was ownership:** atomic claims and a hand-off. Extending
  claims from functions to file regions (imports, module-level blocks, insertion points), plus a merge dry-run
  before hand-off, targets the failure mode we actually observed. **That is a design implication, not a tested
  result.**
- **Multi-hop paths, coverage overlap and co-change stay in the product only as context,** labelled "possible
  overlap", with no accuracy claim.

## Limitations
- One model's patches (Claude k1).
- 57 heavy disjoint pairs untested at the cut-off. The order was seeded and prioritized by an outcome-blind
  structural rule.
- Heavy-repo coverage failed on environment installs.
- The pooled light rows were already-published v2 results.
- Static call resolution.
- The primary subset is defined by function disjointness, which counts pairs that edit no function at all.
