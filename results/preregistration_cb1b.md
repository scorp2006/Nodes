# Pre-registration amendment: CooperBench batch `cb1b` (replaces `cb1`, which was never run)

Committed **before** any test-pair run. `cb1` (results/preregistration_cb1.md) was not launched. Every rule in
`cb1` still holds unless changed below.

## Why it was amended
1. **Model:** on both dev pairs, Haiku 5.5 failed one feature's own tests in every condition (a coding floor). The
   pass metric therefore could not separate the conditions. We switch to Sonnet 5.5 so coding is not the bottleneck.
2. **Time:** the full 40-pair plan (6–11 h, dominated by heavy repos at up to 13.6 GB per container) didn't fit the
   time available. We sample only light repos, which can run in parallel.
3. **Metrics:** the dev runs showed that merge success (whether the two submissions combine at all) is what
   coordination changes, so it becomes co-primary with pass.

## Frozen code
- Branch `next`, commit `595bf2fbb3335a3d882e76d4e41a007b644b0f61`. CooperBench `63b9d44`, files unmodified (same
  runtime changes as `cb1`, plus one: every agent in every condition gets the same `--effort low`).
- Smoke-tested on dev pair click 2068 (coop): Sonnet 5.5 ran with `--effort low`, and Claude Code 2.1.302 in the
  containers (npm `latest` at run time, recorded per run; not pinned). No test pair was run.

## Changes from `cb1`
| | `cb1` | `cb1b` |
|---|---|---|
| Model | Haiku 5.5 | **Sonnet 5.5** (`anthropic/claude-sonnet-5-5`), every condition |
| Thinking | default | **minimum: `--effort low`**, identical for every agent in every condition (Sonnet 5.5 can't disable thinking) |
| Tool server | long-lived | long-lived (unchanged) |
| Pairs | 40 across 14 test tasks | **45 pairs from the 5 light test tasks only** (click 2956, jinja 1465 / 1559 / 1621, dirty-equals 43): 9 random pairs per task, `random.Random(20261021)`, in `coop/test_sample_light45.json` |
| Order | per-pair shuffle, seed 20261020 | per-pair shuffle, seed 20261022 (`coop/plans/test1b.csv`), 6 runs in parallel |
| Conditions | coop / protocol / graph | unchanged |
| Metrics | pass primary; coordinated secondary | **co-primary: merge success** (`identical` or `clean`) **and pass** (`both_passed`) |
| Primary | graph vs protocol on pass | **graph vs protocol on merge success and on pass**, Holm-Bonferroni across these 2 |
| Secondary | 3 tests | **graph vs coop on merge success and on pass**, Holm-Bonferroni across these 2 |

Unchanged: exact two-sided paired sign test, p < 0.05; the infrastructure-failure rule and drop-from-all-conditions
(up to 2 relaunches); identical procedure text in protocol and graph; no scoring hints; one launcher for every run.

## Blind
`--no-auto-eval` on every run. Progress is checked only by counting `<name>.wall.txt`. After every run and
relaunch is done, evaluate once with `coop/eval_all.ps1 -Plan coop/plans/test1b.csv`, then analyze once:
```
python coop/analyze_cb.py coop/plans/test1b.csv runs/cooperbench
```

## Sample size
45 pairs × 3 conditions = 135 runs, sized to finish in about 3 hours at 6 in parallel. The Sonnet smoke run took
81 s wall-clock (coop); tool conditions took 2–3× longer in dev. Sized before any test run; not adjusted afterwards.

## Limitations, stated in advance
- **Light repos only** (click, jinja, dirty-equals: 3 of the 7 Python repos, 5 of the 14 test tasks). The results
  may not transfer to the heavy repos (dspy, outlines, datasets, llama-index).
- One run per pair per condition; Claude Code version in the containers is not pinned.
- Dev observation (2 pairs, Haiku): protocol and graph both reached merge success where coop didn't. We expect
  both procedure conditions to beat coop on merge success, and graph vs protocol may be small. Any null result is
  reported as such.
