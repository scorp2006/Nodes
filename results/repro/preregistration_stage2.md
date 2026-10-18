# Pre-registration: reproduction Stage 2, live regression on the product (dev data only)

Committed before any Stage 2 run. Purpose (REPRODUCTION-PLAN.md): show the product reproduces the harness's live
behavior. Stage 1 (`results/repro/replay_equivalence.md`) covers the code path; this stage covers the full loop with real
models. **Dev tasks only.** No test task is touched; they were used for confirmatory results.

## What runs
The product server runs with `--profile harness-board` (HiddenBench) or `--profile harness-coop` (CooperBench). These
profiles keep the harness's tool names, descriptions and reply texts, implemented on the product's code path:
- Session identity is a bearer token.
- Facts are ASSERTED and SOURCED_FROM a briefing Source.
- Claims are leases.
- Calls are traced.

Each run gets its own fresh workspace graph on the product's FalkorDB (:6380).

| | HiddenBench | CooperBench |
|---|---|---|
| tasks | the 20 dev tasks (`pilot/splits.json` dev): 4, 8, 12, 22, 29, 31, 32, 33, 34, 36, 38, 43, 49, 51, 52, 55, 56, 58, 62, 65 | dev pairs click 2068 f1+f2 and dspy 8394 f1+f2 |
| condition | graph only | graph only |
| model | `claude-haiku-5-5`, CLI default effort (as dev1 / test1) | `claude-sonnet-5-5`, `--effort low` (as cb1b) |
| settings | hidden profile, 15 rounds, base seed 2026, 1 run per task (as dev1) | `--setting coop --git`, the cb1b launcher and prompts |
| baseline | `runs/dev1/graph`: 13/20 group right, 95% CI 43–82% (`results/dev1_summary.txt`) | dev3-click-graph and dev4-dspy-graph: merge status `identical` |

## Acceptance (decided now; reported either way)
1. **HiddenBench accuracy:** product group-right count within the original 95% CI, i.e. **9 to 16 of 20**.
2. **HiddenBench tool use:** mean tool calls per run within 0.5× to 1.5× of the original. The original means are
   44.1 calls in all, with 10.2 `share_fact`, 21.6 `shared_facts` and 12.3 `who_has_not_shared`.
3. **CooperBench:** both dev pairs reach merge success (`identical` or `clean`), the same outcome as the original
   dev runs. The pass rate is reported but not required; the original dev runs failed tests on one feature.
4. **Infrastructure:** a run that fails for infrastructure reasons (usage limit, Docker) is rerun after the cause is
   fixed, at most 2 times. Its count is reported.

## Caveats stated in advance
- **The model differs for CooperBench.** The original dev runs used Haiku, and this stage uses Sonnet at low effort
  to match cb1b (the claim being reproduced). The merge-success comparison is therefore across models.
- **HiddenBench runs are single and stochastic.** A result inside the CI shows consistency, not identity.
- **The harness tool server differs.** The original HiddenBench dev run started a new stdio tool server for every
  agent call. Here one long-lived HTTP server answers. Stage 1 shows the replies are identical.
