"""Independent recomputation of the HiddenBench test1 result. Shares no code with pilot/summarize.py.

Reads the raw run files, recomputes group correctness (strict majority of final votes), applies the pre-registered
exclusions (tasks 1-3) and the drop-from-all rule, and recomputes every paired sign test from scratch with exact
binomial tail sums written with fractions. Holm-Bonferroni is recomputed too.

  python verify/recompute_hiddenbench.py <path to runs/test1>
"""
import json
import sys
from fractions import Fraction
from math import comb
from pathlib import Path

CONDITIONS = ["baseline", "nudge", "placebo", "graph_unverified", "graph"]
EXCLUDED = {"t1_d0", "t2_d0", "t3_d0"}
PAIRS = [("graph", "placebo", "primary"), ("graph", "baseline", "secondary"), ("graph", "nudge", "secondary"),
         ("graph", "graph_unverified", "secondary"), ("graph_unverified", "placebo", "secondary")]


def group_right(run: dict) -> bool:
    votes = [v["vote"] for v in run["final_votes"]]
    return 2 * sum(v == run["correct_answer"] for v in votes) > len(votes)


def exact_sign_p(wins: int, losses: int) -> Fraction:
    n = wins + losses
    if n == 0:
        return Fraction(1)
    tail = sum(comb(n, k) for k in range(0, min(wins, losses) + 1))
    return min(Fraction(1), Fraction(2 * tail, 2 ** n))


def main(root: Path) -> None:
    data = {}
    for c in CONDITIONS:
        data[c] = {f.stem: json.loads(f.read_text(encoding="utf-8")) for f in (root / c).glob("t*_d0.json")}
    common = set.intersection(*(set(d) for d in data.values())) - EXCLUDED
    print(f"tasks valid in all 5 conditions after excluding t1-t3: {len(common)}")
    missing = sorted(set.union(*(set(d) for d in data.values())) - set.intersection(*(set(d) for d in data.values())))
    print(f"tasks missing in some condition (dropped): {missing or 'none'}")
    right = {c: {t: group_right(data[c][t]) for t in common} for c in CONDITIONS}
    for c in CONDITIONS:
        print(f"  {c:17} {sum(right[c].values()):2}/{len(common)}")
    raw = {}
    for a, b, kind in PAIRS:
        w = sum(right[a][t] and not right[b][t] for t in common)
        l = sum(right[b][t] and not right[a][t] for t in common)
        raw[(a, b)] = exact_sign_p(w, l)
        print(f"  [{kind:9}] {a} vs {b}: better {w}, worse {l}, exact p = {float(raw[(a, b)]):.4f}")
    sec = [(a, b) for a, b, k in PAIRS if k == "secondary"]
    order = sorted(sec, key=lambda k: raw[k])
    running, adj = Fraction(0), {}
    for rank, k in enumerate(order):
        running = max(running, min(Fraction(1), (len(sec) - rank) * raw[k]))
        adj[k] = running
    print("Holm-adjusted secondaries:")
    for k in sec:
        print(f"  {k[0]} vs {k[1]}: {float(adj[k]):.4f}")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
