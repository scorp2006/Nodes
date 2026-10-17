"""Summarize a batch folder: accuracy per condition, pre-registered paired comparisons, cost.

  python pilot/summarize.py runs/dev1
  python pilot/summarize.py runs/test1 --exclude t1_d0 t2_d0 t3_d0
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95% confidence interval for a proportion k/n."""
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return (max(0.0, centre - half), min(1.0, centre + half))


def sign_test(wins: int, losses: int) -> float:
    """Two-sided exact sign test on the tasks where the two conditions disagree."""
    n = wins + losses
    if n == 0:
        return 1.0
    tail = sum(math.comb(n, k) for k in range(min(wins, losses) + 1)) / 2 ** n
    return min(1.0, 2 * tail)


def load(batch: Path) -> dict[str, dict[str, dict]]:
    out = {}
    for cond_dir in sorted(p for p in batch.iterdir() if p.is_dir() and p.name != "work"):
        runs = {f.stem: json.loads(f.read_text(encoding="utf-8")) for f in cond_dir.glob("t*_d*.json")}
        if runs:
            out[cond_dir.name] = runs
    return out


def majority_correct(run: dict) -> int:
    """Strict majority of final votes on the correct answer (HiddenBench's majority rule)."""
    votes = [v["vote"] for v in run["final_votes"]]
    return int(votes.count(run["correct_answer"]) * 2 > len(votes))


def mean_correct(votes: list[dict], answer: str) -> float:
    return sum(v["vote"] == answer for v in votes) / len(votes)


# Pre-registered comparisons (see results/preregistration_test1.md). First one is primary.
PAIRS = [
    ("graph", "placebo", "PRIMARY"),
    ("graph", "baseline", "secondary"),
    ("graph", "nudge", "secondary"),
    ("graph", "graph_unverified", "secondary"),
    ("graph_unverified", "placebo", "secondary"),
]


def summarize(batch: Path, exclude: set[str] = frozenset()) -> str:
    data = {c: {t: r for t, r in runs.items() if t not in exclude} for c, runs in load(batch).items()}
    # Drop-from-all rule: only tasks finished in every condition are counted anywhere.
    complete = set.intersection(*(set(runs) for runs in data.values()))
    dropped = sorted(set().union(*(set(runs) for runs in data.values())) - complete)
    data = {c: {t: r for t, r in runs.items() if t in complete} for c, runs in data.items()}

    lines = [f"Batch: {batch}" + (f"  (excluded by plan: {', '.join(sorted(exclude))})" if exclude else ""),
             f"Tasks complete in all {len(data)} conditions: {len(complete)}"
             + (f" (dropped from every condition: {', '.join(dropped)})" if dropped else ""), ""]
    lines.append(f"{'condition':17} {'runs':>4}  {'group right':>16}  {'95% CI':>13}  "
                 f"{'agents right before->after':>27}  {'list $':>7}")
    for cond, runs in data.items():
        n = len(runs)
        k = sum(majority_correct(r) for r in runs.values())
        lo, hi = wilson(k, n)
        pre = sum(mean_correct(r["initial_votes"], r["correct_answer"]) for r in runs.values()) / n
        post = sum(mean_correct(r["final_votes"], r["correct_answer"]) for r in runs.values()) / n
        cost = sum(r["usage"]["list_price_usd"] for r in runs.values())
        lines.append(f"{cond:17} {n:>4}  {k:>3}/{n:<3} = {k / n:5.0%}  {lo:5.0%}-{hi:4.0%}   "
                     f"{pre:10.0%} -> {post:<10.0%}  {cost:7.2f}")

    lines += ["", "Paired comparisons (exact two-sided sign test on group right/wrong, same tasks and seeds):"]
    for a, b, kind in PAIRS:
        if a not in data or b not in data:
            continue
        wins = sum(majority_correct(data[a][t]) > majority_correct(data[b][t]) for t in complete)
        losses = sum(majority_correct(data[a][t]) < majority_correct(data[b][t]) for t in complete)
        lines.append(f"  [{kind:9}] {a} vs {b}: {a} better on {wins}, worse on {losses}, "
                     f"p = {sign_test(wins, losses):.3f}")

    lines += ["", "Compute per run (mean):"]
    for cond, runs in data.items():
        u = [r["usage"] for r in runs.values()]
        calls = [len(r.get("board", {}).get("tool_calls", [])) for r in runs.values()]
        turns = [x.get("turns") for x in u if x.get("turns") is not None]
        wall = [x.get("wall_seconds") for x in u if x.get("wall_seconds") is not None]
        lines.append(f"  {cond:17} {sum(x['output_tokens'] for x in u) / len(u):8.0f} output tokens  "
                     f"{sum(x['seconds'] for x in u) / len(u):5.0f} s model time  "
                     f"{sum(calls) / len(calls):5.1f} tool calls"
                     + (f"  {sum(turns) / len(turns):5.1f} turns" if turns else "")
                     + (f"  {sum(wall) / len(wall):5.0f} s wall" if wall else ""))

    if "graph" in data:
        runs = data["graph"].values()
        posted_hidden = [sum(1 for _, fid in r.get("board", {}).get("posts", []) if fid.startswith("h"))
                         / len(r["fact_assignments"]) for r in runs]
        lines += ["", f"Graph board: {sum(posted_hidden) / len(posted_hidden):.0%} of private facts posted on average"]
    return "\n".join(lines)


def demo() -> None:
    assert abs(sign_test(5, 0) - 0.0625) < 1e-9
    assert sign_test(3, 3) == 1.0
    lo, hi = wilson(6, 20)
    assert 0.14 < lo < 0.16 and 0.51 < hi < 0.53, (lo, hi)


if __name__ == "__main__":
    demo()
    args = sys.argv[1:]
    excluded = set(args[args.index("--exclude") + 1:]) if "--exclude" in args else set()
    print(summarize(Path(args[0]), excluded))
