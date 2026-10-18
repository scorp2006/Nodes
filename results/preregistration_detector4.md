# Pre-registration: detector v4 (co-change and coverage signals)

Committed **before any detector v3 score has been viewed** and before v4 signals were computed for any test task.
No model calls are involved at any step.

## Question
On pairs whose patches edit **different** functions, does either new signal, or a fixed combination, predict
interaction failures better than "same function edited"?
- **co-change:** these functions were changed together in the repo's past.
- **coverage overlap:** the same tests exercise both edits.

## Rows and ground truth (reused, no new patch tests)
Exactly the rows of the v3 primary analysis:
- the v2 light disjoint eligible rows (`results/detector2_rows.json`);
- the v3 heavy disjoint eligible rows finished by the v3 cut-off (`results/detector3_rows.json`, written by
  `coop/detector3.py score`).

Ground truth is the same as v3 (`coop/gt_interaction.py`). The caveat in v3 amendment 2 applies: the light rows'
outcomes were already published in v2.

## Signals (code frozen in this commit: `coop/signals_v4.py`, `coop/score_v4.py`)
Built and checked only on the dev tasks click 2068 and 2800. These are excluded from every test analysis, and no
test task was mined or covered before this commit.

- **Co-change:** `(:Function)-[:CO_CHANGES {support, confidence, resolved_by: 'git-history', window}]-(:Function)`.
  - Source: a full bare clone of the upstream repo, using only history at or before the task's base commit
    (`git log <base>`).
  - At most 2,000 non-merge commits touching the package directory.
  - Commits changing more than 30 package `.py` files, or more than 50 functions, are skipped as bulk changes.
  - A changed line maps to its innermost function in that commit's version of the file.
  - An edge needs support ≥ 2. Confidence = support / min(changes of a, changes of b).
- **Coverage:** `(:Test)-[:EXERCISES {resolved_by: 'coverage', confidence: 1.0}]->(:Function)`.
  - One run, in the task's own Docker image at its base commit, of the task's target test files (union of
    `feature*/tests.patch` paths) **as they exist at base**. Feature tests are not applied.
  - `coverage` with `dynamic_context = test_function`, measuring the package source only.
  - Lines map to their innermost function at base.
  - Tasks where coverage can't be produced (image or install failure, or no target file exists at base) get no
    EXERCISES edges and are reported.
- **Resources:** containers capped at 2 CPUs, low CPU shares and 4 GB, one at a time. Host work runs at
  below-normal priority.

## Predictors (weights and flags fixed now; nothing is fitted)
For a pair with edited-function sets A and B:

| predictor | score | flag |
|---|---|---|
| `same_fn` (baseline) | 1 if A ∩ B is non-empty | score = 1 |
| `cov` | Jaccard of the test sets exercising any function in A and any in B (0 if neither has a test) | cov > 0 |
| `cc` | the largest CO_CHANGES confidence between any a in A and b in B (0 if none) | cc ≥ 0.1 |
| `risk` | same_fn + 0.5 × cov + 0.5 × cc | risk ≥ 0.25 |

## Analysis (run once, together with v3: `coop/detector3.py score`, then `coop/score_v4.py`)
- **Primary:** the pooled eligible disjoint pairs (the v3 primary rows).
  - For each predictor: AUC, flag precision and recall.
  - **Primary comparison:** each new signal (cov, cc, risk) vs same_fn, as the AUC difference with a 95%
    paired-bootstrap CI (2000 resamples, seed 20261023).
  - On this subset same_fn = 0 by construction (AUC 0.5). The comparison therefore asks whether the new signal
    beats chance where same-function overlap can't help.
- **Secondary:**
  - (a) the same, limited to tasks with coverage;
  - (b) the same, limited to pairs where both patches edit at least one function.
- **Too few failures:** with fewer than 10 interaction failures in a subset, its numbers are descriptive only, and
  the report says so plainly.

## Amendment 1 (committed before any v3 or v4 score was viewed)
Signal runs for the test tasks (`runs/v4_signals.log`, raw outputs under `results/v4_signals/cov_raw/`, not
committed) showed environment failures in heavy-repo coverage:
- **dspy:** coverage was not installed in the run environment.
- **datasets:** pytest collection failed (the installed pyarrow is newer than the repo supports).
- **outlines 1655 and 1706:** pytest collection failed (a requests dependency).

These runs produced a coverage file with **no test mapped**. They now count as "coverage unavailable", as the
pre-registration intended for failed runs, so they are excluded from secondary (a). The fix is a one-line change
in `coop/score_v4.py`. Nothing else changed, and the failed installs were not retried (that would be
environment tuning on test tasks). Coverage is available for click, jinja, dirty-equals and outlines 1371.
