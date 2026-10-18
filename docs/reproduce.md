# Reproduce

Every result has a pre-registration committed before its run, a script, and a committed output.

| result | pre-registration | run | output |
|---|---|---|---|
| HiddenBench, Haiku 5.5, 40 held-out tasks | [preregistration_test1.md](../results/preregistration_test1.md) | `python pilot/run_hiddenbench.py --split test ...` | [test1_primary.txt](../results/test1_primary.txt) |
| HiddenBench recompute (no LLM) | — | `python verify/recompute_hiddenbench.py` | [hiddenbench_recompute.txt](../results/verify/hiddenbench_recompute.txt) |
| CooperBench, Sonnet 5.5, 45 pairs | [preregistration_cb1b.md](../results/preregistration_cb1b.md) | `coop/run_many.ps1` | [cb1b_primary.txt](../results/cb1b_primary.txt), [cb1b_report.md](../results/cb1b_report.md) |
| CooperBench breakdown (no LLM) | — | `python verify/cb1b_breakdown.py` | [cb1b_breakdown.txt](../results/verify/cb1b_breakdown.txt) |
| Multi-hop predictor test | [preregistration_detector2.md](../results/preregistration_detector2.md) | `python verify/detector34_exploratory.py` | [detector2_report.md](../results/detector2_report.md) |
| Product = research replay (no LLM) | [preregistration_stage2.md](../results/repro/preregistration_stage2.md) | `python verify/replay_equivalence.py` | [replay_equivalence.txt](../results/repro/replay_equivalence.txt) |
| Scale, 10–100 agents (no LLM) | — | `python verify/scale_test.py` | [scale_test_nodes.txt](../results/verify/scale_test_nodes.txt), [scale_test.txt](../results/verify/scale_test.txt) |
| Gate decisions, all four verdicts (no LLM) | — | `python demo/gate_demo.py` | [gate_decision_paths.txt](../results/v1/gate_decision_paths.txt) |

The no-LLM checks need only Docker and Python:

```bash
docker run -d -p 6379:6379 falkordb/falkordb:v4.22.1      # harness DB (the replay compares it with :6380)
python -m nodes up                                         # product DB on :6380 + server
python verify/replay_equivalence.py                        # 4,916/4,916 identical, 0 leaks
python verify/scale_test.py
```

The LLM runs need Claude Code signed in, and the benchmarks cloned into `external/` (HiddenBench, CooperBench;
both MIT, not redistributed here). CI runs the tests and the replay on every push
([.github/workflows/ci.yml](../.github/workflows/ci.yml)).
