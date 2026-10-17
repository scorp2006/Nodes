"""Run HiddenBench tasks with Claude agents and save results + token usage.

Example:
  python pilot/run_hiddenbench.py --benchmark short --task-ids 2 --rounds 3 --model claude-haiku-5-5
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from hiddenbench import load_benchmark, run_evaluation, score_results

from claude_client import ClaudeCLIClient

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "external" / "HiddenBench_ICML" / "data"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--benchmark", default="short", help="short | full")
    ap.add_argument("--task-ids", type=int, nargs="*", help="subset of task ids")
    ap.add_argument("--profile", default="hidden", choices=["hidden", "full"])
    ap.add_argument("--model", default="claude-haiku-5-5")
    ap.add_argument("--rounds", type=int, default=15)
    ap.add_argument("--duplications", type=int, default=1)
    ap.add_argument("--seed", type=int, default=2026)
    ap.add_argument("--workers", type=int, default=1)
    ap.add_argument("--label", default="baseline")
    args = ap.parse_args()

    path = DATA / ("benchmark_short.json" if args.benchmark == "short" else "benchmark.json")
    tasks = load_benchmark(path=str(path))
    if args.task_ids:
        tasks = [t for t in tasks if t.id in set(args.task_ids)]

    client = ClaudeCLIClient(args.model)
    started = time.time()
    results = run_evaluation(tasks, client, args.profile, rounds=args.rounds,
                             duplications=args.duplications, seed=args.seed, max_workers=args.workers,
                             progress_callback=lambda i, n, run: print(f"  run {i}/{n} done", flush=True))
    scores = score_results(results, tasks)

    u = [c["usage"] for c in client.calls]
    usage = {
        "calls": len(client.calls),
        "wall_seconds": round(time.time() - started, 1),
        "input_tokens": sum(x.get("input_tokens", 0) for x in u),
        "cache_creation_input_tokens": sum(x.get("cache_creation_input_tokens", 0) for x in u),
        "cache_read_input_tokens": sum(x.get("cache_read_input_tokens", 0) for x in u),
        "output_tokens": sum(x.get("output_tokens", 0) for x in u),
        "list_price_usd": round(sum(c["cost_usd"] or 0 for c in client.calls), 4),
    }
    results["metadata"].update(label=args.label, harness="claude -p (stateless, isolated)", usage=usage)

    out_dir = ROOT / "runs"
    out_dir.mkdir(exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out = out_dir / f"{args.label}_{args.profile}_{args.model}_{stamp}.json"
    out.write_text(json.dumps({"results": results, "scores": scores}, indent=1), encoding="utf-8")

    print(json.dumps({"scores": {k: v for k, v in scores.items() if not isinstance(v, (list, dict))},
                      "usage": usage, "saved": str(out.relative_to(ROOT))}, indent=1))


if __name__ == "__main__":
    main()
