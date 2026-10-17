"""Summarize a batch folder: accuracy per condition, paired comparison vs baseline, cost.

  python pilot/summarize.py runs/dev1
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


def summarize(batch: Path) -> str:
    data = load(batch)
    lines = [f"Batch: {batch}", ""]
    lines.append(f"{'condition':10} {'runs':>4}  {'group right':>16}  {'95% CI':>13}  "
                 f"{'agents right before->after':>27}  {'list $':>7}")
    for cond, runs in data.items():
        n = len(runs)
        k = sum(majority_correct(r) for r in runs.values())
        lo, hi = wilson(k, n)
        pre = sum(mean_correct(r["initial_votes"], r["correct_answer"]) for r in runs.values()) / n
        post = sum(mean_correct(r["final_votes"], r["correct_answer"]) for r in runs.values()) / n
        cost = sum(r["usage"]["list_price_usd"] for r in runs.values())
        lines.append(f"{cond:10} {n:>4}  {k:>3}/{n:<3} = {k / n:5.0%}  {lo:5.0%}-{hi:4.0%}   "
                     f"{pre:10.0%} -> {post:<10.0%}  {cost:7.2f}")

    if "baseline" in data:
        lines += ["", "Paired vs baseline (same tasks, same seeds; group-level right/wrong):"]
        for cond, runs in data.items():
            if cond == "baseline":
                continue
            common = sorted(set(runs) & set(data["baseline"]))
            wins = sum(majority_correct(runs[t]) > majority_correct(data["baseline"][t]) for t in common)
            losses = sum(majority_correct(runs[t]) < majority_correct(data["baseline"][t]) for t in common)
            lines.append(f"  {cond:8} on {len(common)} tasks: fixed {wins}, broke {losses}, "
                         f"sign-test p = {sign_test(wins, losses):.3f}")

    if "graph" in data:
        lines += ["", "Paired vs graph (does the shared board beat each control?):"]
        for cond, runs in data.items():
            if cond in ("graph", "baseline"):
                continue
            common = sorted(set(runs) & set(data["graph"]))
            wins = sum(majority_correct(data["graph"][t]) > majority_correct(runs[t]) for t in common)
            losses = sum(majority_correct(data["graph"][t]) < majority_correct(runs[t]) for t in common)
            lines.append(f"  graph vs {cond:8} on {len(common)} tasks: graph better on {wins}, worse on {losses}, "
                         f"sign-test p = {sign_test(wins, losses):.3f}")

        lines += ["", "Compute per run (mean):"]
        for cond, runs in data.items():
            u = [r["usage"] for r in runs.values()]
            calls = [len(r.get("board", {}).get("tool_calls", [])) for r in runs.values()]
            lines.append(f"  {cond:8} {sum(x['output_tokens'] for x in u) / len(u):8.0f} output tokens  "
                         f"{sum(x['seconds'] for x in u) / len(u):5.0f} s model time  "
                         f"{sum(calls) / len(calls):5.1f} tool calls")

        runs = data["graph"].values()
        calls = [len(r.get("board", {}).get("tool_calls", [])) for r in runs]
        posted_hidden = [sum(1 for _, fid in r.get("board", {}).get("posts", []) if fid.startswith("h"))
                         / len(r["fact_assignments"]) for r in runs]
        lines += ["", f"Board use: {sum(calls) / len(calls):.1f} tool calls per run, "
                      f"{sum(posted_hidden) / len(posted_hidden):.0%} of private facts posted on average"]
    return "\n".join(lines)


def demo() -> None:
    assert abs(sign_test(5, 0) - 0.0625) < 1e-9
    assert sign_test(3, 3) == 1.0
    lo, hi = wilson(6, 20)
    assert 0.14 < lo < 0.16 and 0.51 < hi < 0.53, (lo, hi)


if __name__ == "__main__":
    demo()
    print(summarize(Path(sys.argv[1])))
