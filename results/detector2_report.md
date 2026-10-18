# Detector v2: result (pre-registered, scored once)

Pre-registration: `results/preregistration_detector2.md` (commit 59fc8f5). Graph v2 was frozen at c72f338 and
the ground truth committed at c01cb3e, both before scoring. Scorer: `coop/score_detector2.py`, output in
`detector2_scores.txt`, per-pair rows in `detector2_rows.json`. No model calls were involved.

## Data
- 199 held-out pairs. 84 are eligible (each patch passes its own feature's tests alone), and 45 of those are
  interaction failures.
- Evaluator errors: 0. Edited functions missing from the graph: 0.

## Primary: eligible pairs whose edited functions are disjoint (n = 34, 4 interaction failures)
| predictor | AUC [95% bootstrap CI] | flag precision | flag recall |
|---|---|---|---|
| same file | 0.500 [0.500–0.500] | 0.12 (4/34) | 1.00 (4/4) |
| CALLS distance (≤ 3 hops) | 0.417 [0.345–0.482] | 0.00 (0/5) | 0.00 (0/4) |
| CALLS + SHARES_STATE distance | 0.400 [0.152–0.667] | 0.10 (2/21) | 0.50 (2/4) |

**No predictor beats chance on the primary analysis.** Every pair edits the same file, so "same file" can't
discriminate at all. The call-graph predictors land at or below 0.5. With only 4 positives the CIs are wide, so
this is a null result, not proof that no signal exists. Of the 4 failures between disjoint functions, none has
any call path between the two agents' functions. Two are within 2 hops once shared-state edges are added.

## Secondary: all eligible pairs (n = 84, 45 failures)
| predictor | AUC [95% CI] | precision | recall |
|---|---|---|---|
| same file | 0.500 | 0.54 (45/84) | 1.00 |
| CALLS distance | 0.834 [0.752–0.911] | 0.75 (41/55) | 0.91 |
| CALLS + SHARES_STATE distance | 0.833 [0.746–0.913] | 0.61 (43/71) | 0.96 |

## Exploratory (not pre-registered, `detector2_exploratory.txt`)
"Both agents edited the same function" alone gives AUC 0.840 [0.760–0.916] on the same 84 pairs. Failures by
call distance:
- distance 0 (same function): 41/50
- distance 1–3: 0/5
- no path: 4/29

**The secondary AUC of 0.83 therefore comes from same-function overlap. Multi-hop paths add nothing here.**

## What this means for the product
- The coordination value we measured comes from **claims on the same function**: ownership plus a hand-off.
  That is what the CooperBench batch tests.
- Multi-hop conflict paths, at 1–3 calls, did not predict merge or test failures on these tasks. The web view
  and `status` still show them, labelled as possible overlaps, but **we make no accuracy claim for them**.
- Limitations:
  - One model's published patches (Claude k1).
  - 5 light tasks.
  - 4 positives in the primary subset.
  - Static call resolution (36–37% of call sites resolved on dev).
