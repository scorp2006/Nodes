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

from . import identity, indexer, ops
from .lang import EXT
from .shellwrites import write_targets

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


def bash_targets(cmd: str, cwd: Path, worktree: Path) -> tuple[list[tuple[str, set[int], bool]], bool]:
    """Files a Bash command writes, as (repo-relative path, lines, exists) like edit_targets; plus `opaque`."""
    paths, opaque = write_targets(cmd, cwd)
    out = []
    for p in paths:
        try:
            rel = (cwd / p).resolve().relative_to(worktree.resolve()).as_posix()
        except (ValueError, OSError):
            continue
        f = worktree / rel
        n = f.read_text(encoding="utf-8", errors="replace").count("\n") + 1 if f.is_file() else 0
        out.append((rel, set(range(1, n + 1)), f.is_file()))
    return out, opaque


def gate_targets(store, files) -> list[str]:
    """before_acting targets for written files in an indexed language: the units the lines cover, or a planned
    file-level unit (new files, files outside the index) that others can see and wait on."""
    targets = []
    for rel, lines, exists in files:
        if not rel or Path(rel).suffix.lower() not in EXT:
            continue
        indexed = store.q("hook.indexed", "MATCH (u:Unit {path: $p}) WHERE u.start IS NOT NULL RETURN count(u)",
                          {"p": rel}, ro=True)[0][0] if exists else 0
        if exists and indexed and lines:
            targets.append(f"{rel}:{min(lines)}-{max(lines)}")
        else:
            uid = f"file:planned:{rel}"
            store.q("hook.planned", "MERGE (u:Unit {uid: $u}) ON CREATE SET u.kind = 'file', u.status = 'planned', "
                    "u.path = $p, u.lease_epoch = 0", {"u": uid, "p": rel})
            targets.append(uid)
    return targets


