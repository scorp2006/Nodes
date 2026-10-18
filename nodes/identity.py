"""Who is calling, and which workspace: from Claude Code hook inputs (session_id, agent_id, agent_type, cwd) or a
bearer token. Hook-based identity needs no token: the PreToolUse hook stamps the caller into every Nodes MCP call
(`updatedInput`), and the server routes it to the workspace whose repository contains `cwd` (worktrees included).
Calls from repositories that were never `nodes init`-ed are ignored and nothing is stored.
"""
from __future__ import annotations

import functools
import json
import os
import subprocess

from . import ops


def _norm(p: str) -> str:
    return os.path.normcase(os.path.normpath(p)) if p else ""


@functools.lru_cache(maxsize=512)
def git_common_dir(cwd: str) -> str:
    """The repository's shared .git dir (the same for every worktree of a repo)."""
    try:
        out = subprocess.run(["git", "-C", cwd, "rev-parse", "--path-format=absolute", "--git-common-dir"],
                             capture_output=True, text=True, timeout=5)
        return _norm(out.stdout.strip()) if out.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


@functools.lru_cache(maxsize=512)
def branch_of(cwd: str) -> str:
    """The checked-out branch, used to name a session; '' on the default branch or a detached HEAD."""
    try:
        out = subprocess.run(["git", "-C", cwd, "rev-parse", "--abbrev-ref", "HEAD"], capture_output=True, text=True,
                             timeout=5).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return "" if out in ("", "HEAD", "main", "master") else out


def workspace_for_cwd(r, cwd: str) -> str | None:
    common = git_common_dir(cwd) if cwd else ""
    if not common:
        return None
    for ws, raw in r.hgetall("nodes:workspaces").items():
        info = json.loads(raw)
        if _norm(info.get("git_common_dir", "")) == common:
            return ws.decode() if isinstance(ws, bytes) else ws
    return None


def session_for_hook(reg, body: dict) -> dict | None:
    """Session for a hook call (or a stamped MCP call): main session `cc:<session_id>`, subagent
    `cc:<session_id>:<agent_id>`. None if the repo isn't initialized (nothing is stored)."""
    sid, cwd = body.get("session_id") or "", body.get("cwd") or ""
    ws = workspace_for_cwd(reg.r, cwd)
    if not ws or not sid:
        return None
    agent_id, agent_type = body.get("agent_id") or "", body.get("agent_type") or ""
    uid = f"cc:{sid}" + (f":{agent_id}" if agent_id else "")
    main = branch_of(cwd) or "main-" + sid[:6]  # a session in worktree branch `alice` shows as alice
    name = f"{agent_type or 'agent'}-{sid[:6]}-{agent_id[:4]}" if agent_id else main
    store = reg.store(ws)
    # Session.principal is MANDATORY and FalkorDB checks it when the node is created (before ON CREATE SET),
    # so each Session is created with all its properties at once.
    store.q("identity.session", """
        MERGE (p:Principal {uid: $pid}) ON CREATE SET p.kind = 'agent', p.name = $pname
        WITH p OPTIONAL MATCH (m0:Session {uid: $parent})
        FOREACH (_ IN CASE WHEN $parent <> '' AND m0 IS NULL THEN [1] ELSE [] END |
            CREATE (p)-[:RUNS]->(:Session {uid: $parent, principal: $pid, name: $pname, status: 'active',
                    started_at: $now, task: '', client: 'claude-code', token_hash: '', worktree: $cwd}))
        WITH p OPTIONAL MATCH (s0:Session {uid: $uid})
        FOREACH (_ IN CASE WHEN s0 IS NULL THEN [1] ELSE [] END |
            CREATE (p)-[:RUNS]->(:Session {uid: $uid, principal: $pid, name: $name, status: 'active', started_at: $now,
                    task: '', client: 'claude-code', token_hash: '', agent_type: $atype, worktree: $cwd}))
        WITH p MATCH (s:Session {uid: $uid}) SET s.last_heartbeat = $now
        WITH s OPTIONAL MATCH (m:Session {uid: $parent})
        FOREACH (_ IN CASE WHEN m IS NULL THEN [] ELSE [1] END | MERGE (m)-[:SPAWNED]->(s))""",
            {"pid": f"claude-session:{sid}", "pname": main, "uid": uid, "name": name, "now": ops.now_ms(),
             "atype": agent_type, "cwd": cwd, "parent": f"cc:{sid}" if agent_id else ""})
    return {"session": uid, "name": name, "ws": ws}
