"""Detector v3 orchestration (results/preregistration_detector3.md). No model calls. Graph/GT logic is reused frozen.

  python coop/detector3.py static     download patches, map edits, find disjoint pairs -> results/detector3_static.jsonl
  python coop/detector3.py gt         ground truth on disjoint pairs, seeded order, until the cut-off; counts only
  python coop/detector3.py score      run ONCE at completion or cut-off: pooled with v2 light rows
"""
import json
import random
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from detector_eval import fetch, function_spans, pairs_for, touched_functions  # noqa: E402
from prepare_repo import prepare  # noqa: E402

RES = HERE.parent / "results"
STATIC, GT = RES / "detector3_static.jsonl", RES / "detector3_gt.jsonl"
HEAVY = [("dottxt_ai_outlines_task", t) for t in ("1371", "1655", "1706")] + \
        [("dspy_task", t) for t in ("8394", "8563", "8587", "8635")] + \
        [("huggingface_datasets_task", t) for t in ("3997", "6252", "7309")] + \
        [("llama_index_task", t) for t in ("17070", "17244", "18813")]
CUTOFF = datetime(2026, 10, 18, 12, 30, tzinfo=timezone(timedelta(hours=5, minutes=30)))
DOCKER = r"C:\Program Files\Docker\Docker\resources\bin\docker.exe"
lock = threading.Lock()


def lf(s: str) -> str:
    return s.replace("\r\n", "\n")


def static():
    rows = []
    for repo, task in HEAVY:
        try:
            repo_dir, pkg = prepare(repo, task)
            spans = function_spans(repo_dir, pkg)
        except Exception as e:  # noqa: BLE001  (pre-registered: a repo that fails to parse is excluded and listed)
            rows += [{"repo": repo, "task": task, "pair": list(p), "excluded": f"parse failed: {e!r}"[:200]}
                     for p in pairs_for(repo, task)]
            continue
        for i, j in pairs_for(repo, task):
            d = f"{repo}/task{task}/feature{i}_feature{j}"
            p1, p2 = fetch(f"{d}/patch_claude_k1_feature{i}.patch"), fetch(f"{d}/patch_claude_k1_feature{j}.patch")
            row = {"repo": repo, "task": task, "pair": [i, j]}
            if not (p1 and p2):
                rows.append(row | {"excluded": "missing patch"})
                continue
            f1, files1 = touched_functions(lf(p1), spans)
            f2, files2 = touched_functions(lf(p2), spans)
            rows.append(row | {"funcs": [sorted(f1), sorted(f2)], "same_file": bool(files1 & files2),
                               "disjoint": not (f1 & f2)})
        print(f"{repo} {task}: {sum(r['repo'] == repo and r['task'] == task for r in rows)} pairs mapped", flush=True)
    STATIC.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    ok = [r for r in rows if "excluded" not in r]
    print(f"pairs: {len(rows)}; excluded: {len(rows) - len(ok)}; disjoint: {sum(r['disjoint'] for r in ok)}")


def mem_frac() -> float:
    out = subprocess.run([DOCKER, "stats", "--no-stream", "--format", "{{.MemPerc}}"], capture_output=True, text=True).stdout
    return max([float(x.strip("%")) for x in out.split() if x.strip("%")] or [0]) / 100 if out else 0.0


