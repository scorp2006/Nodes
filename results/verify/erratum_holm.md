# Erratum: Holm-adjusted p in results/test1_primary.txt

The Holm block at the end of `results/test1_primary.txt` (on `main`) was not produced by a committed script.
It was computed from p-values already rounded to 3 decimals, so it lists graph vs graph_unverified as 0.018.
The exact value is 3 × 26/4096 = **0.0190**, from `verify/recompute_hiddenbench.py`
(`results/verify/hiddenbench_recompute.txt`). Graph vs nudge has the same issue: 0.024 is listed, the exact value is 2 × 24/2048 = 0.0234.
Graph vs baseline (0.016 vs exact 0.0156) and graph_unverified vs placebo (1.000) agree. Every secondary comparison that survived still survives, and the primary result
(p = 0.0063) is unaffected. Cite the recompute for adjusted p-values.
