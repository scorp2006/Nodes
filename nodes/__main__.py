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
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

from falkordb import FalkorDB

from . import gitimport, indexer, ops
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
        browser = ["-p", "127.0.0.1:3001:3000"] if port == 6380 else []  # FalkorDB Browser, for the raw graph
        subprocess.run(["docker", "run", "-d", "--name", name, "-p", f"127.0.0.1:{port}:6379", *browser, IMAGE], check=True)
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
    lan = a.host not in ("127.0.0.1", "localhost")
    token = os.environ.get("NODES_SERVER_TOKEN", "")
    if lan and len(token) < 24:
        raise SystemExit("Binding beyond localhost requires NODES_SERVER_TOKEN (24+ chars); every endpoint, "
                         "including the dashboard, will require it.")
    stamp.parent.mkdir(parents=True, exist_ok=True)
    stamp.write_text(json.dumps({"code": fingerprint(), "profile": a.profile, "t": time.time()}))
    from .server import Registry, run
    print(f"Nodes {a.profile} on http://{a.host}:{a.port}  (MCP /mcp, dashboard /, hooks /hooks/*); "
          f"FalkorDB {a.db_host}:{a.db_port}" + ("; LAN mode: token required everywhere" if lan else ""), flush=True)
    run(Registry(a.db_host, a.db_port, Path(a.trace_dir)), a.profile, a.host, a.port, server_token=token if lan else "")


def config_files(port: int, lan: bool = False, fail_mode: str = "closed") -> tuple[dict, dict]:
    """MCP + hook config. Local mode needs no token: hooks carry session/agent ids and the server routes by the git
    root of cwd. LAN mode adds the server token (from NODES_SERVER_TOKEN) to every request.
    fail_mode 'closed' (default): if the server can't be reached, the gated tools (Edit/Write/MultiEdit/Bash and the
    Nodes tools) are blocked (`onFailure: block`); 'open': they go ahead ungated (Claude Code's default)."""
    base = f"http://127.0.0.1:{port}"
    auth = {"headers": {"Authorization": "Bearer $NODES_SERVER_TOKEN"}, "allowedEnvVars": ["NODES_SERVER_TOKEN"]} \
        if lan else {}
    mcp = {"mcpServers": {"nodes": {"type": "http", "url": f"{base}/mcp",
                                    **({"headers": {"Authorization": "Bearer ${NODES_SERVER_TOKEN}"}} if lan else {})}}}
    start = ('curl -s -X POST -H "Content-Type: application/json" '
             + ('-H "Authorization: Bearer $NODES_SERVER_TOKEN" ' if lan else "")
             + f'--data-binary @- {base}/hooks/session-start')

    on_fail = "block" if fail_mode == "closed" else "continue"

    def http(path, timeout, **extra):
        return {"type": "http", "url": f"{base}/hooks/{path}", "timeout": timeout, **auth, **extra}
    settings = {
        "enabledMcpjsonServers": ["nodes"],
        "permissions": {"allow": ["mcp__nodes__*"]},
        "hooks": {
            "SessionStart": [{"hooks": [{"type": "command", "command": start, "timeout": 15}]}],
            # Nodes tools first: the hook stamps the caller (session, subagent, cwd) into the call
            "PreToolUse": [{"matcher": "mcp__nodes__.*", "hooks": [http("pre-tool-use", 10, onFailure=on_fail)]},
                           {"matcher": "Edit|Write|MultiEdit|Bash",
                            "hooks": [http("pre-tool-use", 15, onFailure=on_fail)]}],
            "PostToolUse": [{"matcher": "Edit|Write|MultiEdit", "hooks": [http("post-tool-use", 30)]}],
            "SubagentStart": [{"hooks": [http("subagent-start", 5)]}],
            "SubagentStop": [{"hooks": [http("subagent-stop", 5)]}],
            "Stop": [{"hooks": [http("stop", 5)]}],
            "SessionEnd": [{"hooks": [http("session-end", 5)]}],
        },
    }
    return mcp, settings


def plugin_files(port: int = 8780) -> tuple[dict, dict]:
    """The Claude Code plugin (plugin/): the same hooks and MCP server as `nodes init`, except PreToolUse runs
    hooks/gate.sh, which fails closed only in repos that ran `nodes init` (a user-scope plugin sees every repo)."""
    mcp, settings = config_files(port, fail_mode="open")
    gate = {"type": "command", "command": 'bash "${CLAUDE_PLUGIN_ROOT}/hooks/gate.sh"', "timeout": 20}
    settings["hooks"]["PreToolUse"] = [{"matcher": m["matcher"], "hooks": [gate]} for m in settings["hooks"]["PreToolUse"]]
    return {"hooks": settings["hooks"]}, mcp