def gt(workers: int = 2):
    from gt_interaction import one  # frozen ground-truth logic (v2)

    rows = [json.loads(line) for line in STATIC.read_text(encoding="utf-8").splitlines()]
    todo = [r for r in rows if r.get("disjoint")]
    done = {(r["repo"], r["task"], tuple(r["pair"])) for r in map(json.loads, GT.read_text(encoding="utf-8").splitlines())} \
        if GT.exists() else set()
    rng = random.Random(20261025)
    repos = sorted({r["repo"] for r in todo})
    rng.shuffle(repos)
    total = len(todo)
    for repo in repos:  # one heavy repo at a time
        pairs = [r for r in todo if r["repo"] == repo]
        rng.shuffle(pairs)
        pairs = [r for r in pairs if (r["repo"], r["task"], tuple(r["pair"])) not in done]
        for task in sorted({r["task"] for r in pairs}):  # pull only the images needed
            image = "akhatua/cooperbench-" + repo.replace("_task", "").replace("_", "-") + f":task{task}"
            if subprocess.run([DOCKER, "image", "inspect", image], capture_output=True).returncode:
                subprocess.run([DOCKER, "pull", "-q", image], capture_output=True)

        def work(r):
            while mem_frac() > 0.75:  # pre-registered: back off to fewer containers above 75% memory
                time.sleep(15)
            if datetime.now(timezone.utc) >= CUTOFF:
                return
            try:
                res = one(r["repo"], r["task"], *r["pair"])
            except Exception as e:  # noqa: BLE001  an evaluator crash is an infra error (excluded, counted)
                res = {"repo": r["repo"], "task": r["task"], "pair": r["pair"], "infra_error": repr(e)[:300]}
            res["done_at"] = datetime.now(timezone.utc).isoformat()
            with lock:
                with GT.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(res) + "\n")
                done.add((r["repo"], r["task"], tuple(r["pair"])))
                print(f"  {len(done)}/{total} pairs done", flush=True)  # counts only, never outcomes

        with ThreadPoolExecutor(workers) as ex:
            list(ex.map(work, pairs))
        if datetime.now(timezone.utc) >= CUTOFF:
            break
    print(f"stopped: {len(done)}/{total} done, cut-off {'reached' if datetime.now(timezone.utc) >= CUTOFF else 'not reached'}")


def score():
    """Run ONCE. Pooled primary (v2 light disjoint rows + heavy rows finished by the cut-off), then the secondaries."""
    import world_graph_v2 as v2
    from falkordb import FalkorDB
    from score_detector2 import NO_PATH, report

    db = FalkorDB(port=6390)
    v2.FalkorDB = lambda **kw: db  # v2.load hard-codes 6379; use the separate container
    static_rows = {(r["repo"], r["task"], tuple(r["pair"])): r
                   for r in map(json.loads, STATIC.read_text(encoding="utf-8").splitlines())}
    gt_rows = [json.loads(line) for line in GT.read_text(encoding="utf-8").splitlines()] if GT.exists() else []
    in_time = [r for r in gt_rows if datetime.fromisoformat(r["done_at"]) <= CUTOFF]
    infra = [r for r in in_time if "infra_error" in r or r.get("error") or "skip" in r]
    elig = [r for r in in_time if r.get("eligible") and r not in infra]
    disjoint = [k for k, r in static_rows.items() if r.get("disjoint")]
    print(f"heavy disjoint pairs: {len(disjoint)}; tested by the cut-off: {len(in_time)}; "
          f"not tested: {len(disjoint) - len(in_time)}; infra errors (dropped): {len(infra)}; eligible: {len(elig)}")
    heavy = []
    for repo, task in sorted({(r["repo"], r["task"]) for r in elig}):
        rd, pkg = prepare(repo, task)
        funcs, calls, state, _ = v2.parse_repo(rd / pkg)
        g = v2.load(f"det3_{repo}_{task}", funcs, calls, state)
        known = {f["qualname"] for f in funcs}
        for r in [x for x in elig if (x["repo"], x["task"]) == (repo, task)]:
            st = static_rows[(repo, task, tuple(r["pair"]))]
            f1, f2 = (set(f) & known for f in st["funcs"])
            row = r | {"funcs": st["funcs"], "same_file": st["same_file"], "disjoint": True, "heavy": True}
            for key, rel in (("d_calls", "CALLS"), ("d_calls_state", "CALLS|SHARES_STATE")):
                d = v2.distance(g, sorted(f1), sorted(f2), rel) if f1 and f2 else None
                row[key] = NO_PATH if d is None else d
            heavy.append(row)
        g.delete()
    light = [r | {"heavy": False} for r in json.loads((RES / "detector2_rows.json").read_text(encoding="utf-8"))
             if r["disjoint"]]
    (RES / "detector3_rows.json").write_text(json.dumps(heavy, indent=1), encoding="utf-8")
    pooled = light + heavy
    report(pooled, "PRIMARY: eligible disjoint pairs, pooled (v2 light + v3 heavy)")
    report(heavy, "SECONDARY: eligible disjoint pairs, heavy repos only")
    report([r for r in pooled if all(r["funcs"])],
           "SECONDARY (amendment 1): pooled, both patches edit >= 1 mapped function")


if __name__ == "__main__":
    {"static": static, "gt": gt, "score": score}[sys.argv[1]]()
