"""One launcher for every CooperBench condition (CooperBench files themselves are unmodified).

  python coop/run_cb_graph.py --condition graph --world-graph cbw_x --repo-dir <checkout> --src src/click \
      --tool-log runs/x.jsonl -- run -n NAME -r pallets_click_task -t 2068 -f 1,2 -a claude_code --git ...

Conditions:
  coop      CooperBench as published (with --git). Only the Windows container-cleanup fix applies.
  protocol  + the coordination procedure, settled over CooperBench's chat; 5 private tools that share nothing
  graph     + the same procedure, settled through 5 shared coordination tools over the repo's world graph

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

# Identical in the protocol and graph conditions, word for word. Only HOW you claim, announce and wait differs
# (MECHANISM below). Nothing here is about scoring beyond what CooperBench's own git block already says.
PROCEDURE = """

## Coordination procedure
If your changes overlap your partner's (the same functions, or functions that call each other):
1. Settle ownership: one of you owns the overlapping functions. The first to claim them owns them.
2. Owner: implement your WHOLE feature first, including the owned functions. Commit,
   `git push team <your branch>`, then announce your commit hash.
3. The other agent (builder): do NOT edit the owned functions. Implement and commit your other parts.
   When the owner has announced its commit: `git fetch team && git merge --no-edit team/<owner>`, add your
   changes to the shared functions on top, commit, `git push team <your branch>`, announce your commit hash,
   then `git diff team/main..HEAD > patch.txt`.
4. Owner, after the builder announces: `git fetch team && git merge --ff-only team/<builder>`, then
   `git diff team/main..HEAD > patch.txt`. Do not resolve conflicts or merge any other way; if --ff-only fails,
   message your partner.
These steps replace the generic final merge sequence above. If your changes do not overlap, use that generic
sequence instead."""

MECHANISM = {
    "graph": """

## How you claim, announce and wait: shared coordination tools
You have five tools backed by a graph of this repository's functions and calls, shared with your partner:
- declare_plan(functions): record the functions you intend to change, using full dotted names
  (e.g. "package.module.Class.method"). It waits for your partner's plan, then reports overlaps and your next step.
- claim(functions): claim functions (step 1). Atomic: the first to claim a function owns it.
- mark_committed(commit): announce your commit hash (steps 2 and 3).
- coordination_status(wait_seconds=0): your role, who owns what, and your next step. With wait_seconds
  (up to 110) it waits for your partner's announcement instead of you polling.
- impact_of(function): what calls, or is called by, a function within 2 calls.
Call declare_plan before editing anything.""",
    # sprint step 7 (exploratory): the same procedure through the product's six native tools (`--profile nodes`)
    "native": """

## How you claim, announce and wait: shared coordination tools
You have six tools from the Nodes server, backed by a graph of this repository's functions and calls, shared with
your partner:
- declare_intent(task, summary, touches): record the functions you intend to change, e.g. touches=
  [{"path": "src/package/module.py", "symbol": "Class.method", "mode": "modify"}]. It reports overlaps with your
  partner's intent and who should own the shared functions.
- before_acting(action="edit", targets): call before editing, with targets as full dotted names
  (e.g. "package.module.Class.method") or "path:10-20". proceed: those functions are now yours (step 1: the first to
  get proceed owns them). defer: your partner owns them. handoff: the owner finished; build on their commit.
- coordinate(op="done", text=<commit hash>): announce your commit hash (steps 2 and 3).
- status(wait_seconds=0): your leases, waits and recent changes. With wait_seconds (up to 60) it waits for your
  partner's announcement instead of you polling.
- context_for(task_or_query): the functions matching a query and who owns them.
Call declare_intent before editing anything.""",
    "protocol": """

## How you claim, announce and wait: messages
Use the coop-* messaging commands described above:
- Plans: before editing anything, send your partner the full dotted names of the functions you intend to change
  (e.g. "package.module.Class.method"), and read theirs with coop-recv.