def write_config(target: Path, port: int, lan: bool = False, fail_mode: str = "closed") -> None:
    mcp, settings = config_files(port, lan, fail_mode)
    (target / ".mcp.json").write_text(json.dumps(mcp, indent=2), encoding="utf-8")
    (target / ".claude").mkdir(exist_ok=True)
    (target / ".claude" / "settings.json").write_text(json.dumps(settings, indent=2), encoding="utf-8")


def write_user_config(port: int, lan: bool = False, settings_path: Path | None = None, fail_mode: str = "closed") -> str:
    """User scope: merge the hooks into ~/.claude/settings.json and register the MCP server for every project.
    Sessions in repos that were never `nodes init`-ed are ignored by the server (nothing is stored)."""
    mcp, settings = config_files(port, lan, fail_mode)
    path = settings_path or Path.home() / ".claude" / "settings.json"
    cur = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    cur.setdefault("hooks", {}).update(settings["hooks"])
    allow = cur.setdefault("permissions", {}).setdefault("allow", [])
    if "mcp__nodes__*" not in allow:
        allow.append("mcp__nodes__*")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cur, indent=2), encoding="utf-8")
    if settings_path is None:
        srv = mcp["mcpServers"]["nodes"]
        subprocess.run(["claude", "mcp", "add", "--scope", "user", "--transport", "http", "nodes", srv["url"]]
                       + sum((["--header", f"{k}: {v}"] for k, v in srv.get("headers", {}).items()), []), check=False)
    return str(path)


def cmd_init(a):
    ensure_db(a.db_host, a.db_port)
    repo = Path(a.repo).resolve()
    ws = a.workspace or repo.name
    st = Store(ws, a.db_host, a.db_port)
    if a.fresh:
        st.reset()
        st.r.hdel("nodes:git", ws)
    st.setup()
    t0 = time.perf_counter()
    stats = indexer.index(st, repo, a.package, ws)
    units = stats.pop("_units")
    gstats = gitimport.import_git(st, repo, units) if not a.no_git else {"git": "skipped"}
    common = subprocess.run(["git", "-C", str(repo), "rev-parse", "--path-format=absolute", "--git-common-dir"],
                            capture_output=True, text=True).stdout.strip()
    st.r.hset("nodes:workspaces", ws, json.dumps({"root": str(repo), "repo": ws, "package": a.package,
                                                    "team_size": a.team_size, "git_common_dir": common}))
    (repo / ".nodes").mkdir(exist_ok=True)  # agent tokens for clients without hooks; Claude Code needs none
    tok_file = repo / ".nodes" / "tokens.json"
    if not tok_file.exists():
        toks = {f"agent{i + 1}": ops.join(st, f"agent{i + 1}", client="token")[0] for i in range(a.agents)}
        tok_file.write_text(json.dumps(toks, indent=1), encoding="utf-8")
    if common:  # keep local Nodes files out of git
        excl = Path(common) / "info" / "exclude"
        excl.parent.mkdir(exist_ok=True)
        have = excl.read_text(encoding="utf-8") if excl.exists() else ""
        add = [x for x in (".nodes/", ".mcp.json", ".claude/settings.json") if x not in have.split()]
        if add:
            excl.write_text(have.rstrip("\n") + "\n" + "\n".join(add) + "\n", encoding="utf-8")
        # marker in the shared git dir (every worktree sees it): the plugin's gate fails closed only here
        (Path(common) / "nodes.json").write_text(json.dumps({"workspace": ws, "port": a.port,
                                                             "fail_mode": a.fail_mode}), encoding="utf-8")
    where = (write_user_config(a.port, a.lan, fail_mode=a.fail_mode) if a.scope == "user"
             else (write_config(repo, a.port, a.lan, a.fail_mode) or str(repo)))
    print(json.dumps({"workspace": ws, "seconds": round(time.perf_counter() - t0, 2), "index": stats, "git": gstats,
                      "config": f"{a.scope} scope -> {where}", "tokens": str(tok_file)}, indent=1))


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


