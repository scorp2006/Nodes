"""Nodes CLI.

  python -m nodes up    [--profile nodes|harness-board|harness-coop] [--port 8780] [--db-port 6380]
  python -m nodes init  --repo PATH --package src/pkg [--workspace NAME] [--team-size 2]
  python -m nodes join  --workspace NAME --agent alice [--task T] [--worktree PATH --branch B]
  python -m nodes watch --workspace NAME
`up` starts FalkorDB (its own container, pinned digest) if nothing answers, then serves MCP + hooks + dashboard.
`init` indexes the repo and writes .mcp.json + .claude/settings.json (MCP server and hooks) into it.
`join` mints an agent's bearer token; with --worktree it creates the agent's git worktree and copies the config in.
"""
import argparse
import hashlib
import json
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

from falkordb import FalkorDB

from . import indexer, ops
from .db import Store

IMAGE = "falkordb/falkordb@sha256:b0bdcac2cbaff989e77cb0610439b7a9289e86de033ccbbd4368b66fb680ae7a"  # v4.22.1
HERE = Path(__file__).resolve().parent


def ensure_db(host: str, port: int) -> None:
    def up():
        try:
            return FalkorDB(host=host, port=port).connection.ping()
        except Exception:  # noqa: BLE001
            return False
    if up():
        return
    if host not in ("localhost", "127.0.0.1"):
        for _ in range(30):
            if up():
                return
            time.sleep(1)
        raise SystemExit(f"FalkorDB at {host}:{port} is not reachable")
    name = f"nodes-falkordb-{port}" if port != 6380 else "nodes-falkordb"
    if subprocess.run(["docker", "start", name], capture_output=True).returncode != 0:
        subprocess.run(["docker", "run", "-d", "--name", name, "-p", f"127.0.0.1:{port}:6379", IMAGE], check=True)
    for _ in range(30):
        if up():
            return
        time.sleep(1)
    raise SystemExit(f"FalkorDB did not come up on port {port}")


def fingerprint() -> str:
    return hashlib.sha256(b"".join(p.read_bytes() for p in sorted(HERE.glob("*.py")))).hexdigest()[:16]


def cmd_up(a):
    ensure_db(a.db_host, a.db_port)
    stamp = Path(a.trace_dir) / f"server_{a.port}.json"
    with socket.socket() as s:  # stale-server protection: never reuse a server started from other code
        if s.connect_ex(("127.0.0.1", a.port)) == 0:
            old = json.loads(stamp.read_text()) if stamp.exists() else {}
            raise SystemExit(f"port {a.port} is busy (server code {old.get('code', '?')}, this code {fingerprint()}). "
                             "Stop it first.")
    stamp.parent.mkdir(parents=True, exist_ok=True)
    stamp.write_text(json.dumps({"code": fingerprint(), "profile": a.profile, "t": time.time()}))
    from .server import Registry, run
    print(f"Nodes {a.profile} on http://127.0.0.1:{a.port}  (MCP /mcp, dashboard /, hooks /hooks/*); "
          f"FalkorDB {a.db_host}:{a.db_port}", flush=True)
    run(Registry(a.db_host, a.db_port, Path(a.trace_dir)), a.profile, a.host, a.port)


def config_files(port: int) -> tuple[dict, dict]:
    base = f"http://127.0.0.1:{port}"
    mcp = {"mcpServers": {"nodes": {"type": "http", "url": f"{base}/mcp",
                                    "headers": {"Authorization": "Bearer ${NODES_TOKEN}"}}}}
    auth = {"headers": {"Authorization": "Bearer $NODES_TOKEN"}, "allowedEnvVars": ["NODES_TOKEN"]}
    start = (f'curl -s -X POST -H "Authorization: Bearer $NODES_TOKEN" -H "Content-Type: application/json" '
             f'--data-binary @- {base}/hooks/session-start')
    settings = {
        "enabledMcpjsonServers": ["nodes"],
        "permissions": {"allow": ["mcp__nodes__*"]},
        "hooks": {
            "SessionStart": [{"hooks": [{"type": "command", "command": start, "timeout": 15}]}],
            "PreToolUse": [{"matcher": "Edit|Write|MultiEdit|Bash", "hooks": [
                {"type": "http", "url": f"{base}/hooks/pre-tool-use", "timeout": 15, "onFailure": "continue", **auth}]}],
            "PostToolUse": [{"matcher": "Edit|Write|MultiEdit", "hooks": [
                {"type": "http", "url": f"{base}/hooks/post-tool-use", "timeout": 30, **auth}]}],
            "Stop": [{"hooks": [{"type": "http", "url": f"{base}/hooks/stop", "timeout": 5, **auth}]}],
            "SessionEnd": [{"hooks": [{"type": "http", "url": f"{base}/hooks/session-end", "timeout": 5, **auth}]}],
        },
    }
    return mcp, settings


