"""Run HiddenBench tasks with Claude agents under one of three conditions.

  baseline : HiddenBench exactly as published
  nudge    : + one sentence saying others may hold genuine facts you lack
  graph    : + the same sentence + a shared evidence board in FalkorDB (MCP tools)

The nudge text is included in the graph condition, so graph vs nudge isolates
the board itself. Everything else (tasks, prompts, seeds, model) is identical.

Example:
  python pilot/run_hiddenbench.py --condition graph --task-ids 2 --rounds 3
"""
from __future__ import annotations

import argparse
import json
import random
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from hiddenbench import load_benchmark, score_results
from hiddenbench.prompts import load_prompts
from hiddenbench.simulator import _scenario_seed, instantiate_agents, run_scenario

import graph_tools
from claude_client import ClaudeCLIClient

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "external" / "HiddenBench_ICML" / "data"
TOOLS = ["share_fact", "shared_facts", "who_has_not_shared"]

NUDGE = ("Note: other participants may hold genuine facts that you were not given. "
         "Facts they quote come from their own briefing.")
BOARD = (" You also have a shared evidence board. share_fact(fact_number) posts one of your "
         "numbered facts as verified. shared_facts() lists every verified fact posted so far and "
         "who posted it. who_has_not_shared() names participants who hold facts nobody else has "
         "and have not posted them.")
EXTRA = {"baseline": "", "nudge": NUDGE, "graph": NUDGE + BOARD}


def mcp_config_for(graph: str, agent: str) -> str:
    path = ROOT / "runs" / "mcp" / graph / f"{agent.replace(' ', '_')}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    server = {"command": sys.executable,
              "args": ["-I", str(Path(__file__).with_name("graph_tools.py")), "--graph", graph, "--agent", agent]}
    path.write_text(json.dumps({"mcpServers": {"board": server}}), encoding="utf-8")
    return str(path)


def run_one(task, task_index, dup, args, client, prompts, stamp):
    seed = _scenario_seed(args.seed, task_index, dup)
    extra = EXTRA[args.condition]
    graph = None
    if args.condition == "graph":
        # Same seed + same call order as run_scenario, so these are the exact agents it will build.
        agents, assignments = instantiate_agents(task, client, args.profile, prompts, random.Random(seed),
                                                 extra_prompt=extra, special_agent_ratio=1.0)
        graph = f"board_{stamp}_t{task.id}_d{dup}"
        graph_tools.seed(graph, task, assignments)
        for agent in agents:
            client.mcp_for_system[agent.history[0]["content"]] = mcp_config_for(graph, agent.name)

    run = run_scenario(task, client, args.profile, prompts, rounds=args.rounds, seed=seed,
                       extra_prompt=extra, special_agent_ratio=1.0)
    run["condition"] = args.condition
    if graph:
        g = graph_tools.connect(graph)
        run["board"] = {
            "graph": graph,
            "posts": g.ro_query("MATCH (a:Agent)-[s:SHARED]->(f:Fact) "
                                "RETURN a.name, f.id ORDER BY s.seq").result_set,
        }
    print(f"  task {task.id} dup {dup}: majority={run['majority_vote']} correct={task.correct_answer}", flush=True)
    return run


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--condition", default="baseline", choices=list(EXTRA))
    ap.add_argument("--benchmark", default="short", help="short | full")
    ap.add_argument("--task-ids", type=int, nargs="*", help="subset of task ids")
    ap.add_argument("--profile", default="hidden", choices=["hidden", "full"])
    ap.add_argument("--model", default="claude-haiku-5-5")
    ap.add_argument("--rounds", type=int, default=15)
    ap.add_argument("--duplications", type=int, default=1)
    ap.add_argument("--seed", type=int, default=2026)
    ap.add_argument("--workers", type=int, default=1)
    args = ap.parse_args()

    path = DATA / ("benchmark_short.json" if args.benchmark == "short" else "benchmark.json")
    tasks = load_benchmark(path=str(path))
    jobs = [(t, i, d) for i, t in enumerate(tasks) for d in range(args.duplications)
            if not args.task_ids or t.id in args.task_ids]

    client = ClaudeCLIClient(args.model, allowed_tools=" ".join(f"mcp__board__{t}" for t in TOOLS))
    prompts = load_prompts()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    started = time.time()
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        runs = list(pool.map(lambda j: run_one(*j, args, client, prompts, stamp), jobs))

    results = {"metadata": {"benchmark": "HiddenBench", "profile": args.profile, "model": args.model,
                            "condition": args.condition, "rounds": args.rounds, "seed": args.seed,
                            "duplications": args.duplications, "harness": "claude -p (stateless, isolated)"},
               "runs": runs}
    scores = score_results(results, [j[0] for j in jobs])

    u = [c["usage"] for c in client.calls]
    usage = {
        "calls": len(client.calls),
        "calls_with_extra_turns": sum(1 for c in client.calls if (c["num_turns"] or 1) > 1),
        "wall_seconds": round(time.time() - started, 1),
        "cache_creation_input_tokens": sum(x.get("cache_creation_input_tokens", 0) for x in u),
        "cache_read_input_tokens": sum(x.get("cache_read_input_tokens", 0) for x in u),
        "input_tokens": sum(x.get("input_tokens", 0) for x in u),
        "output_tokens": sum(x.get("output_tokens", 0) for x in u),
        "list_price_usd": round(sum(c["cost_usd"] or 0 for c in client.calls), 4),
    }
    results["metadata"]["usage"] = usage

    out = ROOT / "runs" / f"{args.condition}_{args.profile}_{args.model}_{stamp}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps({"results": results, "scores": scores}, indent=1), encoding="utf-8")
    print(json.dumps({"average_accuracy": scores.get("average_accuracy"),
                      "majority_accuracy": scores.get("majority_accuracy"),
                      "usage": usage, "saved": str(out.relative_to(ROOT))}, indent=1))


if __name__ == "__main__":
    main()
