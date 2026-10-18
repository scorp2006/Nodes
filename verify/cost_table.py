"""Cost per correct group decision and latency per condition, from a HiddenBench batch's own usage records.

Uses the same 40 confirmatory tasks as the primary analysis (tasks 1-3 excluded, drop-from-all). List price is
what claude -p reports (total_cost_usd), summed per run. No model calls.

  python verify/cost_table.py runs/test1 [--exclude t1_d0 t2_d0 t3_d0]
"""
import json
import statistics
import sys
from pathlib import Path


def main(root: Path, exclude: set[str]):
    data = {c.name: {f.stem: json.loads(f.read_text(encoding="utf-8")) for f in c.glob("t*_d0.json")}
            for c in sorted(root.iterdir()) if c.is_dir() and c.name != "work"}
    tasks = set.intersection(*(set(v) for v in data.values())) - exclude
    print(f"tasks: {len(tasks)}")
    print(f"{'condition':17} {'right':>6} {'list $ total':>12} {'$ per right':>11} {'out tok/run':>11} "
          f"{'calls/run':>9} {'wall s p50':>10} {'wall s p90':>10}")
    for c, runs in data.items():
        rs = [runs[t] for t in sorted(tasks)]
        right = sum(2 * sum(v["vote"] == r["correct_answer"] for v in r["final_votes"]) > len(r["final_votes"]) for r in rs)
        cost = sum(r["usage"]["list_price_usd"] for r in rs)
        walls = sorted(r["usage"]["wall_seconds"] for r in rs)
        per = f"{cost / right:.3f}" if right else "n/a"
        print(f"{c:17} {right:>6} {cost:>12.2f} {per:>11} {statistics.mean(r['usage']['output_tokens'] for r in rs):>11.0f} "
              f"{statistics.mean(r['usage']['calls'] for r in rs):>9.1f} {statistics.median(walls):>10.0f} "
              f"{walls[int(0.9 * (len(walls) - 1))]:>10.0f}")


if __name__ == "__main__":
    args = sys.argv[1:]
    ex = set(args[args.index("--exclude") + 1:]) if "--exclude" in args else set()
    main(Path(args[0]), ex)
