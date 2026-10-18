"""cb1b checks after the single pre-registered analysis. EXPLORATORY except the recompute.

1. Independent recompute of the four pre-registered sign tests straight from eval.json (no analyze_cb code).
2. Merge status per condition (identical / clean / conflicts / ...), and how many "identical" merges are empty
   (both agents submitted nothing), which would make "coordinated" trivially true.
3. Per-feature pass counts, and pass rate among coordinated vs uncoordinated runs.

  python verify/cb1b_breakdown.py coop/plans/test1b.csv runs/cooperbench
"""
import csv
import json
import sys
from collections import Counter, defaultdict
from fractions import Fraction
from math import comb
from pathlib import Path


def p_sign(w, l):
    n = w + l
    return 1.0 if n == 0 else float(min(Fraction(1), Fraction(2 * sum(comb(n, k) for k in range(min(w, l) + 1)), 2 ** n)))


def main(plan: Path, logs: Path):
    ev = defaultdict(dict)
    for row in csv.DictReader(open(plan, encoding="utf-8")):
        f = next((logs / row["Name"]).rglob("eval.json"))
        ev[(row["Repo"], row["Task"], row["Features"])][row["Condition"]] = json.loads(f.read_text(encoding="utf-8"))
    pairs = sorted(ev)
    ok = lambda e: e["merge"].get("status") in ("identical", "clean")  # noqa: E731
    print(f"pairs: {len(pairs)}")
    print("\n1. recompute of the pre-registered tests (raw p; Holm within each family of 2):")
    for fam in (("protocol",), ("coop",)):
        b = fam[0]
        res = []
        for metric, fn in (("coordinated", ok), ("pass", lambda e: bool(e.get("both_passed")))):
            w = sum(fn(ev[p]["graph"]) and not fn(ev[p][b]) for p in pairs)
            l = sum(fn(ev[p][b]) and not fn(ev[p]["graph"]) for p in pairs)
            res.append((metric, w, l, p_sign(w, l)))
        ps = sorted(r[3] for r in res)
        holm = {ps[0]: min(1, 2 * ps[0]), ps[1]: max(min(1, 2 * ps[0]), ps[1])}
        for metric, w, l, p in res:
            print(f"  graph vs {b} on {metric}: better {w}, worse {l}, raw p = {p:.4f}, Holm p = {holm[p]:.4f}")
    print("\n2. merge status per condition (EXPLORATORY):")
    for c in ("coop", "protocol", "graph"):
        st = Counter(ev[p][c]["merge"].get("status") for p in pairs)
        empty = sum(ev[p][c]["merge"].get("status") == "identical" and not (ev[p][c]["merge"].get("diff") or "").strip()
                    for p in pairs)
        print(f"  {c:9} {dict(st)}   identical-but-empty: {empty}")
    print("\n3. per-feature pass, and pass by coordination (EXPLORATORY):")
    for c in ("coop", "protocol", "graph"):
        f1 = sum(bool(ev[p][c].get("feature1", {}).get("passed")) for p in pairs)
        f2 = sum(bool(ev[p][c].get("feature2", {}).get("passed")) for p in pairs)
        co = [ev[p][c] for p in pairs if ok(ev[p][c])]
        un = [ev[p][c] for p in pairs if not ok(ev[p][c])]
        both = lambda xs: sum(bool(e.get("both_passed")) for e in xs)  # noqa: E731
        print(f"  {c:9} feature1 passed {f1}/{len(pairs)}, feature2 passed {f2}/{len(pairs)};  "
              f"both passed: {both(co)}/{len(co)} when coordinated, {both(un)}/{len(un)} when not")


if __name__ == "__main__":
    main(Path(sys.argv[1]), Path(sys.argv[2]))
