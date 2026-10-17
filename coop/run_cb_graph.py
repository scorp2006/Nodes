"""One launcher for every CooperBench condition (CooperBench files themselves are unmodified).

  python coop/run_cb_graph.py --condition graph --world-graph cbw_x --repo-dir <checkout> --src src/click \
      --tool-log runs/x.jsonl -- run -n NAME -r pallets_click_task -t 2068 -f 1,2 -a claude_code --git ...

Conditions:
  coop     CooperBench as published (with --git). Only the Windows container-cleanup fix applies.
  placebo  + 5 private tools (same server code, same tool count), and a paragraph describing them
  graph    + 5 shared coordination tools over the repo's world graph, and a paragraph with the protocol

At runtime this script: fixes container cleanup (all conditions); for tool conditions it loads a fresh world
graph for the pair, starts the long-lived tool server for that mode, appends the paragraph to each agent's
instruction, and adds `--mcp-config` (pointing at the server, with this agent's id) to each claude call.
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

PROMPTS = {
    "graph": """

## Coordination tools (shared with your partner)
You have five tools backed by a graph of this repository's functions and calls, shared with your partner:
- declare_plan(functions): record the functions you intend to change, using full dotted names
  (e.g. "click.termui.edit"). It waits for your partner's plan, then reports overlaps and your next step.
- claim(functions): claim functions so only you change them. Atomic: the first to claim owns them.
- mark_committed(commit): after you commit and `git push team <your branch>`, pass the commit hash.
- coordination_status(wait_seconds=0): your role, who owns what, and your exact next step. With
  wait_seconds (up to 110) it waits for your partner instead of you polling.
- impact_of(function): what calls, or is called by, a function within 2 calls.
Protocol: call declare_plan before editing anything. If you overlap, claim the overlapping functions.
Then follow the "Next step" that the tools give you, every time. The owner implements first; the other agent
merges the owner's commit and builds on top; the owner then fast-forwards to that combined branch, so you
both submit the same tree. Where the tools give you final git commands, use them instead of the generic
merge sequence above.""",
    "placebo": """

## Planning tools (private to you)
You have five tools backed by a graph of this repository's functions and calls. They are private:
- impact_of(function): what calls, or is called by, a function within 2 calls.
- record_plan(functions): record the functions you intend to change, in a private plan.
- my_plan(): show your private plan.
- write_note(text): save a private note.
- read_notes(): show your private notes.
Protocol: record your plan before editing and check impact_of for functions you will change.
Coordinate with your partner using the messaging and git workflow described above.""",
}


def start_tool_server(mode: str) -> None:
    """Reuse the running server for `mode` only if it runs the current code; otherwise replace it."""
    port = coop_tools.PORTS[mode]
    stamp = HERE / f".server_{mode}.json"
    with socket.socket() as s:
        listening = s.connect_ex(("127.0.0.1", port)) == 0
    if listening:
        info = json.loads(stamp.read_text(encoding="utf-8")) if stamp.exists() else {}
        if info.get("code") == coop_tools.code_fingerprint():
            return
        if info.get("pid"):
            subprocess.run(["taskkill", "/PID", str(info["pid"]), "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(1)
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                raise RuntimeError(f"a server from older code still holds port {port}; stop it first")
    main_python = HERE.parent.parent.parent / ".venv" / "Scripts" / "python.exe"
    subprocess.Popen([str(main_python), "-I", str(HERE / "coop_tools.py"), "--mode", mode],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(150):
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.1)
    raise RuntimeError(f"{mode} tool server did not start on port {port}")


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


def patch_adapter(mode: str, world_graph: str, log_path: str) -> None:
    from cooperbench.agents.claude_code import adapter

    original_instruction = adapter.build_instruction
    original_command = adapter._build_claude_command
    port = coop_tools.PORTS[mode]

    def build_instruction(*args, **kwargs):
        return original_instruction(*args, **kwargs) + PROMPTS[mode]

    def build_claude_command(*args, extra_flags: str = "", coop_env=None, **kwargs):
        agent_id = (coop_env or {}).get("COOP_AGENT_ID", "agent")
        mcp = {"mcpServers": {"code_graph": {
            "type": "http", "url": f"http://host.docker.internal:{port}/mcp",
            "headers": {"X-World-Graph": world_graph, "X-Agent": agent_id, "X-Log": log_path,
                        "X-Team-Size": str(len((coop_env or {}).get("COOP_AGENTS", "a,b").split(",")))}}}}
        extra_flags += f"--mcp-config {shlex.quote(json.dumps(mcp))} "
        return original_command(*args, extra_flags=extra_flags, coop_env=coop_env, **kwargs)

    adapter.build_instruction = build_instruction
    adapter._build_claude_command = build_claude_command


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--condition", choices=["coop", "placebo", "graph"], required=True)
    ap.add_argument("--world-graph", help="graph key for this pair (tool conditions)")
    ap.add_argument("--repo-dir")
    ap.add_argument("--src", help="package dir inside the repo, e.g. src/click")
    ap.add_argument("--tool-log")
    ap.add_argument("cooperbench_args", nargs=argparse.REMAINDER)
    args = ap.parse_args()

    patch_container_cleanup()
    if args.condition != "coop":
        t0 = time.perf_counter()
        funcs, calls, stats = parse_repo(Path(args.repo_dir) / args.src)
        load(args.world_graph, funcs, calls)  # fresh graph: no claims or plans from earlier runs
        print(f"world graph {args.world_graph}: {stats} in {time.perf_counter() - t0:.1f}s", flush=True)
        Path(args.tool_log).unlink(missing_ok=True)
        start_tool_server(args.condition)
        patch_adapter(args.condition, args.world_graph, str(Path(args.tool_log).resolve()))

    from cooperbench.cli import main as cooperbench_main
    sys.argv = ["cooperbench", *[a for a in args.cooperbench_args if a != "--"]]
    cooperbench_main()


if __name__ == "__main__":
    main()
