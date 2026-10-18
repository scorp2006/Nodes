# Proofs

Every number in the main README comes from a file in this folder. Each confirmatory result has a pre-registration,
committed before its run, and was analysed once. Scripts are in [`verify/`](../verify), [`pilot/`](../pilot) and
[`coop/`](../coop).

## Headline results

| claim | pre-registration | result | independent recompute |
|---|---|---|---|
| **HiddenBench**, Haiku 5.5, 40 held-out tasks: board 15/40 (38%) vs placebo 5/40, baseline 6/40, nudge 6/40, unchecked board 5/40; primary p = 0.006 | [preregistration_test1.md](preregistration_test1.md) | [test1_primary.txt](test1_primary.txt) | [hiddenbench_recompute.txt](verify/hiddenbench_recompute.txt), [erratum_holm.md](verify/erratum_holm.md) |
| **CooperBench**, Sonnet 5.5, 45 pairs: clean merges 35/45 vs 22/45 (protocol) and 19/45 (coop); Holm p = 0.002 | [preregistration_cb1b.md](preregistration_cb1b.md) | [cb1b_primary.txt](cb1b_primary.txt), [cb1b_report.md](cb1b_report.md) | [cb1b_breakdown.txt](verify/cb1b_breakdown.txt) |

Supporting checks for HiddenBench:
- Leakage audit, 135 boards and 2,490 transcript combinations, 0 leaks: [leakage_audit.txt](verify/leakage_audit.txt).
- Cost per correct decision: [cost_table_test1.txt](verify/cost_table_test1.txt).
- Share of the hidden-profile gap closed: [gap_closed_reference.txt](verify/gap_closed_reference.txt).
- Five case studies, three wins and two losses: [case_studies.md](verify/case_studies.md).

## The product is the research

| claim | proof |
|---|---|
| 4,916/4,916 recorded tool calls give identical replies in the product, 0 leaks | [replay_equivalence.txt](repro/replay_equivalence.txt), [replay_equivalence.md](repro/replay_equivalence.md) |
| Live regression on the product (dev data): PASS | [preregistration_stage2.md](repro/preregistration_stage2.md), [stage2_report.md](repro/stage2_report.md) |
| 0 double-claims at 10, 50 and 100 concurrent agents | [scale_test_nodes.txt](verify/scale_test_nodes.txt), [hardening.md](verify/hardening.md) |
| Conflict query p50 2–4 ms on the CPython stdlib (15,713 nodes) | [scale_test.txt](verify/scale_test.txt) |
| All four gate verdicts, with their graph paths | [gate_decision_paths.txt](v1/gate_decision_paths.txt) |

## Other pre-registered results (reported as they came out)

| test | outcome | files |
|---|---|---|
| HiddenBench replication with Sonnet 5.5, 20 tasks, board vs placebo | **not significant**: better on 3, worse on 1, p = 0.625 | [preregistration_hb_sonnet.md](preregistration_hb_sonnet.md), [hb_sonnet_report.md](hb_sonnet_report.md), [hb_sonnet_replication.txt](hb_sonnet_replication.txt) |
| Does multi-hop call distance predict merge failures beyond "same function"? | **no**: AUC 0.40–0.50 on the primary subset. This is why multi-hop impact is advisory. | [preregistration_detector2.md](preregistration_detector2.md), [detector2_report.md](detector2_report.md), [detector34_report.md](detector34_report.md) |
| CooperBench plan `cb1` (Haiku 5.5) | never run: amended to `cb1b` before any test pair, because Haiku failed its own feature tests on the dev pairs | [preregistration_cb1.md](preregistration_cb1.md), [preregistration_cb1b.md](preregistration_cb1b.md) |

## Development and exploratory (no claims rest on these)

- Dev split, 20 tasks: [dev1_summary.txt](dev1_summary.txt).
- All 43 tasks, non-confirmatory: [test1_all43_nonconfirmatory.txt](test1_all43_nonconfirmatory.txt).
- Native tools check: [native_check.md](v1/native_check.md).
- Edge coverage: [edge_coverage_F.json](v1/edge_coverage_F.json).
- Live BlindSpot run with 3 subagents: [live_blindspot_parallel.txt](v1/live_blindspot_parallel.txt).
- Sprint report: [sprint_report.md](v1/sprint_report.md).
- Screenshots: [v1/screenshots](v1/screenshots).

## Raw transcripts

The per-run transcripts and logs behind these files (`runs/`, 173 MB) are not in git. The recompute scripts read
them, and every summary above was produced from them.
