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
import os
import re
import socket
import time
from collections import Counter, deque
from pathlib import Path

from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response

from . import identity, indexer, ops, redact
from .lang import EXT
from .shellwrites import write_targets

PROTOCOL = """You are {name} on a team of coding agents coordinated by Nodes (a shared FalkorDB graph).
- Before you start, call declare_intent with the functions/files you plan to change.
- Every Edit/Write is gated automatically. If the gate says DEFER, a teammate owns that code: work on something
  else, or call status(wait_seconds=60) to be told when it frees up. If it says HANDOFF, merge the named commit
  first and build on top. If it says ESCALATE, stop and ask the user.
- When your work is committed, call coordinate(op='done', text='<commit sha>') so teammates can build on it.
- Share facts teammates need with post_fact, citing the file they come from.
- Plan with the graph: impact_of(unit) shows what a change reaches within 3 hops and who holds it; hotspots() and
  communities() show central code and clusters. A DEFER marked "possible overlap (advisory)" is not a block: if your
  change can't affect their code, call before_acting again with acknowledge='<why>'.
- Several independent tasks? Work in parallel: dispatch one subagent per task in a single message (each declares its
  intent and goes through the gate). You can keep editing yourself too."""


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


CHECK = re.compile(r"\b(pytest|unittest|tox|nox|npm (run )?test|yarn test|pnpm test|jest|vitest|mocha|go test|cargo test|"
                   r"ruff|flake8|pylint|mypy|pyright|eslint|tsc|black --check|gradle test|mvn test|rspec)\b")
SUMMARY = re.compile(r"(\d+ (passed|failed|errors?)\b.*|All checks passed.*|Found \d+ errors?.*|\d+ problems?.*|"
                     r"Success: no issues.*|FAIL\b.*|PASS\b.*|error\b.*)", re.I)


