"""Claude Code hook endpoints and the dashboard API, served by the same app as MCP.

Hook mapping (checked against code.claude.com/docs/en/hooks):
  SessionStart  command hook (http isn't allowed there) that POSTs stdin to /hooks/session-start; we return
                hookSpecificOutput.additionalContext: the team protocol + this agent's context packet
  PreToolUse    http: Edit/Write/MultiEdit -> before_acting on the units the edit touches;
                proceed/handoff -> additionalContext only (normal permission flow still applies);
                defer/escalate -> permissionDecision "deny" + reason (shown to the agent).
                (Claude Code's own "defer" means "suspend a headless run"; it is NOT our defer.)
                Bash `git merge|pull` -> GRAPH.COPY snapshot first (dry-run / replay point)
  PostToolUse   http: re-index the edited file, renew the agent's leases there, fencing check on the epoch
  Stop          http: heartbeat only (Stop fires after every turn; releasing here would drop ownership mid-task)
  SessionEnd    http: release the session's leases
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response

from . import indexer, ops

PROTOCOL = """You are {name} on a team of coding agents coordinated by Nodes (a shared FalkorDB graph).
- Before you start, call declare_intent with the functions/files you plan to change.
- Every Edit/Write is gated automatically. If the gate says DEFER, a teammate owns that code: work on something
  else, or call status(wait_seconds=60) to be told when it frees up. If it says HANDOFF, merge the named commit
  first and build on top. If it says ESCALATE, stop and ask the user.