def write_config(target: Path, port: int) -> None:
    mcp, settings = config_files(port)
    (target / ".mcp.json").write_text(json.dumps(mcp, indent=2), encoding="utf-8")
    (target / ".claude").mkdir(exist_ok=True)
    (target / ".claude" / "settings.json").write_text(json.dumps(settings, indent=2), encoding="utf-8")


def cmd_init(a):
    ensure_db(a.db_host, a.db_port)
    repo = Path(a.repo).resolve()
    ws = a.workspace or repo.name
    st = Store(ws, a.db_host, a.db_port)
    st.reset()
    st.setup()
    stats = indexer.index(st, repo, a.package, ws)
    st.r.hset("nodes:workspaces", ws, json.dumps({"root": str(repo), "repo": ws, "package": a.package,
                                                    "team_size": a.team_size}))
    write_config(repo, a.port)
    print(f"workspace {ws}: {stats}; wrote {repo / '.mcp.json'} and {repo / '.claude' / 'settings.json'}")


def cmd_join(a):
    st = Store(a.workspace, a.db_host, a.db_port)
    info = json.loads(st.r.hget("nodes:workspaces", a.workspace) or "{}")
    if not info:
        raise SystemExit(f"unknown workspace {a.workspace}; run nodes init first")
    wt = ""
    if a.worktree:
        wt_path = Path(a.worktree).resolve()
        if not wt_path.exists():
            subprocess.run(["git", "-C", info["root"], "worktree", "add", "-q", "-b", a.branch or a.agent,
                            str(wt_path)], check=True)
        for f in (".mcp.json", ".claude/settings.json"):
            (wt_path / f).parent.mkdir(exist_ok=True)
            shutil.copyfile(Path(info["root"]) / f, wt_path / f)
        wt = str(wt_path)
    token, sid = ops.join(st, a.agent, a.task, a.client, wt, a.branch or "")
    print(token if a.quiet else f"agent {a.agent} joined {a.workspace} as {sid}\nNODES_TOKEN={token}")


def cmd_watch(a):
    r = Store(a.workspace, a.db_host, a.db_port).r
    last = "$"
    print(f"watching ws:{a.workspace}:events (Ctrl+C to stop)", flush=True)
    while True:
        for _, entries in r.xread({f"ws:{a.workspace}:events": last}, block=5000) or []:
            for eid, fields in entries:
                last = eid
                e = json.loads(fields[b"e"])
                extra = {k: v for k, v in e.items() if k not in ("kind", "agent", "t", "notify")}
                print(f"{time.strftime('%H:%M:%S', time.localtime(e['t']))} {e['agent']:>10} {e['kind']:<14} "
                      f"{json.dumps(extra)[:160]}", flush=True)


def main():
    ap = argparse.ArgumentParser(prog="nodes")
    ap.add_argument("--db-host", default="localhost")
    ap.add_argument("--db-port", type=int, default=6380)
    sub = ap.add_subparsers(dest="cmd", required=True)
    u = sub.add_parser("up")
    u.add_argument("--profile", default="nodes", choices=["nodes", "harness-board", "harness-coop"])
    u.add_argument("--port", type=int, default=8780)
    u.add_argument("--host", default="127.0.0.1")
    u.add_argument("--trace-dir", default=".nodes/traces")
    i = sub.add_parser("init")
    i.add_argument("--repo", required=True)
    i.add_argument("--package", required=True, help="package dir relative to the repo, e.g. src/shop")
    i.add_argument("--workspace")
    i.add_argument("--team-size", type=int, default=2)
    i.add_argument("--port", type=int, default=8780)
    j = sub.add_parser("join")
    j.add_argument("--workspace", required=True)
    j.add_argument("--agent", required=True)
    j.add_argument("--task", default="")
    j.add_argument("--client", default="claude-code")
    j.add_argument("--worktree")
    j.add_argument("--branch")
    j.add_argument("--quiet", action="store_true", help="print only the token")
    w = sub.add_parser("watch")
    w.add_argument("--workspace", required=True)
    a = ap.parse_args()
    {"up": cmd_up, "init": cmd_init, "join": cmd_join, "watch": cmd_watch}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
