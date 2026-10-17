"""Re-merge patch pairs with CooperBench's CURRENT evaluator (no model calls) to explain conflict-rate differences.

  python coop/merge_probe.py
Runs: (a) published Dec-2025 Claude patches for click 2068, a few pairs; (b) our dev patches, read as raw bytes and
forced to LF, to rule out line endings.
"""
import json
import sys
from pathlib import Path

from cooperbench.eval.sandbox import test_merged

ROOT = Path(__file__).resolve().parent.parent
DATASET = ROOT / "external" / "CooperBench" / "dataset"
PUB = ROOT / "external" / "cb_trajectories" / "pallets_click_task" / "task2068"
OURS = ROOT / "runs" / "cooperbench"


def lf(path: Path) -> str:
    return path.read_bytes().decode("utf-8").replace("\r\n", "\n")


def probe(label: str, p1: str, p2: str, f1: int, f2: int) -> dict:
    r = test_merged("pallets_click_task", 2068, f1, f2, p1, p2, backend="docker", dataset_dir=DATASET)
    out = {"label": label, "merge": r.get("merge", {}).get("status"), "strategy": r.get("merge", {}).get("strategy"),
           "apply": r.get("apply_status"), "f1": r.get("feature1", {}).get("passed"),
           "f2": r.get("feature2", {}).get("passed"), "error": r.get("error")}
    print(json.dumps(out), flush=True)
    return out


if __name__ == "__main__":
    for i, j in [(1, 2), (1, 3), (2, 3), (3, 4), (5, 6)]:
        d = PUB / f"feature{i}_feature{j}"
        a, b = d / f"patch_claude_k1_feature{i}.patch", d / f"patch_claude_k1_feature{j}.patch"
        if a.exists() and b.exists():
            probe(f"published f{i}_f{j}", lf(a), lf(b), i, j)
    for run in ["smoke-coop2", "dev3-click-coop"]:
        d = OURS / run / "coop" / "pallets_click_task" / "2068" / "f1_f2"
        probe(f"ours {run} (forced LF)", lf(d / "agent1.patch"), lf(d / "agent2.patch"), 1, 2)
    sys.exit(0)
