"""Run HiddenBench tasks with Claude agents under one of five conditions.

  baseline         : HiddenBench exactly as published
  nudge            : + one sentence saying others may hold genuine facts you lack
  graph            : + the same sentence + a shared board in FalkorDB, posts source-checked
  graph_unverified : + the same sentence + the same board, but free-text posts, nothing checked
  placebo          : + the same sentence + a private notepad (same tool mechanics, nothing shared)

The nudge text is in every tool condition, so graph vs nudge isolates the board,
graph vs placebo separates shared evidence from extra tool use and thinking, and
graph vs graph_unverified isolates the source check. Everything else (tasks,
prompts, seeds, model) is identical.

Each run is saved to its own file as soon as it finishes, and finished runs are
skipped, so a batch can be stopped and restarted safely.

Example:
  python pilot/run_hiddenbench.py --condition graph --split dev --out runs/dev1 --workers 3
"""
from __future__ import annotations

import argparse
import json
import random
import socket
import subprocess
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from hiddenbench import load_benchmark
from hiddenbench.prompts import load_prompts
from hiddenbench.simulator import _scenario_seed, instantiate_agents, run_scenario

import graph_tools
from claude_client import ClaudeCLIClient

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "external" / "HiddenBench_ICML" / "data"
SPLITS = json.loads((Path(__file__).with_name("splits.json")).read_text(encoding="utf-8"))
TOOLS = {"graph": ["share_fact", "shared_facts", "who_has_not_shared"],
         "graph_unverified": ["post_claim", "shared_claims", "who_has_not_posted"],
         "placebo": ["write_note", "read_notes", "review_my_facts"]}
MODE = {"graph": "board", "graph_unverified": "claims", "placebo": "notepad"}

NUDGE = ("Note: other participants may hold genuine facts that you were not given. "
         "Facts they quote come from their own briefing.")
BOARD = (" You also have a shared evidence board. share_fact(fact_number) posts one of your "
         "numbered facts, marked source-checked (it is really in your briefing). shared_facts() lists "
         "every fact posted so far and who posted it. who_has_not_shared() names participants who hold "
         "facts nobody else has and have not posted them.")
CLAIMS = (" You also have a shared evidence board. post_claim(text) posts a claim in your own words; "
          "claims are not checked. shared_claims() lists every claim posted so far and who posted it. "
          "who_has_not_posted() names participants who hold facts nobody else has and have not posted "
          "anything.")
NOTEPAD = (" You also have a private notepad. write_note(text) saves a note only you can see. "
           "read_notes() shows your notes. review_my_facts() lists your own numbered facts.")
EXTRA = {"baseline": "", "nudge": NUDGE, "graph": NUDGE + BOARD,
         "graph_unverified": NUDGE + CLAIMS, "placebo": NUDGE + NOTEPAD}


def mcp_config_for(run_dir: Path, graph: str, agent: str, mode: str, http: bool = False) -> str:
    path = run_dir / f"{agent.replace(' ', '_')}.mcp.json"
    log = str(run_dir / "tool_calls.jsonl")
    if http:  # long-lived server for the whole batch; identity travels in headers we write
        server = {"type": "http", "url": f"http://127.0.0.1:{graph_tools.HTTP_PORTS[mode]}/mcp",
                  "headers": {"X-Board-Graph": graph, "X-Board-Agent": agent, "X-Board-Log": log}}
    else:  # a fresh server process for every agent call
        server = {"command": sys.executable,
                  "args": ["-I", str(Path(__file__).with_name("graph_tools.py")), "--graph", graph,
                           "--agent", agent, "--mode", mode, "--log", log]}
    path.write_text(json.dumps({"mcpServers": {"board": server}}), encoding="utf-8")
    return str(path)


