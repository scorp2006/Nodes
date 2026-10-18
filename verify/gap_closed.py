"""REFERENCE (not confirmatory): how much of the hidden-profile gap the board closes, HiddenBench definition.

HiddenBench defines the gap as hidden-profile post-discussion accuracy minus full-profile pre-discussion accuracy
(the full-profile run gives every agent all facts; only its first votes are needed, so it ran with 1 round).
  % of gap closed = (graph_post - baseline_post) / (full_pre - baseline_post)
Computed on the same 40 confirmatory tasks (tasks 1-3 excluded; 45 and 48 have no runs), for both the
individual-agent average (HiddenBench's own metric) and the strict-majority group metric.

  python verify/gap_closed.py <runs/test1> <runs/test1_fullref>
"""
import json
import sys
from pathlib import Path

EXCLUDED = {"t1_d0", "t2_d0", "t3_d0"}


def load(d: Path):
    return {f.stem: json.loads(f.read_text(encoding="utf-8")) for f in d.glob("t*_d0.json")}


def avg_correct(votes, ans):
    return sum(v["vote"] == ans for v in votes) / len(votes)


def majority(votes, ans):
    return float(2 * sum(v["vote"] == ans for v in votes) > len(votes))


def main(test_root: Path, full_root: Path):
    full = load(full_root / "baseline")
    conds = {c: load(test_root / c) for c in ("baseline", "graph", "placebo")}
    tasks = sorted((set(full) & set.intersection(*(set(v) for v in conds.values()))) - EXCLUDED)
    print(f"tasks: {len(tasks)}")
    for metric, fn in (("individual average (HiddenBench metric)", avg_correct), ("group strict majority", majority)):
        full_pre = sum(fn(full[t]["initial_votes"], full[t]["correct_answer"]) for t in tasks) / len(tasks)
        post = {c: sum(fn(v[t]["final_votes"], v[t]["correct_answer"]) for t in tasks) / len(tasks) for c, v in conds.items()}
        gap = full_pre - post["baseline"]
        print(f"\n{metric}:")
        print(f"  full profile, before discussion (ceiling): {full_pre:.1%}")
        for c in ("baseline", "placebo", "graph"):
            print(f"  hidden profile, after discussion, {c:8}: {post[c]:.1%}")
        if gap > 0:
            for c in ("placebo", "graph"):
                print(f"  {c} closes {(post[c] - post['baseline']) / gap:.0%} of the gap")


if __name__ == "__main__":
    main(Path(sys.argv[1]), Path(sys.argv[2]))
