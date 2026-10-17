"""Run CooperBench (unmodified files) with our code-graph tools added to every claude_code agent.

  python coop/run_cb_graph.py --world-graph cbw_click2068_f1_f2 --repo-dir <checkout> --src src/click -- \
      run -n smoke-graph -r pallets_click_task -t 2068 -f 1,2 -a claude_code ...

What this changes, at runtime only:
  1. loads the pair's repo into a fresh world graph (functions + calls) in FalkorDB
  2. starts the long-lived code-graph tool server (coop_tools.py) if it isn't running
  3. appends a short paragraph about the tools to each agent's instruction
  4. adds `--mcp-config` (pointing at that server, with this agent's id) to each claude call
Everything else (prompts, protocol, containers, evaluation) is CooperBench as published.
"""
from __future__ import annotations

import argparse
import json
import shlex
import socket
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import coop_tools  # noqa: E402
from world_graph import load, parse_repo  # noqa: E402

GRAPH_PROMPT = """

## Code coordination tools
You also have three tools backed by a graph of this repository's functions and calls:
- declare_plan(functions): record the functions you intend to change (e.g. ["click.termui.edit"]). It
  replaces your previous plan and tells you if any of them is within 3 calls of your partner's plan.
- check_conflicts(): re-check your plan against your partner's current plan.
- impact_of(function): list what calls, or is called by, a function within 2 calls.
Declare your plan before editing, and check again before you finish."""


def start_tool_server() -> None:
    with socket.socket() as s:
        if s.connect_ex(("127.0.0.1", coop_tools.PORT)) == 0:
            return
    main_python = HERE.parent.parent.parent / ".venv" / "Scripts" / "python.exe"
    subprocess.Popen([str(main_python), "-I", str(HERE / "coop_tools.py")],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(100):
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", coop_tools.PORT)) == 0:
                return
        time.sleep(0.1)
    raise RuntimeError("code-graph tool server did not start")


def patch_container_cleanup() -> None:
    """CooperBench stops containers with `timeout 60 docker stop ... >/dev/null &` via the shell, which only
    works on Linux; on Windows it fails ("The system cannot find the path specified") and every agent
    container keeps running. Replace it with a plain `docker rm -f`. Agent behavior is unaffected."""
    from cooperbench.agents.mini_swe_agent_v2.environments.docker import DockerEnvironment

    def cleanup(self):
        cid = getattr(self, "container_id", None)
        if cid is not None:
            subprocess.run([self.config.executable, "rm", "-f", cid],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)
            self.container_id = None

    DockerEnvironment.cleanup = cleanup


def patch_adapter(world_graph: str, log_path: str) -> None:
    from cooperbench.agents.claude_code import adapter

    original_instruction = adapter.build_instruction
    original_command = adapter._build_claude_command

    def build_instruction(*args, **kwargs):
        return original_instruction(*args, **kwargs) + GRAPH_PROMPT

    def build_claude_command(*args, extra_flags: str = "", coop_env=None, **kwargs):
        agent_id = (coop_env or {}).get("COOP_AGENT_ID", "agent")
        mcp = {"mcpServers": {"code_graph": {
            "type": "http", "url": f"http://host.docker.internal:{coop_tools.PORT}/mcp",
            "headers": {"X-World-Graph": world_graph, "X-Agent": agent_id, "X-Log": log_path}}}}
        extra_flags += f"--mcp-config {shlex.quote(json.dumps(mcp))} "
        return original_command(*args, extra_flags=extra_flags, coop_env=coop_env, **kwargs)

    adapter.build_instruction = build_instruction
    adapter._build_claude_command = build_claude_command


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--world-graph", help="omit to run plain coop (only the container-cleanup fix applies)")
    ap.add_argument("--repo-dir")
    ap.add_argument("--src", help="package dir inside the repo, e.g. src/click")
    ap.add_argument("--tool-log")
    ap.add_argument("cooperbench_args", nargs=argparse.REMAINDER)
    args = ap.parse_args()

    patch_container_cleanup()
    if args.world_graph:
        t0 = time.perf_counter()
        funcs, calls, stats = parse_repo(Path(args.repo_dir) / args.src)
        load(args.world_graph, funcs, calls)
        print(f"world graph {args.world_graph}: {stats} in {time.perf_counter() - t0:.1f}s", flush=True)
        Path(args.tool_log).unlink(missing_ok=True)
        start_tool_server()
        patch_adapter(args.world_graph, str(Path(args.tool_log).resolve()))

    from cooperbench.cli import main as cooperbench_main
    sys.argv = ["cooperbench", *[a for a in args.cooperbench_args if a != "--"]]
    cooperbench_main()


if __name__ == "__main__":
    main()
