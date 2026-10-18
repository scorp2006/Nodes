"""Score detector v4 (results/preregistration_detector4.md) on the SAME rows as v3. Run ONCE, right after
`coop/detector3.py score` (which writes results/detector3_rows.json). No model calls, no new patch tests.

Signals are loaded into falkordb-scale (port 6390) as edges and queried with Cypher:
  (:Function)-[:CO_CHANGES {support, confidence, resolved_by, window}]-(:Function)
  (:Test)-[:EXERCISES {resolved_by, confidence}]->(:Function)
Predictors (fixed in advance): same_fn; cov = Jaccard of the test sets exercising A's and B's functions;
cc = max co-change confidence between A and B; risk = same_fn + 0.5*cov + 0.5*cc.
Flags: same_fn = 1; cov > 0; cc >= 0.1; risk >= 0.25.
"""
import json
import random
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from falkordb import FalkorDB  # noqa: E402
from score_detector2 import auc  # noqa: E402

RES = HERE.parent / "results"
SIG = RES / "v4_signals"
FLAGS = {"same_fn": lambda r: r["same_fn"] >= 1, "cov": lambda r: r["cov"] > 0,
         "cc": lambda r: r["cc"] >= 0.1, "risk": lambda r: r["risk"] >= 0.25}
SEED, RESAMPLES = 20261023, 2000


def load_graph(db, repo, task):
    g = db.select_graph(f"v4_{repo}_{task}")
    g.query("MATCH (n) DETACH DELETE n")
    g.query("CREATE INDEX FOR (f:Function) ON (f.qualname)")
    cc = json.loads((SIG / f"{repo}_{task}.cochange.json").read_text(encoding="utf-8"))
    cv_path = SIG / f"{repo}_{task}.coverage.json"
    cv = json.loads(cv_path.read_text(encoding="utf-8")) if cv_path.exists() else {"status": "missing", "exercises": {}}
    for i in range(0, len(cc["edges"]), 1000):
        g.query("UNWIND $e AS e MERGE (a:Function {qualname: e.a}) MERGE (b:Function {qualname: e.b}) "
                "MERGE (a)-[c:CO_CHANGES]->(b) SET c.support = e.support, c.confidence = e.confidence, "
                "c.resolved_by = e.resolved_by, c.window = e.window", {"e": cc["edges"][i:i + 1000]})
    rows = [{"t": t, "f": f} for t, fs in cv["exercises"].items() for f in fs]
    for i in range(0, len(rows), 1000):
        g.query("UNWIND $r AS r MERGE (t:Test {name: r.t}) MERGE (f:Function {qualname: r.f}) "
                "MERGE (t)-[e:EXERCISES]->(f) SET e.resolved_by = 'coverage', e.confidence = 1.0", {"r": rows[i:i + 1000]})
    # v4 amendment 1: a run that mapped no test (collection failed) counts as coverage unavailable
    return g, cv["status"] if cv["exercises"] else "coverage unavailable (no test mapped)"


def signals(g, a, b):
    ta = {r[0] for r in g.ro_query("MATCH (t:Test)-[:EXERCISES]->(f:Function) WHERE f.qualname IN $a RETURN DISTINCT t.name",
                                   {"a": a}).result_set}
    tb = {r[0] for r in g.ro_query("MATCH (t:Test)-[:EXERCISES]->(f:Function) WHERE f.qualname IN $b RETURN DISTINCT t.name",
                                   {"b": b}).result_set}
    cov = len(ta & tb) / len(ta | tb) if ta | tb else 0.0
    cc = g.ro_query("MATCH (x:Function)-[c:CO_CHANGES]-(y:Function) WHERE x.qualname IN $a AND y.qualname IN $b "
                    "RETURN max(c.confidence)", {"a": a, "b": b}).result_set[0][0] or 0.0
    return cov, cc


def report(rows, title):
    y = [r["interaction_failure"] for r in rows]
    print(f"\n{title}: n = {len(rows)}, interaction failures = {sum(y)}"
          + ("   [fewer than 10 failures: descriptive only]" if sum(y) < 10 else ""))
    rng = random.Random(SEED)
    idx = list(range(len(rows)))
    boots = [[rng.choice(idx) for _ in idx] for _ in range(RESAMPLES)]
    base = [r["same_fn"] for r in rows]
    for name in ("same_fn", "cov", "cc", "risk"):
        score = [r[name] for r in rows]
        a = auc(score, y)
        flag = [FLAGS[name](r) for r in rows]
        tp, fl = sum(f and t for f, t in zip(flag, y)), sum(flag)
        prec = f"{tp / fl:.2f} ({tp}/{fl})" if fl else "n/a (nothing flagged)"
        rec = f"{tp / sum(y):.2f} ({tp}/{sum(y)})" if sum(y) else "n/a"
        line = f"  {name:8} AUC {a:.3f}" if a is not None else f"  {name:8} AUC n/a"
        if a is not None and name != "same_fn":  # primary comparison: AUC(new) - AUC(same_fn), paired bootstrap
            diffs = sorted(d for d in ((auc([score[i] for i in s], [y[i] for i in s]),
                                        auc([base[i] for i in s], [y[i] for i in s])) for s in boots)
                           if None not in d for d in [d[0] - d[1]])
            line += (f"   vs same_fn: {a - auc(base, y):+.3f} [95% CI {diffs[int(.025 * len(diffs))]:+.3f} to "
                     f"{diffs[int(.975 * len(diffs)) - 1]:+.3f}]")
        print(line + f"   flag precision {prec}   recall {rec}")


def main():
    db = FalkorDB(port=6390)
    light = [r for r in json.loads((RES / "detector2_rows.json").read_text(encoding="utf-8")) if r["disjoint"]]
    heavy = json.loads((RES / "detector3_rows.json").read_text(encoding="utf-8"))
    rows, status = [], {}
    for r in light + heavy:
        key = (r["repo"], r["task"])
        if key not in status:
            status[key] = load_graph(db, *key)
        g, st = status[key]
        a, b = r["funcs"]
        cov, cc = signals(g, a, b)
        same = float(bool(set(a) & set(b)))
        rows.append(r | {"same_fn": same, "cov": cov, "cc": cc, "risk": same + 0.5 * cov + 0.5 * cc,
                         "coverage_status": st})
    print("coverage status by task:", {f"{k[0]}/{k[1]}": v[1] for k, v in status.items()})
    (RES / "detector4_rows.json").write_text(json.dumps(rows, indent=1), encoding="utf-8")
    report(rows, "PRIMARY: eligible disjoint pairs, pooled (same rows as v3 primary)")
    report([r for r in rows if r["coverage_status"] == "ok"], "SECONDARY: pooled, tasks with coverage only")
    report([r for r in rows if all(r["funcs"])], "SECONDARY: pooled, both patches edit >= 1 mapped function")
    for g, _ in status.values():
        g.delete()


if __name__ == "__main__":
    main()