def register(server, reg) -> None:
    from .server import traced_call  # local import: server imports this module

    def hook(path, handler):
        async def endpoint(request: Request) -> Response:
            body = await request.json() if request.headers.get("content-length", "0") != "0" else {}
            # identity: a bearer token (non-Claude clients), else the hook input routed by the git root of its cwd
            sess = _auth(reg, request) or identity.session_for_hook(reg, body)
            if not sess:
                return JSONResponse({})  # not an initialized repo / not on a team: store nothing, never block
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
        if tool.startswith("mcp__nodes__"):
            # stamp the caller (session, subagent, cwd) into the Nodes tool call, so the server knows which
            # session or subagent made it without any token (updatedInput replaces the whole input)
            caller = {k: body.get(k) or "" for k in ("session_id", "agent_id", "agent_type", "cwd")}
            return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow",
                                           "permissionDecisionReason": "Nodes tool, caller stamped",
                                           "updatedInput": {**ti, "caller": caller}}}
        wt = Path(_session_row(store, sess["session"])["worktree"] or body.get("cwd", "."))
        extra = ""
        if tool == "Bash":
            cmd = ti.get("command", "")
            if any(k in cmd for k in ("git merge", "git pull")):
                snap = store.snapshot(f"merge-{int(time.time())}")
                store.event("merge-snapshot", sess["name"], snapshot=snap, command=cmd[:200])
                extra = f"Nodes: graph snapshot {snap} taken before this merge (dry-run / replay point). "
            files = bash_targets(cmd, Path(body.get("cwd") or wt), wt)
            if files[1]:
                extra += ("Nodes: this script writes files it doesn't name, so they are not gated; use Edit/Write "
                          "for source files. ")
            files = files[0]
        elif tool in ("Edit", "Write", "MultiEdit"):
            files = [edit_targets(tool, ti, wt)]
        else:
            return {}
        targets = gate_targets(store, files)
        if not targets:
            return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": extra.strip()}} \
                if extra else {}
        r = ops.before_acting(store, sess, "edit", targets)["data"]
        note = extra + f"Nodes gate: {r['decision'].upper()}: {r['reason']}. Path: {' '.join(r['path'])}"
        if r["decision"] in ("proceed", "handoff"):
            if body.get("tool_use_id"):  # the write ticket: PostToolUse checks the epoch it was granted
                store.r.set(f"ws:{sess['ws']}:ticket:{sess['session']}:{body['tool_use_id']}",
                            json.dumps({"t": ops.now_ms(), "epochs": {x["uid"]: x["epoch"] for x in r["lease"]}}),
                            ex=3600)
            return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": note}}
        nxt = ("Work on something else, or call the status tool with wait_seconds=60 to be told when it frees up."
               if r["decision"] == "defer" else "Stop and ask the user to resolve this.")
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                       "permissionDecisionReason": f"{note}. Next: {nxt}"}}

    def post_tool_use(store, sess, body):
        tool, ti = body.get("tool_name", ""), body.get("tool_input") or {}
        info = reg.workspace(sess["ws"])
        wt = Path(_session_row(store, sess["session"])["worktree"] or body.get("cwd", "."))
        if tool == "Bash":
            written = [(rel, lines) for rel, lines, ex in bash_targets(ti.get("command", ""),
                                                                      Path(body.get("cwd") or wt), wt)[0]]
            written = [(rel, set(range(1, (wt / rel).read_text(encoding="utf-8", errors="replace").count("\n") + 2)))
                       for rel, _ in written if (wt / rel).is_file()]  # line counts after the write
        elif tool in ("Edit", "Write", "MultiEdit"):
            rel, _, _ = edit_targets(tool, {"file_path": ti.get("file_path")}, wt)
            written = [(rel, None)] if rel else []
        else:
            return {}
        written = [(rel, lines) for rel, lines in written if Path(rel).suffix.lower() in EXT]
        if not written or not info:
            return {}
        try:  # incremental enough for small repos: re-scan the package from this agent's worktree
            indexer.index(store, wt, info["package"], info["repo"])
        except SyntaxError:
            pass
        units = []
        for rel, lines in written:
            if lines is None:
                text = (wt / rel).read_text(encoding="utf-8", errors="replace")
                news = [e.get("new_string", "") for e in ti.get("edits", [])] if tool == "MultiEdit" \
                    else [ti.get("new_string", "") or ti.get("content", "")]
                lines = set().union(*(_lines_of(text, n) for n in news if n)) if any(news) else set()
            units += indexer.units_for_lines(store, rel, lines) if lines else []
        key = f"ws:{sess['ws']}:ticket:{sess['session']}:{body.get('tool_use_id', '')}"
        ticket = json.loads(store.r.getdel(key) or "null") if body.get("tool_use_id") else None
        now = ops.now_ms()
        # Fencing. A unit my write just created (nobody ever held it) becomes mine. Any other unit must still be
        # leased to me at the epoch my gate granted (the ticket), and I must not have finished (done) since then.
        # Without a ticket (a client that sends no tool_use_id), it must be mine at the epoch I last held.
        rows = store.q("hook.fence", """
            MATCH (s:Session {uid: $me})
            UNWIND $u AS id MATCH (u:Unit {uid: id})
            OPTIONAL MATCH (:Session {uid: $me})-[h:HELD]->(u)
            OPTIONAL MATCH (:Session)-[any:HELD]->(u)
            WITH s, u, max(h.epoch) AS mine, count(any) AS held_ever, u.lease_owner AS owner
            WITH s, u, mine, owner, owner IS NULL AND held_ever = 0 AS fresh,
                 coalesce([p IN $ticket WHERE p[0] = u.uid | p[1]][0], mine) AS granted
            SET u.lease_owner = CASE WHEN fresh THEN $me ELSE u.lease_owner END,
                u.lease_epoch = CASE WHEN fresh THEN coalesce(u.lease_epoch, 0) + 1 ELSE u.lease_epoch END,
                u.lease_expires = CASE WHEN fresh OR owner = $me THEN $until ELSE u.lease_expires END
            FOREACH (_ IN CASE WHEN fresh THEN [1] ELSE [] END |
                CREATE (s)-[:HELD {epoch: u.lease_epoch, from: $now, until: $until}]->(u))
            RETURN u.uid, fresh OR (owner = $me AND granted IS NOT NULL AND u.lease_epoch = granted
                                    AND NOT (s.status = 'done' AND coalesce(s.done_at, 0) > $gated_at))""",
            {"u": units, "me": sess["session"], "now": now, "until": now + ops.LEASE_TTL_MS,
             "ticket": [[k, v] for k, v in ticket["epochs"].items()] if ticket else [], "gated_at": ticket["t"] if ticket else 0})
        stale = [u for u, ok in rows if not ok]
        if stale:
            store.event("stale-write", sess["name"], units=stale)
            return {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext":
                    f"Nodes: STALE_EPOCH: you wrote {', '.join(stale)} without holding its current lease (it was "
                    "released, handed on, or you had already called done). Call before_acting on it now; if a "
                    "teammate owns it, revert your change."}}
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
            RETURN collect(u.uid)""", {"s": sess["session"]})
        freed = rel[0][0] if rel else []
        store.event("session-end", sess["name"], released=len(freed),
                    notify=ops.waiters_on(store, sess["session"], freed))
        return {}

    def subagent_start(store, sess, body):
        store.event("subagent-start", sess["name"], agent_type=body.get("agent_type", ""))
        return {"hookSpecificOutput": {"hookEventName": "SubagentStart", "additionalContext":
                PROTOCOL.format(name=sess["name"])}}

    def subagent_stop(store, sess, body):
        # a finished subagent is like a finished owner: its leases stay so the next claimant gets a HANDOFF
        store.q("hook.subagent_stop", "MATCH (s:Session {uid: $s}) SET s.status = 'done', s.done_at = $now",
                {"s": sess["session"], "now": ops.now_ms()})
        store.event("subagent-stop", sess["name"], agent_type=body.get("agent_type", ""))
        return {}

    for path, fn in (("session-start", session_start), ("pre-tool-use", pre_tool_use),
                     ("post-tool-use", post_tool_use), ("stop", stop), ("session-end", session_end),
                     ("subagent-start", subagent_start), ("subagent-stop", subagent_stop)):
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

    # ------------------------------------------------------------------ live dashboard v1 -------
    def _ws(request):
        names = sorted(n.decode() if isinstance(n, bytes) else n for n in reg.r.hkeys("nodes:workspaces"))
        return request.query_params.get("ws") or (names[0] if names else "")

    async def stream(request: Request) -> Response:
        """Server-sent events: every workspace event as it is appended (Redis XREAD BLOCK, no polling)."""
        import redis.asyncio as aioredis
        from starlette.responses import StreamingResponse
        ws = _ws(request)
        last = request.query_params.get("from", "$")

        async def gen():
            r = aioredis.Redis(host=reg.host, port=reg.port)
            nonlocal last
            try:
                yield ": connected\n\n"
                while not await request.is_disconnected():
                    got = await r.xread({f"ws:{ws}:events": last}, block=15000, count=100)
                    if not got:
                        yield ": keepalive\n\n"
                        continue
                    for _, entries in got:
                        for eid, fields in entries:
                            last = eid.decode() if isinstance(eid, bytes) else eid
                            e = json.loads(fields.get(b"e") or fields.get("e"))
                            yield f"id: {last}\ndata: {json.dumps(e | {'id': last})}\n\n"
            finally:
                await r.aclose()
        return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})

    async def all_events(request: Request) -> Response:
        """The whole event history (oldest first), for the timeline slider and replays."""
        ws = _ws(request)
        rows = reg.r.xrange(f"ws:{ws}:events", count=int(request.query_params.get("limit", "5000")))
        return JSONResponse([json.loads(f.get("e") or f.get(b"e")) | {"id": i.decode() if isinstance(i, bytes) else i}
                             for i, f in rows])

    async def trace(request: Request) -> Response:
        """One traced call: raw request, response and every Cypher query it ran, with timings."""
        ws, tid = _ws(request), request.query_params.get("id", "")
        ws = json.loads(reg.r.hget("nodes:workspaces", ws) or "{}").get("replay_of", ws)  # replays share the source trace
        p = reg.trace_dir / f"{ws}.trace.jsonl"
        if tid and p.exists():
            with p.open(encoding="utf-8") as fh:
                for line in fh:
                    if f'"id": "{tid}"' in line:
                        return JSONResponse(json.loads(line))
        return JSONResponse({"error": "not found"}, 404)

    async def graph(request: Request) -> Response:
        """The active neighborhood only: units leased or recently touched, their direct dependencies, the agents."""
        ws = _ws(request)
        st = reg.store(ws)
        active = st.q("dash.active", """
            MATCH (u:Unit) WHERE u.lease_owner IS NOT NULL OR (:Intent)-[:TOUCHES]->(u) OR (:Decision)-[:CITES]->(u)
            OPTIONAL MATCH (o:Session {uid: u.lease_owner})
            RETURN u.uid, u.kind, u.symbol, u.path, o.name LIMIT 60""", ro=True)
        uids = [a[0] for a in active]
        nbr = st.q("dash.neighbors", """
            UNWIND $u AS id MATCH (a:Unit {uid: id})-[d:DEPENDS_ON]-(b:Unit)
            RETURN DISTINCT a.uid, b.uid, b.kind, b.symbol, b.path, d.resolved_by LIMIT 120""", {"u": uids}, ro=True)
        nodes = {u: {"id": u, "kind": k, "label": s or p, "owner": o} for u, k, s, p, o in active}
        for a, b, k, s, p, by in nbr:
            nodes.setdefault(b, {"id": b, "kind": k, "label": s or p, "owner": None, "context": True})
        edges = [{"a": a, "b": b, "by": by} for a, b, *_, by in nbr]
        return JSONResponse({"nodes": list(nodes.values()), "edges": edges})

    async def replay_page(request: Request) -> Response:
        return FileResponse(Path(__file__).with_name("web") / "index.html")

    for path, fn in (("/api/stream", stream), ("/api/events", all_events), ("/api/trace", trace), ("/api/graph", graph)):
        server.custom_route(path, methods=["GET"])(fn)
