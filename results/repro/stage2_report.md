# Reproduction Stage 2: live regression on the product (dev data). PASS

Pre-registration: `results/repro/preregistration_stage2.md` (e948d60). Each part was analyzed once.
- HiddenBench: `verify/stage2_hiddenbench.py` → `results/repro/stage2_hiddenbench.txt`.
- CooperBench: eval files under `runs/cooperbench/repro2-*`.

## HiddenBench: 20 dev tasks, graph condition, through the product (`--profile harness-board`)
| check | original dev run (`runs/dev1`) | live product run (`runs/repro_dev1`) | bar | result |
|---|---|---|---|---|
| model / rounds / seed / profile / split | claude-haiku-5-5 / 15 / 2026 / hidden / dev | identical | identical | same |
| groups right | 13/20 | **13/20** | 9–16 | **PASS** |
| `share_fact` calls / source-checked / unverified | 205 / 205 / 0 | 306 / 306 / 0 | — | reported |
| product's own verdicts vs the harness rule | — | 306/306 agree | — | **PASS** |
| board posts | 128 | 171 | — | reported |
| leakage (unposted briefing fact in a board or who reply) | 0 (test1 audit) | **0 in 549 replies** | 0 | **PASS** |

Tool calls per run, mean (bar: live within 0.5×–1.5× of the original):

| tool | original | live | ratio | result |
|---|---|---|---|---|
| `share_fact` (posting a fact) | 10.2 | 15.3 | 1.49 | PASS |
| `shared_facts` | 21.6 | 18.4 | 0.85 | PASS |
| `who_has_not_shared` | 12.3 | 9.1 | 0.74 | PASS |
| all | 44.1 | 42.8 | 0.97 | PASS |

Per-task counts are in the output file. Individual tasks vary a lot: single stochastic runs.

## CooperBench: 2 dev pairs, graph condition, through the product (`--profile harness-coop`)
| pair | original dev outcome | live product | bar (merge success) |
|---|---|---|---|
| click 2068 f1+f2 | identical (Haiku) | **clean**; both features pass: no | **PASS** |
| dspy 8394 f1+f2 | identical (Haiku) | **identical**; both features pass: **yes** | **PASS** |

Every agent used the product's tools through the whole protocol. Per pair: declare_plan ×2, claim ×2,
mark_committed ×2, coordination_status ×3 (from the product traces).

**Infrastructure failure, rerun under the pre-registered rule.** The first attempt passed the model as
`anthropic/claude-sonnet-5-5`. Claude Code rejected it (404 model_not_found, $0 spent), so both agents errored at
step 1. cb1b used `claude-sonnet-5-5`. Both pairs were rerun with the correct string, as `repro2-*`. The failed
attempt is kept in `runs/cooperbench/repro-click-2068-graph`.

## Caveats (stated in the pre-registration)
- **CooperBench model:** Sonnet at low effort, matching cb1b, vs Haiku in the original dev runs.
- **Single stochastic runs:** inside the CI means consistent, not identical.
- **Tool server:** one long-lived HTTP server instead of a stdio server per call. Stage 1 shows the replies are
  identical.
- **Analysis script bug fixed after the first execution:** the trace file name used `t12_d0` instead of the
  workspace's `t12-d0`. It crashed before the leakage and tool-use sections. The accuracy and verdict numbers printed
  before the crash were unchanged by the fix.
