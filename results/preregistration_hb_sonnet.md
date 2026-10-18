# Pre-registration: HiddenBench replication with Claude Sonnet (hb_sonnet)

Committed before any run of this batch. It asks one question: does the source-checked board's advantage over
placebo, found with Haiku (`results/preregistration_test1.md`, graph 15/40 vs placebo 5/40, p = 0.0063), also
appear with a stronger model?

## Design
- **Tasks:** 20 of the 40 confirmatory test tasks, drawn with `random.Random(20261024).sample(pool, 20)`.
  The pool is the 40 tasks that were complete in test1 (tasks 1–3 excluded as in test1).
  The draw: 6, 9, 10, 11, 17, 18, 19, 24, 27, 28, 40, 42, 44, 46, 50, 54, 57, 60, 61, 63.
  No task was chosen by looking at its results, and nothing was tuned on these tasks.
- **Conditions:** `graph` (source-checked board) and `placebo` only. The code is identical to test1, apart from
  one change: the new optional `--effort` flag in `pilot/run_hiddenbench.py` / `pilot/claude_client.py`.
- **Model:** `claude-sonnet-5-5` through `claude -p`, `--effort low`, the same for both conditions. Low effort keeps
  token use small; it is not tuned.
- Everything else is as in test1: hidden profile, 15 rounds, HiddenBench default prompts and voting, base seed
  2026 with HiddenBench's per-task seeds, one run per task per condition, `--no-peek`.

Commands (2 workers per condition, to stay inside usage limits):
```
python pilot/run_hiddenbench.py --condition graph   --split test --task-ids <the 20> --model claude-sonnet-5-5 --effort low --out runs/hb_sonnet --workers 2 --no-peek
python pilot/run_hiddenbench.py --condition placebo --split test --task-ids <the 20> --model claude-sonnet-5-5 --effort low --out runs/hb_sonnet --workers 2 --no-peek
```

## Outcome and analysis (run once, after all 40 runs finish)
- **Primary:** group right (strict majority of final votes correct), graph vs placebo, paired by task, exact
  two-sided sign test. Reported whichever way it comes out.
- **Descriptive only:** individual accuracy before and after discussion; tokens, list-price cost and wall time
  per run.
- Analysis: `python pilot/summarize.py runs/hb_sonnet` (graph vs placebo), with an independent recompute by
  `verify/recompute_hiddenbench.py`'s method.

## Power
With 20 tasks this is a directional replication, not a precise estimate. If the Haiku discordant split
(11 better, 1 worse out of 40) held, about 6 discordant pairs would be expected here. At 5 to 1, the exact p is
0.22. **A non-significant result would therefore not contradict test1.** The effect size (discordant counts)
is the main thing to read.

## Failure rule
- **Infrastructure failures** (every call of a run failed, or a usage-limit error):
  - wait for the limit's cooldown to clear;
  - rerun that run, at most 2 times;
  - if it still fails, drop that task from both conditions.
- **Usage limits:**
  - check run logs for limit errors before every relaunch;
  - never retry in a loop while limited;
  - if limits make finishing impossible, report the partial count and do not analyze.