def cmd_import_sessions(a):
    """Opt-in (never automatic): past Claude Code transcripts of this repo -> rationale facts at 'extracted' trust."""
    from .sessions_import import import_sessions
    st = Store(a.workspace, a.db_host, a.db_port)
    info = json.loads(st.r.hget("nodes:workspaces", a.workspace) or "{}")
    if not info.get("git_common_dir"):
        raise SystemExit(f"unknown workspace {a.workspace}; run nodes init first")
    print(json.dumps(import_sessions(st, info["git_common_dir"])))


def cmd_watch(a):
    r = Store(a.workspace, a.db_host, a.db_port).r
    last = "$"
    print(f"watching ws:{a.workspace}:events (Ctrl+C to stop)", flush=True)
    while True:
        for _, entries in r.xread({f"ws:{a.workspace}:events": last}, block=5000) or []:
            for eid, fields in entries:
                last = eid
                e = json.loads(fields.get("e") or fields.get(b"e"))
                extra = {k: v for k, v in e.items() if k not in ("kind", "agent", "t", "notify")}
                print(f"{time.strftime('%H:%M:%S', time.localtime(e['t']))} {e['agent']:>10} {e['kind']:<14} "
                      f"{json.dumps(extra)[:160]}", flush=True)


def cmd_replay(a):
    """Re-emit a recorded workspace's events into <ws>-replay at their original pace (no agents, no LLM).
    The replay graph is a copy of the source graph's final state; only the event stream is time-shifted."""
    src = Store(a.workspace, a.db_host, a.db_port)
    dst_name = a.into or f"{a.workspace}-replay"
    r = src.r
    rows = r.xrange(f"ws:{a.workspace}:events")
    if not rows:
        raise SystemExit(f"no recorded events in {a.workspace}")
    r.delete(f"ws:{dst_name}:events")
    if r.exists(f"ws:{dst_name}"):
        r.execute_command("GRAPH.DELETE", f"ws:{dst_name}")
    r.execute_command("GRAPH.COPY", f"ws:{a.workspace}", f"ws:{dst_name}")
    info = json.loads(r.hget("nodes:workspaces", a.workspace) or "{}")
    r.hset("nodes:workspaces", dst_name, json.dumps(info | {"replay_of": a.workspace}))
    print(f"replaying {len(rows)} events from {a.workspace} into {dst_name} at {a.speed}x "
          f"(open /?ws={dst_name})", flush=True)
    time.sleep(a.wait)
    prev = None
    for _, f in rows:
        e = json.loads(f.get("e") or f.get(b"e"))
        if prev is not None:
            time.sleep(min(max(e["t"] - prev, 0) / a.speed, a.max_gap))
        prev = e["t"]
        r.xadd(f"ws:{dst_name}:events", {"e": json.dumps(e | {"t": time.time(), "recorded_t": e["t"]})})
    print("replay done", flush=True)


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
    i.add_argument("--repo", default=".")
    i.add_argument("--package", help="Python package dir for the precise resolver (default: auto-detect)")
    i.add_argument("--scope", choices=["project", "user"], default="project", help="where to write MCP + hook config")
    i.add_argument("--agents", type=int, default=2, help="agent tokens to mint for clients without hooks")
    i.add_argument("--fresh", action="store_true", help="wipe the workspace graph first (default: incremental)")
    i.add_argument("--no-git", action="store_true")
    i.add_argument("--fail-mode", choices=["closed", "open"], default="closed",
                   help="server unreachable: block gated tools (closed, default) or let them run ungated (open)")
    i.add_argument("--lan", action="store_true", help="config for a server bound beyond localhost (token required)")
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
    m = sub.add_parser("import-sessions", help="opt-in: rationale from past Claude Code transcripts (local only)")
    m.add_argument("--workspace", required=True)
    w = sub.add_parser("watch")
    w.add_argument("--workspace", required=True)
    p = sub.add_parser("replay", help="re-emit a recorded run's events at original timing into <ws>-replay")
    p.add_argument("--workspace", required=True)
    p.add_argument("--into", help="target workspace (default: <workspace>-replay)")
    p.add_argument("--speed", type=float, default=1.0)
    p.add_argument("--max-gap", type=float, default=3.0, help="cap idle gaps, in seconds")
    p.add_argument("--wait", type=float, default=0.0, help="seconds to wait before the first event (open the page)")
    a = ap.parse_args()
    {"up": cmd_up, "init": cmd_init, "join": cmd_join, "watch": cmd_watch,
     "import-sessions": cmd_import_sessions, "replay": cmd_replay}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
