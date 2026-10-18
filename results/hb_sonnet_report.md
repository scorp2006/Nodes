# REPLICATION: HiddenBench with Claude Sonnet (pre-registered, analyzed once)

Pre-registration: `results/preregistration_hb_sonnet.md` (a952d98).
- 20 of the 40 confirmatory tasks, drawn with a fixed seed.
- Board vs placebo, `claude-sonnet-5-5`, `--effort low`; everything else as in the Haiku test.
- Output: `results/hb_sonnet_replication.txt` (`pilot/summarize.py`).
- Independent recompute: `results/verify/hb_sonnet_recompute.txt`. It matches.
- Cost: `results/verify/cost_table_hb_sonnet.txt`.

The batch stopped once at a usage limit (11 runs). Per the pre-registered failure rule, those runs were rerun
after the cooldown with the exact same commands. All 40 runs completed; no task was dropped.

| condition | group right | 95% CI | agents right, before → after | output tokens / run | list $ per correct decision |
|---|---|---|---|---|---|
| placebo | 14/20 (70%) | 48–85% | 17% → 70% | 7,966 | 2.20 |
| **board** | **16/20 (80%)** | 58–92% | 28% → 80% | 16,620 | 2.64 |

**Primary (exact two-sided sign test): board better on 3 tasks, worse on 1, p = 0.625. Not significant.**

## Reading
- **Same direction as Haiku, but the gap is much smaller.**
  - Haiku (40 tasks): board 15/40 vs placebo 5/40, p = 0.0063.
  - Sonnet: placebo alone already gets 70% of groups right, so there is less left for the board to fix.
  - The pre-registration warned that 20 tasks give a directional result. 4 discordant tasks is fewer than the
    ~6 expected if the Haiku split had held.
- **We do not claim the board helps Sonnet.** This replication neither confirms nor contradicts the Haiku
  result. One reading, untested: the board matters most when the model doesn't share facts well on its own.
- **Cost:** with Sonnet the board costs more per correct decision ($2.64 vs $2.20 at list price), the reverse of
  Haiku ($0.32 vs $0.80). It roughly doubles output tokens, and the accuracy gain is small.
