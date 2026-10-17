"""Run HiddenBench tasks with Claude agents under one of three conditions.

  baseline : HiddenBench exactly as published
  nudge    : + one sentence saying others may hold genuine facts you lack
  graph    : + the same sentence + a shared evidence board in FalkorDB (MCP tools)

The nudge text is included in the graph condition, so graph vs nudge isolates
the board itself. Everything else (tasks, prompts, seeds, model) is identical.

Each run is saved to its own file as soon as it finishes, and finished runs are
skipped, so a batch can be stopped and restarted safely.

Example:
  python pilot/run_hiddenbench.py --condition graph --split dev --out runs/dev1 --workers 3
"""
from __future__ import annotations

import argparse
import json
import random
import sys
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from hiddenbench import load_benchmark
from hiddenbench.prompts import load_prompts
from hiddenbench.simulator import _scenario_seed, instantiate_agents, run_scenario

import graph_tools
from claude_client import ClaudeCLIClient

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "external" / "HiddenBench_ICML" / "data"
SPLITS = json.loads((Path(__file__).with_name("splits.json")).read_text(encoding="utf-8"))
TOOLS = ["share_fact", "shared_facts", "who_has_not_shared"]

NUDGE = ("Note: other participants may hold genuine facts that you were not given. "
         "Facts they quote come from their own briefing.")
BOARD = (" You also have a shared evidence board. share_fact(fact_number) posts one of your "
         "numbered facts as verified. shared_facts() lists every verified fact posted so far and "
         "who posted it. who_has_not_shared() names participants who hold facts nobody else has "
         "and have not posted them.")
EXTRA = {"baseline": "", "nudge": NUDGE, "graph": NUDGE + BOARD}


def mcp_config_for(run_dir: Path, graph: str, agent: str) -> str:
    path = run_dir / f"{agent.replace(' ', '_')}.mcp.json"
    args = ["-I", str(Path(__file__).with_name("graph_tools.py")), "--graph", graph, "--agent", agent,
            "--log", str(run_dir / "tool_calls.jsonl")]
    path.write_text(json.dumps({"mcpServers": {"board": {"command": sys.executable, "args": args}}}),
                    encoding="utf-8")
    return str(path)


def usage_summary(calls: list[dict]) -> dict:
    u = [c["usage"] for c in calls]
    return {
        "calls": len(calls),
        "seconds": round(sum((c["duration_ms"] or 0) for c in calls) / 1000, 1),
        "input_tokens": sum(x.get("input_tokens", 0) for x in u),
        "cache_creation_input_tokens": sum(x.get("cache_creation_input_tokens", 0) for x in u),
        "cache_read_input_tokens": sum(x.get("cache_read_input_tokens", 0) for x in u),
        "output_tokens": sum(x.get("output_tokens", 0) for x in u),
        "list_price_usd": round(sum(c["cost_usd"] or 0 for c in calls), 4),
    }


def run_one(task, task_index, dup, args, prompts, out_dir: Path):
    out_file = out_dir / args.condition / f"t{task.id}_d{dup}.json"
    if out_file.exists():
        return "skipped"
    seed = _scenario_seed(args.seed, task_index, dup)
    extra = EXTRA[args.condition]
    client = ClaudeCLIClient(args.model, allowed_tools=" ".join(f"mcp__board__{t}" for t in TOOLS))
    work = out_dir / "work" / args.condition / f"t{task.id}_d{dup}"
    graph = None
    try:
        if args.condition == "graph":
            work.mkdir(parents=True, exist_ok=True)
            (work / "tool_calls.jsonl").unlink(missing_ok=True)
            # Same seed + same call order as run_scenario, so these are the exact agents it will build.
            agents, assignments = instantiate_agents(task, client, args.profile, prompts, random.Random(seed),
                                                     extra_prompt=extra, special_agent_ratio=1.0)
            graph = f"board_{out_dir.name}_t{task.id}_d{dup}"
            graph_tools.seed(graph, task, assignments)  # wipes any leftover board from a failed attempt
            for agent in agents:
                client.mcp_for_system[agent.history[0]["content"]] = mcp_config_for(work, graph, agent.name)

        run = run_scenario(task, client, args.profile, prompts, rounds=args.rounds, seed=seed,
                           extra_prompt=extra, special_agent_ratio=1.0)
    except Exception:
        with open(out_dir / "errors.log", "a", encoding="utf-8") as fh:
            fh.write(f"--- {args.condition} t{task.id} d{dup}\n{traceback.format_exc()}\n")
        print(f"  FAILED {args.condition} task {task.id} dup {dup} (see errors.log)", flush=True)
        return "failed"

    run.update(condition=args.condition, model=args.model, usage=usage_summary(client.calls))
    if graph:
        g = graph_tools.connect(graph)
        log = work / "tool_calls.jsonl"
        run["board"] = {
            "graph": graph,
            "posts": g.ro_query("MATCH (a:Agent)-[s:SHARED]->(f:Fact) "
                                "RETURN a.name, f.id ORDER BY s.seq").result_set,
            "tool_calls": [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
            if log.exists() else [],
        }
    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_text(json.dumps(run, indent=1), encoding="utf-8")
    ok = "OK " if run["majority_vote"] == task.correct_answer else "-- "
    print(f"  {ok}{args.condition} task {task.id} dup {dup}: majority={run['majority_vote']}", flush=True)
    return "done"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--condition", default="baseline", choices=list(EXTRA))
    ap.add_argument("--split", choices=["dev", "test", "short"], default="short")
    ap.add_argument("--task-ids", type=int, nargs="*", help="override: run only these task ids")
    ap.add_argument("--out", required=True, help="batch folder, e.g. runs/dev1")
    ap.add_argument("--profile", default="hidden", choices=["hidden", "full"])
    ap.add_argument("--model", default="claude-haiku-5-5")
    ap.add_argument("--rounds", type=int, default=15)
    ap.add_argument("--duplications", type=int, default=1)
    ap.add_argument("--seed", type=int, default=2026)
    ap.add_argument("--workers", type=int, default=1)
    args = ap.parse_args()

    path = DATA / ("benchmark_short.json" if args.split == "short" else "benchmark.json")
    tasks = load_benchmark(path=str(path))
    wanted = set(args.task_ids or (SPLITS[args.split] if args.split != "short" else [t.id for t in tasks]))
    # task_index comes from the full file so seeds don't depend on which subset we run
    jobs = [(t, i, d) for i, t in enumerate(tasks) for d in range(args.duplications) if t.id in wanted]

    out_dir = ROOT / args.out
    (out_dir / args.condition).mkdir(parents=True, exist_ok=True)
    (out_dir / args.condition / "config.json").write_text(json.dumps(vars(args), indent=1), encoding="utf-8")
    prompts = load_prompts()
    print(f"{args.condition}: {len(jobs)} runs, {args.workers} workers", flush=True)
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        status = list(pool.map(lambda j: run_one(*j, args, prompts, out_dir), jobs))
    print(f"{args.condition} finished: " + ", ".join(f"{s}={status.count(s)}" for s in sorted(set(status))))


if __name__ == "__main__":
    main()