- When your work is committed, call coordinate(op='done', text='<commit sha>') so teammates can build on it.
- Share facts teammates need with post_fact, citing the file they come from."""


def _auth(reg, request: Request) -> dict | None:
    h = request.headers.get("authorization", "")
    return reg.session(h[7:].strip()) if h.lower().startswith("bearer ") else None


def _lines_of(text: str, needle: str) -> set[int]:
    i = text.find(needle) if needle else -1
    if i < 0:
        return set()
    start = text.count("\n", 0, i) + 1
    return set(range(start, start + needle.count("\n") + 1))


def _session_row(store, sid: str) -> dict:
    rows = store.q("hook.session", "MATCH (s:Session {uid: $s}) RETURN s.worktree, s.name", {"s": sid}, ro=True)
    return {"worktree": rows[0][0] if rows else "", "name": rows[0][1] if rows else ""}


def edit_targets(tool: str, ti: dict, worktree: Path) -> tuple[str | None, set[int], bool]:
    """(repo-relative path, lines touched, file_exists) for an Edit/Write/MultiEdit call."""
    fp = ti.get("file_path") or ""
    try:
        rel = Path(fp).resolve().relative_to(worktree.resolve()).as_posix()
    except ValueError:
        return None, set(), False
    p = worktree / rel
    if not p.exists():
        return rel, set(), False
    text = p.read_text(encoding="utf-8", errors="replace")
    if tool == "Write":
        return rel, set(range(1, text.count("\n") + 2)), True
    olds = [e.get("old_string", "") for e in ti.get("edits", [])] if tool == "MultiEdit" else [ti.get("old_string", "")]
    lines = set().union(*(_lines_of(text, o) for o in olds)) if olds else set()
    return rel, lines or set(range(1, text.count("\n") + 2)), True


def register(server, reg) -> None:
    from .server import traced_call  # local import: server imports this module

    def hook(path, handler):
        async def endpoint(request: Request) -> Response:
            sess = _auth(reg, request)
            body = await request.json() if request.headers.get("content-length", "0") != "0" else {}
            if not sess:
                return JSONResponse({})  # unknown caller: not on the team, never block
            store = reg.store(sess["ws"])
            out = traced_call(reg, sess["ws"], sess, "hook", path, body, lambda: handler(store, sess, body))
            return JSONResponse(out if isinstance(out, dict) and "error" not in out else {})
        server.custom_route(f"/hooks/{path}", methods=["POST"])(endpoint)

    def session_start(store, sess, body):
        info = _session_row(store, sess["session"])
        store.q("hook.session_start", "MATCH (s:Session {uid: $s}) SET s.claude_session = $c, s.last_heartbeat = $now, "
                "s.worktree = CASE WHEN s.worktree = '' THEN $cwd ELSE s.worktree END",
                {"s": sess["session"], "c": body.get("session_id", ""), "now": ops.now_ms(), "cwd": body.get("cwd", "")})
        packet = ops.context_for(store, sess, "", 800)["data"]
        others = [u for u in packet["units"] if u["owner"] and u["owner"] != sess["name"]]
        ctx = (PROTOCOL.format(name=sess["name"]) + "\n\nTeam state now: "
               + json.dumps({"you_own": packet["you_own"], "owned_by_others": others[:15],
                             "facts": packet["facts"][:10]}))
        return {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": ctx}}

    def pre_tool_use(store, sess, body):
        tool, ti = body.get("tool_name", ""), body.get("tool_input") or {}
        if tool == "Bash":
            cmd = ti.get("command", "")
            if any(k in cmd for k in ("git merge", "git pull")):
                snap = store.snapshot(f"merge-{int(time.time())}")
                store.event("merge-snapshot", sess["name"], snapshot=snap, command=cmd[:200])
                return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext":
                        f"Nodes: graph snapshot {snap} taken before this merge (dry-run / replay point)."}}
            return {}
        if tool not in ("Edit", "Write", "MultiEdit"):
            return {}
        wt = Path(_session_row(store, sess["session"])["worktree"] or body.get("cwd", "."))
        rel, lines, exists = edit_targets(tool, ti, wt)
        if not rel or not rel.endswith(".py"):
            return {}
        info = reg.workspace(sess["ws"])
        if exists and rel.startswith(info.get("package", "") + "/"):
            targets = [f"{rel}:{min(lines)}-{max(lines)}"]
        else:  # a new file, or one outside the indexed package: a file-level unit others can see and wait on
            uid = f"file:planned:{rel}"
            store.q("hook.planned", "MERGE (u:Unit {uid: $u}) ON CREATE SET u.kind = 'file', u.status = 'planned', "
                    "u.path = $p, u.lease_epoch = 0", {"u": uid, "p": rel})
            targets = [uid]
        r = ops.before_acting(store, sess, "edit", targets)["data"]
        note = f"Nodes gate: {r['decision'].upper()}: {r['reason']}. Path: {' '.join(r['path'])}"
        if r["decision"] in ("proceed", "handoff"):
            return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": note}}
        nxt = ("Work on something else, or call the status tool with wait_seconds=60 to be told when it frees up."
               if r["decision"] == "defer" else "Stop and ask the user to resolve this.")
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                       "permissionDecisionReason": f"{note}. Next: {nxt}"}}

    def post_tool_use(store, sess, body):
        tool, ti = body.get("tool_name", ""), body.get("tool_input") or {}
        if tool not in ("Edit", "Write", "MultiEdit"):
            return {}
        info = reg.workspace(sess["ws"])
        wt = Path(_session_row(store, sess["session"])["worktree"] or body.get("cwd", "."))
        rel, _, _ = edit_targets(tool, {"file_path": ti.get("file_path")}, wt)
        if not rel or not rel.endswith(".py") or not info:
            return {}
        try:  # incremental enough for small repos: re-scan the package from this agent's worktree
            indexer.index(store, wt, info["package"], info["repo"])
        except SyntaxError:
            pass
        text = (wt / rel).read_text(encoding="utf-8", errors="replace")
        news = [e.get("new_string", "") for e in ti.get("edits", [])] if tool == "MultiEdit" else [ti.get("new_string", "")]
        lines = set().union(*(_lines_of(text, n) for n in news if n)) if any(news) else set()
        units = indexer.units_for_lines(store, rel, lines) if lines else []
        now = ops.now_ms()  # fencing: a unit my edit just created is mine; one someone else holds is a stale write
        rows = store.q("hook.fence", """
            UNWIND $u AS id MATCH (u:Unit {uid: id})
            OPTIONAL MATCH (:Session {uid: $me})-[h:HELD]->(u)
            WITH u, max(h.epoch) AS mine, u.lease_owner AS owner
            WITH u, mine, owner, owner IS NULL AS free
            SET u.lease_owner = CASE WHEN free THEN $me ELSE u.lease_owner END,
                u.lease_epoch = CASE WHEN free THEN coalesce(u.lease_epoch, 0) + 1 ELSE u.lease_epoch END,
                u.lease_expires = CASE WHEN free OR owner = $me THEN $until ELSE u.lease_expires END
            WITH u, mine, owner, free
            MATCH (s:Session {uid: $me})
            FOREACH (_ IN CASE WHEN free THEN [1] ELSE [] END |
                CREATE (s)-[:HELD {epoch: u.lease_epoch, from: $now, until: $until}]->(u))
            RETURN u.uid, free OR (owner = $me AND u.lease_epoch = mine)""",
            {"u": units, "me": sess["session"], "now": now, "until": now + ops.LEASE_TTL_MS})
        stale = [u for u, ok in rows if not ok]
        if stale:
            store.event("stale-write", sess["name"], units=stale)
            return {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext":
                    f"Nodes: STALE_EPOCH: you edited {', '.join(stale)} without holding its current lease. "
                    "Call before_acting on it now; if a teammate owns it, revert your change."}}
        return {}

    def stop(store, sess, body):
        store.q("hook.heartbeat", "MATCH (s:Session {uid: $s}) SET s.last_heartbeat = $now",
                {"s": sess["session"], "now": ops.now_ms()})
        return {}

    def session_end(store, sess, body):
        # A finished session keeps its leases: the next agent to touch those units gets a HANDOFF (merge my commit
        # first). Releasing them here would let teammates proceed without ever merging the finished work.
        rel = store.q("hook.session_end", """
            MATCH (s:Session {uid: $s})
            WITH s, s.status = 'done' AS done SET s.status = CASE WHEN done THEN 'done' ELSE 'ended' END
            WITH s, done OPTIONAL MATCH (u:Unit {lease_owner: $s}) WHERE NOT done
            SET u.lease_owner = NULL, u.lease_expires = NULL
            RETURN count(u)""", {"s": sess["session"]})
        store.event("session-end", sess["name"], released=rel[0][0] if rel else 0)
        return {}

    for path, fn in (("session-start", session_start), ("pre-tool-use", pre_tool_use),
                     ("post-tool-use", post_tool_use), ("stop", stop), ("session-end", session_end)):
        hook(path, fn)

    # ------------------------------------------------------------------ dashboard -------
    async def index(request: Request) -> Response:
        return FileResponse(Path(__file__).with_name("web") / "index.html")

    async def state(request: Request) -> Response:
        graphs = set(reg.r.execute_command("GRAPH.LIST"))
        names = sorted(n for n in (k.decode() if isinstance(k, bytes) else k for k in reg.r.hkeys("nodes:workspaces"))
                       if f"ws:{n}" in graphs)  # skip registered workspaces whose graph is gone
        ws = request.query_params.get("ws") or (names[0] if names else "")
        if not ws or ws not in names:
            return JSONResponse({"workspace": ws, "workspaces": names, "sessions": [], "decisions": [], "facts": [],
                                 "waits": [], "events": [], "latency": {}})
        st = reg.store(ws)
        now = ops.now_ms()
        sessions = [{"name": n, "status": s, "commit": c, "task": t, "owns": owns} for n, s, c, t, owns in st.q(
            "dash.sessions", """
            MATCH (s:Session) OPTIONAL MATCH (u:Unit {lease_owner: s.uid}) WHERE u.lease_expires >= $now
            RETURN s.name, s.status, s.commit, s.task, collect({uid: u.uid, epoch: u.lease_epoch, kind: u.kind})
            ORDER BY s.name""", {"now": now}, ro=True)]
        decisions = [{"agent": a, "verdict": v, "reason": r, "path": p, "at": at, "snapshot": sn} for a, v, r, p, at, sn
                     in st.q("dash.decisions", "MATCH (s:Session)-[:MADE]->(d:Decision) RETURN s.name, d.verdict, "
                             "d.reason, d.path, d.at, d.snapshot ORDER BY d.at DESC LIMIT 40", ro=True)]
        facts = [{"by": a, "text": t, "status": s, "source": src} for a, t, s, src in st.q(
            "dash.facts", "MATCH (s:Session)-[a:ASSERTED]->(f:Fact) RETURN s.name, f.text, f.status, f.source_uid "
            "ORDER BY a.seq DESC LIMIT 30", ro=True)]
        waits = [{"from": a, "to": b, "unit": u} for a, b, u in st.q(
            "dash.waits", "MATCH (a:Session)-[w:WAITS_FOR]->(b:Session) RETURN a.name, b.name, w.unit", ro=True)]
        return JSONResponse({"workspace": ws, "workspaces": names,
                             "sessions": sessions, "decisions": decisions, "facts": facts, "waits": waits,
                             "events": st.events(30), "latency": st.latency_report()})

    async def http_gate(request: Request) -> Response:  # for clients without MCP or hooks (git hooks, scripts)
        sess = _auth(reg, request)
        if not sess:
            return JSONResponse(ops.error("INVALID_ARGS", "missing or unknown bearer token", "Run nodes join."), 401)
        body = await request.json()
        store = reg.store(sess["ws"])
        out = traced_call(reg, sess["ws"], sess, "http", "before_acting", body, lambda: ops.before_acting(
            store, sess, str(body.get("action", "edit")), [str(t) for t in body.get("targets", [])][:50]))
        return JSONResponse(out)

    server.custom_route("/", methods=["GET"])(index)
    server.custom_route("/api/state", methods=["GET"])(state)
    server.custom_route("/api/before_acting", methods=["POST"])(http_gate)
