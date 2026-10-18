"""EXPLORATORY (not pre-registered), run after the one-time v3/v4 scoring, on the same primary rows.

1. How the breakages happened (merge conflict vs tests failing after a clean merge), by whether both patches edit
   at least one function.
2. The case-study hypothesis (results/detector_breakage_cases.md): do the two patches touch a common line, or lines
   within 3 of each other, in the same file? AUC as a binary score. Hypothesis-generating only; it would need its
   own pre-registered test on new data.

  python verify/detector34_exploratory.py > results/detector34_exploratory.txt
"""
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "coop"))
from detector_eval import CACHE, changed_lines  # noqa: E402
from score_detector2 import auc, boot_ci  # noqa: E402

rows = json.loads((ROOT / "results" / "detector4_rows.json").read_text(encoding="utf-8"))
print(f"primary rows: {len(rows)}, failures: {sum(r['interaction_failure'] for r in rows)}")
print("\n1. failure type by whether both patches edit >= 1 function:")
for both in (True, False):
    sub = [r for r in rows if all(r["funcs"]) == both]
    kinds = Counter(r["merge_status"] if r["interaction_failure"] else "no failure" for r in sub)
    print(f"  both edit functions = {both}: n = {len(sub)}, failures = {sum(r['interaction_failure'] for r in sub)}, {dict(kinds)}")


def lines(r, k):
    p = CACHE / r["repo"] / f"task{r['task']}" / f"feature{r['pair'][0]}_feature{r['pair'][1]}" / \
        f"patch_claude_k1_feature{r['pair'][k]}.patch"
    return changed_lines(p.read_bytes().decode("utf-8", errors="replace").replace("\r\n", "\n"))


print("\n2. line-level overlap (EXPLORATORY, hypothesis from 4 case studies):")
y = [r["interaction_failure"] for r in rows]
for gap in (0, 3):
    score = []
    for r in rows:
        a, b = lines(r, 0), lines(r, 1)
        score.append(float(any(abs(x - z) <= gap for f in a.keys() & b.keys() for x in a[f] for z in b[f])))
    tp, fl = sum(s and t for s, t in zip(score, y)), int(sum(score))
    lo, hi = boot_ci(score, y)
    print(f"  changed lines within {gap} of each other in a shared file: AUC {auc(score, y):.3f} "
          f"[95% CI {lo:.3f}-{hi:.3f}], precision {tp}/{fl}, recall {tp}/{sum(y)}")