def observed_fact(store, sess, command: str, response) -> None:
    """A test or lint run the hook saw: a Fact whose source is the command's own output, at trust 'observed'."""
    if isinstance(response, dict):
        out = "\n".join(str(response.get(k) or "") for k in ("stdout", "stderr", "output"))
    else:
        out = response if isinstance(response, str) else ""
    lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
    summary = next((ln for ln in reversed(lines) if SUMMARY.search(ln)), lines[-1] if lines else "no output")
    cmd = " ".join(command.split())[:160]
    text = redact.text(f"`{cmd}`: {summary[:200]}")
    fid = "fact:run:" + ops.h(f"{sess['session']}|{cmd}|{ops.now_ms()}")
    store.q("hook.observed_fact", """
        MERGE (src:Source {uid: $src}) ON CREATE SET src.kind = 'command', src.ref = $cmd, src.output = $tail
        CREATE (f:Fact {uid: $fid, text: $text, source_uid: $src, status: 'observed', recorded_at: $now})
        MERGE (f)-[:SOURCED_FROM {quote: $quote}]->(src)
        WITH f MATCH (s:Session {uid: $sid}) MERGE (s)-[:ASSERTED {seq: $now, status: 'observed'}]->(f)""",
            {"src": "command:" + fid[9:], "cmd": cmd, "tail": redact.text("\n".join(lines[-15:]))[:1500], "fid": fid,
             "text": text, "now": ops.now_ms(), "quote": redact.text(summary)[:300], "sid": sess["session"]})
    store.event("fact", sess["name"], fact=fid, status="observed", text=text[:200])


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
        if tool in ("Agent", "Task") and not body.get("agent_id"):  # a dispatch: remember its task for SubagentStart
            k = f"ws:{sess['ws']}:pending_tasks:{sess['session']}"
            store.r.rpush(k, " ".join(str(ti.get("description") or ti.get("prompt") or "").split())[:60])
            store.r.expire(k, 3600)
            return {}
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
        nxt = (f"If your change can't affect their code, call the Nodes before_acting tool with targets={targets} and "
               "acknowledge='<why>', then retry this edit; otherwise coordinate with them first."
               if r.get("advisory") else
               "Work on something else, or call the status tool with wait_seconds=60 to be told when it frees up."
               if r["decision"] == "defer" else "Stop and ask the user to resolve this.")
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                       "permissionDecisionReason": f"{note}. Next: {nxt}"}}

    def post_tool_use(store, sess, body):
        tool, ti = body.get("tool_name", ""), body.get("tool_input") or {}
        if tool == "Bash" and CHECK.search(ti.get("command", "")):
            observed_fact(store, sess, ti["command"], body.get("tool_response"))
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
                                    AND NOT (s.status = 'done' AND coalesce(s.done_at, 0) >= $gated_at))""",
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
        # the parent's Agent call recorded the task description; attach the oldest one waiting to this subagent
        task = store.r.lpop(f"ws:{sess['ws']}:pending_tasks:cc:{body.get('session_id', '')}")
        task = (task.decode() if isinstance(task, bytes) else task) or body.get("agent_type", "")
        store.q("hook.subagent_task", "MATCH (s:Session {uid: $s}) SET s.task = $t", {"s": sess["session"], "t": task})
        store.event("subagent-start", sess["name"], agent_type=body.get("agent_type", ""), task=task)
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
            RETURN DISTINCT startNode(d).uid, endNode(d).uid, b.uid, b.kind, b.symbol, b.path, d.resolved_by
            LIMIT 120""", {"u": uids}, ro=True)
        files = st.q("dash.files", """
            UNWIND $u AS id MATCH (f:Unit {kind: 'file'})-[:CONTAINS]->(x:Unit {uid: id})
            RETURN DISTINCT f.uid, f.path, x.uid""", {"u": uids}, ro=True)
        nodes = {u: {"id": u, "kind": k, "label": s or p, "owner": o} for u, k, s, p, o in active}
        total = st.q("dash.size", "MATCH (u:Unit {status: 'active'}) RETURN count(u)", ro=True)[0][0]
        if request.query_params.get("scope", "auto") != "active" and total <= 400:
            # small enough to show whole: the repo is the map, the active code is lit on top of it
            for u, k, s, p in st.q("dash.all", "MATCH (u:Unit) WHERE u.status = 'active' OR u.status IS NULL "
                                   "RETURN u.uid, u.kind, u.symbol, u.path", ro=True):
                nodes.setdefault(u, {"id": u, "kind": k, "label": s or p, "owner": None, "context": True})
            ids = list(nodes)
            edges = [{"a": a, "b": b, "rel": "CALLS" if r == "DEPENDS_ON" else r, "by": by} for a, b, r, by in st.q(
                "dash.all_edges", "UNWIND $u AS id MATCH (a:Unit {uid: id})-[d:DEPENDS_ON|CONTAINS]->(b:Unit) "
                "WHERE b.uid IN $u RETURN a.uid, b.uid, type(d), d.resolved_by", {"u": ids}, ro=True)]
            return JSONResponse({"nodes": list(nodes.values()), "edges": edges, "scope": "all", "total": total})
        for _, _, b, k, s, p, _ in nbr:
            nodes.setdefault(b, {"id": b, "kind": k, "label": s or p, "owner": None, "context": True})
        edges = [{"a": a, "b": b, "rel": "CALLS", "by": by} for a, b, _, _, _, _, by in nbr]
        for f, p, x in files:  # files as parents: the tree layout reads repo -> file -> unit
            nodes.setdefault(f, {"id": f, "kind": "file", "label": p, "owner": None, "context": True})
            edges.append({"a": f, "b": x, "rel": "CONTAINS"})
        return JSONResponse({"nodes": list(nodes.values()), "edges": edges, "scope": "active", "total": total})

    async def node(request: Request) -> Response:
        """Everything about one unit: where it is, who leases it, who wrote it and why, facts and decisions on it."""
        ws, uid = _ws(request), request.query_params.get("uid", "")
        st = reg.store(ws)
        rows = st.q("dash.node", """
            MATCH (u:Unit {uid: $u}) OPTIONAL MATCH (o:Session {uid: u.lease_owner})
            RETURN u.kind, u.path, u.symbol, u.start, u.end, u.lang, u.status, o.name, u.lease_epoch, u.lease_expires,
                   u.last_rev, u.rationale""", {"u": uid}, ro=True)
        if not rows:
            return JSONResponse({"error": "not found"}, 404)
        k, path, sym, s, e, lang, status, owner, epoch, exp, rev, why = rows[0]
        authors = st.q("dash.node.authors", "MATCH (p:Principal)-[o:OWNS_CODE]->(:Unit {uid: $u}) RETURN p.name, p.uid, "
                       "o.lines ORDER BY o.lines DESC LIMIT 5", {"u": uid}, ro=True)
        decisions = st.q("dash.node.decisions", "MATCH (s:Session)-[:MADE]->(d:Decision)-[:CITES]->(:Unit {uid: $u}) "
                         "RETURN s.name, d.verdict, d.reason, d.at ORDER BY d.at DESC LIMIT 6", {"u": uid}, ro=True)
        held = st.q("dash.node.held", "MATCH (s:Session)-[h:HELD]->(:Unit {uid: $u}) RETURN s.name, h.epoch, h.from "
                    "ORDER BY h.epoch DESC LIMIT 6", {"u": uid}, ro=True)
        calls = st.q("dash.node.calls", "MATCH (:Unit {uid: $u})-[:DEPENDS_ON]->(b:Unit) RETURN b.symbol LIMIT 12",
                     {"u": uid}, ro=True)
        callers = st.q("dash.node.callers", "MATCH (a:Unit)-[:DEPENDS_ON]->(:Unit {uid: $u}) RETURN a.symbol LIMIT 12",
                       {"u": uid}, ro=True)
        facts = st.q("dash.node.facts", """
            MATCH (f:Fact)-[:SOURCED_FROM]->(src:Source) WHERE src.ref STARTS WITH $p RETURN f.text, f.status, src.ref
            UNION
            MATCH (f:Fact)-[:ABOUT]->(:Unit {kind: 'file', path: $p}) MATCH (f)-[:SOURCED_FROM]->(src:Source)
            RETURN f.text, f.status, src.ref""", {"p": path}, ro=True)[:8] if path else []
        return JSONResponse({
            "uid": uid, "kind": k, "path": path, "symbol": sym, "lines": [s, e], "lang": lang, "status": status,
            "lease": {"owner": owner, "epoch": epoch, "expires": exp} if owner else None,
            "last_change": {"commit": rev, "why": why} if rev else None,
            "authors": [{"name": n, "id": i, "lines": ln} for n, i, ln in authors],
            "lease_history": [{"agent": a, "epoch": ep, "at": t} for a, ep, t in held],
            "decisions": [{"agent": a, "verdict": v, "reason": r, "at": t} for a, v, r, t in decisions],
            "calls": [c for (c,) in calls], "called_by": [c for (c,) in callers],
            "facts": [{"text": t, "status": s_, "source": r} for t, s_, r in facts]})

    started = time.time()

    async def server_info(request: Request) -> Response:
        """The local MCP server and its database, for the dashboard's status panel."""
        ws = _ws(request)
        t0 = time.perf_counter()
        reg.r.ping()
        ping = round((time.perf_counter() - t0) * 1000, 2)
        ver = ""
        for m in reg.r.execute_command("MODULE", "LIST"):
            m = m.items() if isinstance(m, dict) else zip(m[::2], m[1::2])  # RESP3 dict or RESP2 list
            m = {(k.decode() if isinstance(k, bytes) else k): v for k, v in m}
            if m.get("name") in (b"graph", "graph"):
                v = int(m["ver"])
                ver = f"{v // 10000}.{v // 100 % 100}.{v % 100}"
        counts = {}
        if ws and f"ws:{ws}" in set(reg.r.execute_command("GRAPH.LIST")):
            st = reg.store(ws)
            counts = {lbl: n for lbl, n in st.q("dash.labels", "MATCH (n) RETURN labels(n)[0], count(n)", ro=True)}
            counts["_edges"] = st.q("dash.edges", "MATCH ()-[e]->() RETURN count(e)", ro=True)[0][0]
        trace = reg.trace_dir / f"{ws}.trace.jsonl"
        info = reg.workspace(ws)
        return JSONResponse({
            "server": {"profile": "nodes", "mcp": f"{request.url.scheme}://{request.url.netloc}/mcp",
                       "started_at": started, "uptime_s": round(time.time() - started),
                       "tools": ["context_for", "declare_intent", "before_acting", "post_fact", "coordinate", "status",
                                 "impact_of", "path_between", "hotspots", "communities", "query_graph"],
                       "hooks": ["SessionStart", "PreToolUse", "PostToolUse", "SubagentStart", "SubagentStop", "Stop",
                                 "SessionEnd"]},
            "falkordb": {"host": reg.host, "port": reg.port, "module_version": ver, "ping_ms": ping,
                         "graph": f"ws:{ws}" if ws else "",
                         **browser(reg.port)},
            "workspace": {"name": ws, "root": info.get("root", ""), "replay_of": info.get("replay_of"),
                          "counts": counts, "traced_calls": sum(1 for _ in trace.open(encoding="utf-8"))
                          if trace.exists() else 0}})

    async def calls(request: Request) -> Response:
        """Recent traced calls (MCP tools and hooks), newest first, each with what it did to the graph."""
        ws = _ws(request)
        ws = json.loads(reg.r.hget("nodes:workspaces", ws) or "{}").get("replay_of", ws)
        p = reg.trace_dir / f"{ws}.trace.jsonl"
        if not p.exists():
            return JSONResponse([])
        with p.open(encoding="utf-8") as fh:
            tail = deque(fh, maxlen=int(request.query_params.get("limit", "200")))
        out = []
        for line in reversed(tail):
            r = json.loads(line)
            qs = r.get("queries") or []
            fx = Counter()
            for q in qs:
                fx.update(q[3] if len(q) > 3 else {})
            measured = any(len(q) > 3 for q in qs)  # traces written before effects were recorded have none
            resp = json.dumps(r.get("response"))
            m = re.search(r"Nodes gate: (\w+)", resp) or re.search(r'"decision": "(\w+)"', resp)
            out.append({"id": r["id"], "t": r["t"], "kind": r["kind"], "name": r["name"], "agent": r.get("agent"),
                        "ms": r.get("ms"), "queries": len(qs), "effects": dict(fx) if measured else None,
                        "verdict": m.group(1).lower() if m else None, "error": '"code":' in resp.replace(" ", ""),
                        "patterns": sorted({pt for q in qs for pt in patterns(q[2])})[:8]})
        return JSONResponse(out)

    def graph_names() -> list[str]:
        return sorted(n.decode() if isinstance(n, bytes) else n for n in reg.r.execute_command("GRAPH.LIST"))

    async def graphs(request: Request) -> Response:
        """Every graph in this FalkorDB (workspaces, snapshots, anything else), for the raw graph explorer."""
        return JSONResponse(graph_names())

    def clip(props: dict) -> dict:  # what the browser may see of a node: secrets redacted, long values cut
        out = redact.value({k: v for k, v in props.items() if k != "token_hash"})
        return {k: (v[:300] + "…" if isinstance(v, str) and len(v) > 300 else v) for k, v in out.items()}

    async def raw(request: Request) -> Response:
        """A read-only look at any graph: label and relationship counts, and up to `limit` nodes of the chosen labels
        with the relationships between them (FalkorDB Browser's view, inside the dashboard)."""
        name = request.query_params.get("graph", "")
        if name not in graph_names():
            return JSONResponse({"error": "no such graph"}, 404)
        g = reg.store("_registry").db.select_graph(name)
        limit = min(int(request.query_params.get("limit", "300")), 1500)
        labels = {lbl: g.ro_query(f"MATCH (n:`{lbl}`) RETURN count(n)").result_set[0][0]
                  for (lbl,) in g.ro_query("CALL db.labels()").result_set}
        rels = {t: g.ro_query(f"MATCH ()-[r:`{t}`]->() RETURN count(r)").result_set[0][0]
                for (t,) in g.ro_query("CALL db.relationshipTypes()").result_set}
        want = [x for x in request.query_params.get("labels", "").split(",") if x in labels] or list(labels)
        per = max(1, limit // max(1, len(want)))  # a fair share for every label, so small labels aren't crowded out
        nodes = {}
        for lbl in want:
            for nid, lbls, props in g.ro_query(f"MATCH (n:`{lbl}`) RETURN ID(n), labels(n), properties(n) LIMIT $k",
                                               {"k": per}).result_set:
                nodes[nid] = {"id": nid, "labels": lbls, "props": clip(props)}
        ids = list(nodes)
        edges = [{"id": rid, "type": t, "src": a, "dst": b, "props": clip(p)} for rid, t, a, b, p in g.ro_query(
            "MATCH (a)-[r]->(b) WHERE ID(a) IN $ids AND ID(b) IN $ids RETURN ID(r), type(r), ID(a), ID(b), properties(r) "
            "LIMIT 4000", {"ids": ids}).result_set]
        return JSONResponse({"graph": name, "labels": labels, "relationships": rels, "nodes": list(nodes.values()),
                             "edges": edges, "shown": len(nodes), "total": sum(labels.values())})

    async def static(request: Request) -> Response:  # the dashboard's own files (app.js, vendored canvas)
        base = Path(__file__).with_name("web").resolve()
        f = (base / request.path_params["path"]).resolve()
        if not f.is_file() or not f.is_relative_to(base):
            return JSONResponse({"error": "not found"}, 404)
        return FileResponse(f, media_type="text/javascript" if f.suffix == ".js" else None)

    async def replay_page(request: Request) -> Response:
        return FileResponse(Path(__file__).with_name("web") / "index.html")

    for path, fn in (("/api/stream", stream), ("/api/events", all_events), ("/api/trace", trace), ("/api/graph", graph),
                     ("/api/node", node), ("/api/server", server_info), ("/api/calls", calls),
                     ("/api/graphs", graphs), ("/api/raw", raw), ("/static/{path:path}", static)):
        server.custom_route(path, methods=["GET"])(fn)


def _listening(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.2)
        return s.connect_ex(("127.0.0.1", port)) == 0


def browser(db_port: int) -> dict:
    """Where FalkorDB Browser is, and what to type in its login form (it can't be deep-linked to a graph)."""
    own = os.environ.get("NODES_BROWSER_URL") or ("http://127.0.0.1:3001" if _listening(3001) else "")
    if own:  # the browser inside our own FalkorDB container (`nodes up` publishes it on :3001)
        return {"browser": own, "browser_login": "localhost : 6379"}
    if _listening(3000):  # another FalkorDB container's browser: point it at ours through Docker's host alias
        return {"browser": "http://127.0.0.1:3000", "browser_login": f"host.docker.internal : {db_port}"}
    return {"browser": "", "browser_login": ""}


REL = re.compile(r"\((\w*)(?::(\w+))?[^()]*\)\s*(<?)-\[\w*(?::(\w+))?[^\]]*\]-(>?)\s*(?=\((\w*)(?::(\w+))?)")


def patterns(cypher: str) -> list[str]:
    """The relationship patterns a query walks or writes, e.g. 'Session-[:HELD]->Unit' (variables resolved to labels)."""
    labels = dict(re.findall(r"\((\w+):(\w+)", cypher))
    out = []
    for a, la, left, rel, right, b, lb in REL.findall(cypher):
        if not rel:
            continue
        la, lb = la or labels.get(a, "?"), lb or labels.get(b, "?")
        out.append(f"{lb}-[:{rel}]->{la}" if left and not right else f"{la}-[:{rel}]->{lb}")
    return out