- Claim (step 1): `coop-send <partner> "CLAIM: <functions>"`. The first claim sent owns them; reply "ACK".
- Announce (steps 2 and 3): `coop-send <partner> "COMMITTED <commit hash>"`.
- Wait: check with coop-recv; between checks run `sleep 30` instead of polling quickly.
You also have five private tools backed by a graph of this repository's functions and calls. They share nothing
with your partner:
- impact_of(function): what calls, or is called by, a function within 2 calls.
- record_plan(functions) / my_plan(): a private plan only you can see.
- write_note(text) / read_notes(): private notes.""",
}

PROMPTS = {c: PROCEDURE + MECHANISM[c] for c in MECHANISM}
TOOL_MODE = {"graph": "graph", "protocol": "placebo"}  # which tool server each condition uses


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


def patch_effort(effort: str | None) -> None:
    """Same reasoning effort for every agent in every condition (Claude Code's --effort flag)."""
    if not effort:
        return
    from cooperbench.agents.claude_code import adapter

    original_command = adapter._build_claude_command

    def build_claude_command(*args, extra_flags: str = "", **kwargs):
        return original_command(*args, extra_flags=extra_flags + f"--effort {shlex.quote(effort)} ", **kwargs)

    adapter._build_claude_command = build_claude_command


def patch_adapter(mode: str, world_graph: str, log_path: str) -> None:
    from cooperbench.agents.claude_code import adapter

    original_instruction = adapter.build_instruction
    original_command = adapter._build_claude_command
    port = coop_tools.PORTS[TOOL_MODE[mode]]

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


def patch_adapter_product(mode: str, port: int, tokens: dict, server: str = "code_graph") -> None:
    """Reproduction stage 2: the same `code_graph` tools, served by the Nodes product (harness-coop profile).
    Identity is each agent's bearer token instead of the harness's X-Agent header."""
    from cooperbench.agents.claude_code import adapter

    original_instruction = adapter.build_instruction
    original_command = adapter._build_claude_command

    def build_instruction(*args, **kwargs):
        return original_instruction(*args, **kwargs) + PROMPTS[mode]

    def build_claude_command(*args, extra_flags: str = "", coop_env=None, **kwargs):
        agent_id = (coop_env or {}).get("COOP_AGENT_ID", "agent")
        mcp = {"mcpServers": {server: {
            "type": "http", "url": f"http://host.docker.internal:{port}/mcp",
            "headers": {"Authorization": f"Bearer {tokens[agent_id]}"}}}}
        extra_flags += f"--mcp-config {shlex.quote(json.dumps(mcp))} "
        return original_command(*args, extra_flags=extra_flags, coop_env=coop_env, **kwargs)

    adapter.build_instruction = build_instruction
    adapter._build_claude_command = build_claude_command


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--condition", choices=["coop", "protocol", "graph", "native"], required=True)
    ap.add_argument("--world-graph", help="graph key for this pair (tool conditions)")
    ap.add_argument("--repo-dir")
    ap.add_argument("--src", help="package dir inside the repo, e.g. src/click")
    ap.add_argument("--tool-log")
    ap.add_argument("--effort", help="Claude Code --effort for every agent (low|medium|high|xhigh|max)")
    ap.add_argument("--product-port", type=int, default=0, help="serve the graph tools from the Nodes product")
    ap.add_argument("cooperbench_args", nargs=argparse.REMAINDER)
    args = ap.parse_args()

    patch_container_cleanup()
    patch_effort(args.effort)  # applied first, so every condition gets exactly the same flag
    if args.condition in ("graph", "native") and args.product_port:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from nodes import indexer, ops
        from nodes.db import Store

        st = Store(args.world_graph)
        st.reset()
        st.setup()
        stats = indexer.index(st, Path(args.repo_dir), args.src, args.world_graph, all_languages=False)  # Python, as in Stage 2
        print(f"product workspace {args.world_graph}: { {k: v for k, v in stats.items() if k != '_units'} }", flush=True)
        st.r.hset("nodes:workspaces", args.world_graph, json.dumps(
            {"root": args.repo_dir, "repo": args.world_graph, "package": args.src, "team_size": 2}))
        tokens = {a: ops.join(st, a, task="cooperbench", client="harness")[0] for a in ("agent1", "agent2")}
        patch_adapter_product(args.condition, args.product_port, tokens,
                              "nodes" if args.condition == "native" else "code_graph")
    elif args.condition != "coop":
        t0 = time.perf_counter()
        funcs, calls, stats = parse_repo(Path(args.repo_dir) / args.src)
        load(args.world_graph, funcs, calls)  # fresh graph: no claims or plans from earlier runs
        print(f"world graph {args.world_graph}: {stats} in {time.perf_counter() - t0:.1f}s", flush=True)
        Path(args.tool_log).unlink(missing_ok=True)
        start_tool_server(TOOL_MODE[args.condition])
        patch_adapter(args.condition, args.world_graph, str(Path(args.tool_log).resolve()))

    from cooperbench.cli import main as cooperbench_main
    sys.argv = ["cooperbench", *[a for a in args.cooperbench_args if a != "--"]]
    cooperbench_main()


if __name__ == "__main__":
    main()
