"""One command:  python -m nodes up --workspace <repo> --package <repo>/src/pkg

Starts FalkorDB in Docker if nothing answers on the DB port (pinned image digest), loads the package's code graph,
then serves MCP on :8780 and the web view on :8781. Also:
  python -m nodes replay-export <runs/test1> <task, e.g. t42_d0>   write a HiddenBench graph-vs-chat replay for /replay
"""
import argparse
import json
import re
import subprocess
import time
from pathlib import Path

from falkordb import FalkorDB

from . import core, server

IMAGE = "falkordb/falkordb@sha256:b0bdcac2cbaff989e77cb0610439b7a9289e86de033ccbbd4368b66fb680ae7a"


def ensure_db(port: int) -> None:
    def up():
        try:
            return FalkorDB(host="localhost", port=port).connection.ping()
        except Exception:  # noqa: BLE001
            return False
    if up():
        return
    name = f"nodes-falkordb-{port}"
    if subprocess.run(["docker", "start", name], capture_output=True).returncode != 0:
        subprocess.run(["docker", "run", "-d", "--name", name, "-p", f"127.0.0.1:{port}:6379", IMAGE], check=True)
    for _ in range(30):
        if up():
            return
        time.sleep(1)
    raise SystemExit(f"FalkorDB did not come up on port {port}")


def replay_export(runs: Path, task: str) -> Path:
    out = {"task": task}
    for cond in ("graph", "placebo"):
        r = json.loads((runs / cond / f"{task}.json").read_text(encoding="utf-8"))
        out[cond] = {k: r[k] for k in ("scenario", "correct_answer", "initial_votes", "final_votes",
                                       "discussion_transcript", "fact_assignments")}
        if cond == "graph":
            hidden = r["fact_assignments"]
            out[cond]["posts"] = r["board"]["posts"]
            out[cond]["tool_calls"] = [{k: c.get(k) for k in ("t", "agent", "tool", "fact_number")}
                                       for c in r["board"]["tool_calls"]]
            out[cond]["hidden_holders"] = {a["agent"]: a["assigned_hidden_information"] for a in hidden}
    dest = server.WEB / "replays" / f"{task}.json"
    dest.parent.mkdir(exist_ok=True)
    dest.write_text(json.dumps(out, indent=1), encoding="utf-8")
    return dest


def main():
    ap = argparse.ArgumentParser(prog="python -m nodes")
    sub = ap.add_subparsers(dest="cmd", required=True)
    u = sub.add_parser("up")
    u.add_argument("--workspace", type=Path, default=Path.cwd())
    u.add_argument("--package", type=Path, help="Python package dir to graph (default: the workspace)")
    u.add_argument("--team", help="graph name (default: nodes_<workspace name>)")
    u.add_argument("--db-port", type=int, default=6379)
    u.add_argument("--port", type=int, default=8780)
    u.add_argument("--web-port", type=int, default=8781)
    u.add_argument("--keep", action="store_true", help="keep the existing team graph (plans, claims, board)")
    u.add_argument("--stdio", action="store_true", help="serve MCP over stdio (agent name from NODES_AGENT)")
    r = sub.add_parser("replay-export")
    r.add_argument("runs", type=Path)
    r.add_argument("task")
    a = ap.parse_args()
    if a.cmd == "replay-export":
        print(replay_export(a.runs, a.task))
        return
    ws = a.workspace.resolve()
    team = a.team or "nodes_" + re.sub(r"[^A-Za-z0-9_]", "_", ws.name)
    ensure_db(a.db_port)
    g = FalkorDB(host="localhost", port=a.db_port).select_graph(team)
    if not a.keep:
        t0 = time.perf_counter()
        stats = core.load_repo(g, (a.package or ws).resolve())
        if not a.stdio:
            print(f"loaded {stats['functions']} functions, {stats['call_edges']} call edges in "
                  f"{time.perf_counter() - t0:.1f} s into graph {team}")
    if not a.stdio:
        print(f"MCP:  http://127.0.0.1:{a.port}/mcp   web: http://127.0.0.1:{a.web_port}/")
        print(f'add an agent:  claude mcp add --transport http nodes http://127.0.0.1:{a.port}/mcp --header "X-Agent: alice"')
    log_dir = ws / ".nodes"
    log_dir.mkdir(exist_ok=True)
    server.run(team, ws, db_port=a.db_port, port=a.port, web_port=a.web_port, log_dir=log_dir,
               transport="stdio" if a.stdio else "streamable-http")


if __name__ == "__main__":
    main()
