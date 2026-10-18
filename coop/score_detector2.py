"""Score the detector v2 predictors exactly as pre-registered (results/preregistration_detector2.md).

Reads the interaction-failure ground truth (results/detector2_gt.jsonl, held-out tasks), maps each published patch to
the functions and files it edits (coop/detector_eval.py helpers), and computes three predictors on the frozen v2 graph
(coop/world_graph_v2.py): same file; CALLS distance; CALLS|SHARES_STATE distance (undirected, up to 6 hops, no path = 7,
score = -distance, flag at <= 3). Primary: AUC on eligible pairs whose edited function sets are disjoint. Secondary:
all eligible pairs. 95% bootstrap CIs on AUC (2000 resamples, seed 20261023). No model calls.

  .venv-cb/Scripts/python coop/score_detector2.py            # held-out scores (run once, after the ground truth is complete)
  .venv-cb/Scripts/python coop/score_detector2.py --dev      # pipeline check on dev pairs: predictor values only, no labels
"""
import json
import random
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import world_graph_v2 as v2  # noqa: E402
from detector_eval import CACHE, function_spans, pairs_for, touched_functions  # noqa: E402
from prepare_repo import prepare  # noqa: E402

GT = HERE.parent / "results" / "detector2_gt.jsonl"
DEV = [("pallets_click_task", "2068"), ("pallets_click_task", "2800")]
NO_PATH = 7


def auc(scores, labels):
    """P(score of a random positive > a random negative), ties count half."""
    pos = [s for s, y in zip(scores, labels) if y]
    neg = [s for s, y in zip(scores, labels) if not y]
    if not pos or not neg:
        return None
    return sum((p > n) + 0.5 * (p == n) for p in pos for n in neg) / (len(pos) * len(neg))


def boot_ci(scores, labels, n=2000, seed=20261023):
    rng, idx, vals = random.Random(seed), list(range(len(scores))), []
    for _ in range(n):
        s = [rng.choice(idx) for _ in idx]
        a = auc([scores[i] for i in s], [labels[i] for i in s])
        if a is not None:
            vals.append(a)
    vals.sort()
    return vals[int(0.025 * len(vals))], vals[int(0.975 * len(vals)) - 1]


def features(repo, task, pairs):
    """{(i, j): row} with edited functions/files and the three predictor values."""
    repo_dir, pkg = prepare(repo, task)
    funcs, calls, state, _ = v2.parse_repo(repo_dir / pkg)
    g = v2.load(f"det2_{repo}_{task}", funcs, calls, state)
    known = {f["qualname"] for f in funcs}
    spans = function_spans(repo_dir, pkg)
    out = {}
    for i, j in pairs:
        d = CACHE / repo / f"task{task}" / f"feature{i}_feature{j}"
        p1, p2 = d / f"patch_claude_k1_feature{i}.patch", d / f"patch_claude_k1_feature{j}.patch"
        if not (p1.exists() and p2.exists()):
            continue
        f1, files1 = touched_functions(p1.read_bytes().decode("utf-8").replace("\r\n", "\n"), spans)
        f2, files2 = touched_functions(p2.read_bytes().decode("utf-8").replace("\r\n", "\n"), spans)
        row = {"funcs": [sorted(f1), sorted(f2)], "unknown_funcs": sorted((f1 | f2) - known),
               "disjoint": not (f1 & f2), "same_file": bool(files1 & files2)}
        for key, rel in (("d_calls", "CALLS"), ("d_calls_state", "CALLS|SHARES_STATE")):
            dist = v2.distance(g, sorted(f1 & known), sorted(f2 & known), rel) if f1 and f2 else None
            row[key] = NO_PATH if dist is None else dist
        out[(i, j)] = row
    g.delete()
    return out


def report(rows, title):
    y = [r["interaction_failure"] for r in rows]
    print(f"\n{title}: n = {len(rows)}, interaction failures = {sum(y)}")
    preds = [("same file", [float(r["same_file"]) for r in rows], [r["same_file"] for r in rows]),
             ("CALLS distance", [-r["d_calls"] for r in rows], [r["d_calls"] <= 3 for r in rows]),
             ("CALLS|SHARES_STATE distance", [-r["d_calls_state"] for r in rows], [r["d_calls_state"] <= 3 for r in rows])]
    for name, score, flag in preds:
        tp = sum(f and t for f, t in zip(flag, y))
        fp = sum(f and not t for f, t in zip(flag, y))
        prec = f"{tp / (tp + fp):.2f} ({tp}/{tp + fp})" if tp + fp else "n/a (nothing flagged)"
        rec = f"{tp / sum(y):.2f} ({tp}/{sum(y)})" if sum(y) else "n/a"
        a = auc(score, y)
        ci = boot_ci(score, y) if a is not None else None
        a_txt = f"{a:.3f} [95% CI {ci[0]:.3f}-{ci[1]:.3f}]" if a is not None else "n/a (one class only)"
        print(f"  {name:28} AUC {a_txt}   flag precision {prec}   recall {rec}")


def demo():
    assert auc([3, 2, 1], [1, 0, 0]) == 1.0
    assert auc([1, 2, 3], [1, 0, 0]) == 0.0
    assert auc([1, 1], [1, 0]) == 0.5
    assert auc([1, 1], [1, 1]) is None


def main():
    demo()
    if "--dev" in sys.argv:
        for repo, task in DEV:
            feats = features(repo, task, pairs_for(repo, task))
            unknown = sum(bool(r["unknown_funcs"]) for r in feats.values())
            dist = [r["d_calls_state"] for r in feats.values()]
            print(f"{repo} {task}: {len(feats)} pairs, pairs with an edited function missing from the graph: {unknown}, "
                  f"CALLS|SHARES_STATE distances {sorted(dist)}")
        return
    gt = [json.loads(line) for line in GT.read_text(encoding="utf-8").splitlines()]
    print(f"ground truth rows: {len(gt)}; skipped (missing patch): {sum('skip' in r for r in gt)}; "
          f"eligible: {sum(r.get('eligible', False) for r in gt)}; "
          f"eligible with an evaluator error (dropped): {sum(r.get('eligible', False) and bool(r.get('error')) for r in gt)}")
    rows = []
    for repo, task in sorted({(r["repo"], r["task"]) for r in gt}):
        mine = [r for r in gt if r["repo"] == repo and r["task"] == task and r.get("eligible") and not r.get("error")]
        feats = features(repo, task, [tuple(r["pair"]) for r in mine])
        rows += [r | feats[tuple(r["pair"])] for r in mine if tuple(r["pair"]) in feats]
    out = HERE.parent / "results" / "detector2_rows.json"
    out.write_text(json.dumps(rows, indent=1), encoding="utf-8")
    print(f"pairs with an edited function missing from the graph: {sum(bool(r['unknown_funcs']) for r in rows)}")
    report([r for r in rows if r["disjoint"]], "PRIMARY: eligible held-out pairs, disjoint edited functions")
    report(rows, "SECONDARY: all eligible held-out pairs")


if __name__ == "__main__":
    main()