def start_http_server(mode: str) -> subprocess.Popen | None:
    """Start the long-lived tool server for `mode` unless one is already listening."""
    port = graph_tools.HTTP_PORTS[mode]
    with socket.socket() as s:
        if s.connect_ex(("127.0.0.1", port)) == 0:
            return None
    proc = subprocess.Popen([sys.executable, "-I", str(Path(__file__).with_name("graph_tools.py")),
                             "--http", "--mode", mode], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(100):
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return proc
        time.sleep(0.1)
    proc.kill()
    raise RuntimeError(f"tool server for {mode} did not start on port {port}")


def usage_summary(calls: list[dict]) -> dict:
    u = [c["usage"] for c in calls]
    return {
        "calls": len(calls),
        "turns": sum((c["num_turns"] or 1) for c in calls),
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
    started = time.time()
    seed = _scenario_seed(args.seed, task_index, dup)
    extra = EXTRA[args.condition]
    tools = TOOLS.get(args.condition, [])
    client = ClaudeCLIClient(args.model, effort=args.effort, allowed_tools=" ".join(f"mcp__board__{t}" for t in tools))
    work = out_dir / "work" / args.condition / f"t{task.id}_d{dup}"
    graph = None
    try:
        if tools:
            work.mkdir(parents=True, exist_ok=True)
            (work / "tool_calls.jsonl").unlink(missing_ok=True)
            # Same seed + same call order as run_scenario, so these are the exact agents it will build.
            agents, assignments = instantiate_agents(task, client, args.profile, prompts, random.Random(seed),
                                                     extra_prompt=extra, special_agent_ratio=1.0)
            suffix = "" if args.condition == "graph" else f"_{args.condition}"  # keep dev1 graph names stable
            graph = f"board_{out_dir.name}{suffix}_t{task.id}_d{dup}"
            graph_tools.seed(graph, task, assignments)  # wipes any leftover board from a failed attempt
            mode = MODE[args.condition]
            for agent in agents:
                system = agent.history[0]["content"]
                client.mcp_for_system[system] = mcp_config_for(work, graph, agent.name, mode, args.server == "http")
                if args.digest:
                    client.digest_for_system[system] = (
                        lambda name=agent.name: graph_tools.digest(graph, name, mode))

        run = run_scenario(task, client, args.profile, prompts, rounds=args.rounds, seed=seed,
                           extra_prompt=extra, special_agent_ratio=1.0)
    except Exception:
        with open(out_dir / "errors.log", "a", encoding="utf-8") as fh:
            fh.write(f"--- {args.condition} t{task.id} d{dup}\n{traceback.format_exc()}\n")
        print(f"  FAILED {args.condition} task {task.id} dup {dup} (see errors.log)", flush=True)
        return "failed"

    run.update(condition=args.condition, model=args.model,
               usage=usage_summary(client.calls) | {"wall_seconds": round(time.time() - started, 1)})
    if graph:
        g = graph_tools.connect(graph)
        log = work / "tool_calls.jsonl"
        run["board"] = {
            "graph": graph,
            "posts": g.ro_query("MATCH (a:Agent)-[s:SHARED]->(f:Fact) "
                                "RETURN a.name, f.id ORDER BY s.seq").result_set,
            "claims": g.ro_query("MATCH (a:Agent)-[c:CLAIMED]->(cl:Claim) "
                                 "RETURN a.name, cl.text ORDER BY c.seq").result_set,
            "tool_calls": [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
            if log.exists() else [],
        }
    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_text(json.dumps(run, indent=1), encoding="utf-8")
    if not args.no_peek:
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
    ap.add_argument("--effort", choices=["low", "medium", "high"], help="Claude Code --effort (default: CLI default)")
    ap.add_argument("--rounds", type=int, default=15)
    ap.add_argument("--duplications", type=int, default=1)
    ap.add_argument("--seed", type=int, default=2026)
    ap.add_argument("--workers", type=int, default=1)
    ap.add_argument("--no-peek", action="store_true", help="print progress counts only, never results")
    ap.add_argument("--server", choices=["stdio", "http"], default="stdio",
                    help="stdio: new tool process per call (dev1/test1); http: one long-lived server")
    ap.add_argument("--digest", action="store_true", help="push the board into every turn")
    args = ap.parse_args()
    if args.server == "http" and args.condition in MODE:
        start_http_server(MODE[args.condition])  # left running for later batches; it holds no run state

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
    status = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for fut in as_completed([pool.submit(run_one, *j, args, prompts, out_dir) for j in jobs]):
            status.append(fut.result())
            if args.no_peek:
                print(f"  {args.condition}: {len(status)}/{len(jobs)} finished", flush=True)
    print(f"{args.condition} finished: " + ", ".join(f"{s}={status.count(s)}" for s in sorted(set(status))))


if __name__ == "__main__":
    main()
