"""Ground truth for detector v2 (results/preregistration_detector2.md): interaction failures, current evaluator.

  python coop/gt_interaction.py [--workers 3]
Resumable: one JSON line per pair in results/detector2_gt.jsonl. Held-out light tasks only. No model calls.
"""
import argparse
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from cooperbench.eval.sandbox import run_patch_test, test_merged

ROOT = Path(__file__).resolve().parent.parent
DATASET = ROOT / "external" / "CooperBench" / "dataset"
PUB = ROOT / "external" / "cb_trajectories"
OUT = ROOT / "results" / "detector2_gt.jsonl"
HELD_OUT = [("pallets_click_task", "2956"), ("pallets_jinja_task", "1465"), ("pallets_jinja_task", "1559"),
            ("pallets_jinja_task", "1621"), ("samuelcolvin_dirty_equals_task", "43")]
lock = threading.Lock()


def lf(path: Path) -> str:
    return path.read_bytes().decode("utf-8").replace("\r\n", "\n")


def one(repo: str, task: str, i: int, j: int) -> dict:
    d = PUB / repo / f"task{task}" / f"feature{i}_feature{j}"
    p1, p2 = d / f"patch_claude_k1_feature{i}.patch", d / f"patch_claude_k1_feature{j}.patch"
    row = {"repo": repo, "task": task, "pair": [i, j]}
    if not (p1.exists() and p2.exists()):
        return row | {"skip": "missing patch"}
    a, b = lf(p1), lf(p2)
    s1 = run_patch_test(repo, int(task), i, a, backend="docker", dataset_dir=DATASET)
    s2 = run_patch_test(repo, int(task), j, b, backend="docker", dataset_dir=DATASET)
    row |= {"alone1": bool(s1.get("passed")), "alone2": bool(s2.get("passed"))}
    row["eligible"] = row["alone1"] and row["alone2"]
    if row["eligible"]:
        m = test_merged(repo, int(task), i, j, a, b, backend="docker", dataset_dir=DATASET)
        clean = m.get("merge", {}).get("status") in ("clean", "identical")
        both = bool(m.get("feature1", {}).get("passed")) and bool(m.get("feature2", {}).get("passed"))
        row |= {"merge_status": m.get("merge", {}).get("status"), "merged_both_pass": both and clean,
                "interaction_failure": not (clean and both), "error": m.get("error")}
    return row


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=3)
    args = ap.parse_args()
    sp = json.loads((Path(__file__).with_name("splits.json")).read_text(encoding="utf-8"))
    done = set()
    if OUT.exists():
        for line in OUT.read_text(encoding="utf-8").splitlines():
            r = json.loads(line)
            done.add((r["repo"], r["task"], tuple(r["pair"])))
    jobs = [(repo, task, i, j) for repo, task in HELD_OUT for i, j in sp["pairs"][f"{repo}/{task}"]
            if (repo, task, (i, j)) not in done]
    print(f"{len(done)} done, {len(jobs)} to go", flush=True)

    def run(job):
        row = one(*job)
        with lock, open(OUT, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
        return row

    with ThreadPoolExecutor(args.workers) as pool:
        for k, _ in enumerate(pool.map(run, jobs), 1):
            if k % 10 == 0:
                print(f"  {k}/{len(jobs)}", flush=True)
    print("ground truth complete")


if __name__ == "__main__":
    main()
