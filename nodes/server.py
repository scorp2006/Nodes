"""Nodes server: MCP (stateless streamable HTTP, bearer-token identity) + Claude Code hook endpoints + dashboard API.

Profiles (one per server process):
  nodes          the six product tools, in a fixed order
  harness-board  HiddenBench board tools (share_fact, shared_facts, who_has_not_shared), harness texts
  harness-coop   CooperBench tools (impact_of, declare_plan, claim, mark_committed, coordination_status)
Identity: `Authorization: Bearer <token>` minted by `nodes join`; tools never take an agent name.
Every tool call and hook call is written to the trace (request, response, total ms, per-query ms).
"""
from __future__ import annotations

import asyncio
import contextvars
import json
import time
from pathlib import Path

import redis.asyncio as aioredis
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import Context, MCPServer

from . import harness, identity, ops, redact
from .db import Store

WEB = Path(__file__).with_name("web")
CALLS = contextvars.ContextVar("calls", default=None)


class Registry:
    """Workspaces live in Redis (`nodes:workspaces`); one Store per workspace, created lazily."""

    def __init__(self, host: str, port: int, trace_dir: Path):
        self.host, self.port, self.trace_dir = host, port, trace_dir
        self.stores: dict[str, Store] = {}
        self.r = Store("_registry", host, port).r
        trace_dir.mkdir(parents=True, exist_ok=True)

    def store(self, ws: str) -> Store:
        if ws not in self.stores:
            st = Store(ws, self.host, self.port)
            orig = st.q

            def q(name, cypher, params=None, ro=False, _orig=orig):  # per-query latency into the current trace
                t0 = time.perf_counter()
                try:
                    return _orig(name, cypher, params, ro)
                finally:
                    calls = CALLS.get()
                    if calls is not None:
                        calls.append([name, round((time.perf_counter() - t0) * 1000, 2)])
            st.q = q
            self.stores[ws] = st
        return self.stores[ws]

    def workspace(self, ws: str) -> dict:
        raw = self.r.hget("nodes:workspaces", ws)
        return json.loads(raw) if raw else {}

    def session(self, token: str | None) -> dict | None:
        return ops.session_by_token(self.r, token) if token else None

    def trace(self, ws: str, rec: dict) -> None:
        rec = redact.value(json.loads(json.dumps(rec, default=str)))  # secrets never reach disk
        with open(self.trace_dir / f"{ws}.trace.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")


class Verifier:
    def __init__(self, reg: Registry):
        self.reg = reg

    async def verify_token(self, token: str) -> AccessToken | None:
        s = self.reg.session(token)
        if not s:
            return None
        return AccessToken(token=token, client_id=s["session"], scopes=["agent"], subject=s["name"], claims=s)


def traced_call(reg: Registry, ws: str, sess: dict, kind: str, name: str, request, fn):
    """Run fn() with per-query timing; write one trace record; return fn's result."""
    calls = []
    token = CALLS.set(calls)
    t0 = time.perf_counter()
    try:
        out = fn()
    except Exception as e:  # noqa: BLE001  every failure becomes a stable error envelope
        out = ops.error("DB_UNAVAILABLE" if "Connection" in type(e).__name__ else "INVALID_ARGS",
                        str(e)[:300], "Check the arguments and retry.")
    finally:
        CALLS.reset(token)
    reg.trace(ws, {"t": time.time(), "kind": kind, "agent": sess.get("name"), "session": sess.get("session"),
                   "name": name, "request": request, "response": out,
                   "ms": round((time.perf_counter() - t0) * 1000, 2), "queries": calls})
    return out


def build(reg: Registry, profile: str = "nodes", public_url: str = "http://127.0.0.1:8780") -> MCPServer:
    if profile == "nodes":  # identity from the hook stamp or a bearer token, checked per call (local mode: no token)
        server = MCPServer("nodes")
    else:  # harness profiles: bearer token required on every request, exactly as in the reproduction runs
        server = MCPServer("nodes", token_verifier=Verifier(reg),
                           auth=AuthSettings(issuer_url=public_url, resource_server_url=f"{public_url}/mcp",
                                             validate_token_resource=False))  # tokens are opaque session handles

    def ctx(mcp_ctx=None, caller=None):
        if caller and caller.get("session_id"):  # stamped by the PreToolUse hook: session, subagent, cwd
            s = identity.session_for_hook(reg, caller)
            if not s:
                raise LookupError("this repository is not initialized: run `nodes init` in it")
            return reg.store(s["ws"]), {"session": s["session"], "name": s["name"]}, s["ws"]
        tok = get_access_token()
        if tok and tok.claims:
            c = tok.claims
            return reg.store(c["ws"]), {"session": c["session"], "name": c["name"]}, c["ws"]
        h = (getattr(mcp_ctx, "headers", None) or {}).get("authorization", "") if mcp_ctx else ""
        s = reg.session(h[7:].strip()) if h.lower().startswith("bearer ") else None
        if s:
            return reg.store(s["ws"]), {"session": s["session"], "name": s["name"]}, s["ws"]
        raise LookupError("unknown caller: use Claude Code with the Nodes hooks, or a token from `nodes init`")

    def call(tool, fn, mcp_ctx=None, caller=None, **kw):
        try:
            store, sess, ws = ctx(mcp_ctx, caller)
        except LookupError as e:
            return json.dumps(ops.error("UNKNOWN_CALLER", str(e), "Run `nodes init` in this repository."))
        out = traced_call(reg, ws, sess, "tool:" + profile, tool, kw | {"caller": caller}, lambda: fn(store, sess, ws))
        return out if isinstance(out, str) else json.dumps(out)

    if profile == "nodes":
        @server.tool(name="context_for")
        def context_for(ctx: Context, task_or_query: str = "", budget_tokens: int = 1500, caller: dict | None = None) -> str:
            """The context packet for a task or query: units and who owns them, facts with their sources, requests
            to you, recent changes, and each unit's recorded rationale. Budget-capped."""
            return call("context_for", lambda st, s, ws: ops.context_for(st, s, task_or_query, budget_tokens),
                        ctx, caller, task_or_query=task_or_query, budget_tokens=budget_tokens)

        @server.tool(name="declare_intent")
        def declare_intent(task: str, summary: str, touches: list[dict], ctx: Context, idempotency_key: str = "",
                           caller: dict | None = None) -> str:
            """Record what you plan to change. touches: [{uid?, path?, symbol?, mode: create|modify|delete|read}].
            Returns overlaps with teammates' intents and who should own shared units."""
            return call("declare_intent", lambda st, s, ws: ops.declare_intent(st, s, task, summary, touches),
                        ctx, caller, task=task, summary=summary, touches=touches)

        @server.tool(name="before_acting")
        def before_acting(action: str, targets: list[str], ctx: Context, relies_on_facts: list[str] | None = None,
                          idempotency_key: str = "", caller: dict | None = None) -> str:
            """The action gate. Call before every change: action (edit|commit|merge|delete|decide), targets (unit
            uids, qualnames, paths or 'path:10-20'). Returns proceed | defer | handoff | escalate, the reason, and
            the graph path that explains it. Only act on proceed or handoff."""
            return call("before_acting", lambda st, s, ws: ops.before_acting(st, s, action, targets, relies_on_facts),
                        ctx, caller, action=action, targets=targets, relies_on_facts=relies_on_facts)

        @server.tool(name="post_fact")
        def post_fact(text: str, source_ref: dict, ctx: Context, idempotency_key: str = "",
                      caller: dict | None = None) -> str:
            """Share a fact with the team, citing its source: source_ref {kind: file|briefing, ref, quote?}. The
            server checks it against the source and marks it source-checked or unverified."""
            def fn(st, s, ws):
                root = reg.workspace(ws).get("root")
                return ops.post_fact(st, s, text, source_ref, Path(root) if root else None)
            return call("post_fact", fn, ctx, caller, text=text, source_ref=source_ref)

        @server.tool(name="coordinate")
        def coordinate(op: str, ctx: Context, target: str = "", text: str = "", idempotency_key: str = "",
                       caller: dict | None = None) -> str:
            """Coordination acts. op: request (ask a unit's owner for a change) | accept | decline | done (your
            work is committed; put the commit in text) | release | commit_to."""
            return call("coordinate", lambda st, s, ws: ops.coordinate(st, s, op, target, text),
                        ctx, caller, op=op, target=target, text=text)

        @server.tool(name="status")
        async def status(ctx: Context, wait_seconds: int = 0, caller: dict | None = None) -> str:
            """What changed that concerns you: your leases, waits, requests, recent facts. wait_seconds (<= 60)
            waits on the server (Redis pub/sub, no polling) for a notification first."""
            try:
                store, sess, ws = ctx_resolve(ctx, caller)
            except LookupError as e:
                return json.dumps(ops.error("UNKNOWN_CALLER", str(e), "Run `nodes init` in this repository."))
            changes = await wait_notify(reg, ws, sess["name"], min(max(wait_seconds, 0), 60))
            return call("status", lambda st, s, w: ops.envelope(ops.digest(st, s) | {"changes": changes},
                        "Act on anything that concerns you; call before_acting before each edit."),
                        ctx, caller, wait_seconds=wait_seconds)
        ctx_resolve = ctx
    elif profile == "harness-board":
        @server.tool(name="share_fact", description=harness.BOARD_TOOLS["share_fact"])
        def share_fact(fact_number: int) -> str:
            return call("share_fact", lambda st, s, ws: harness.share_fact(st, s, fact_number), fact_number=fact_number)

        @server.tool(name="shared_facts", description=harness.BOARD_TOOLS["shared_facts"])
        def shared_facts() -> str:
            return call("shared_facts", lambda st, s, ws: harness.shared_facts(st))

        @server.tool(name="who_has_not_shared", description=harness.BOARD_TOOLS["who_has_not_shared"])
        def who_has_not_shared() -> str:
            return call("who_has_not_shared", lambda st, s, ws: harness.who_has_not_shared(st))
    elif profile == "harness-coop":
        @server.tool(name="impact_of", description=harness.COOP_TOOLS["impact_of"])
        def impact_of(function: str) -> str:
            return call("impact_of", lambda st, s, ws: harness.impact_text(st, function), function=function)

        @server.tool(name="declare_plan", description=harness.COOP_TOOLS["declare_plan"])
        async def declare_plan(functions: list[str]) -> str:
            store, sess, ws = ctx()
            team = int(reg.workspace(ws).get("team_size", 2))
            found, _ = harness.resolve_names(store, functions)
            harness.set_plan(store, sess, found)
            deadline = time.time() + 60  # the harness waits up to 60 s for the partner's plan
            while time.time() < deadline and len(harness.planners(store)) < team:
                await asyncio.sleep(1)
            return call("declare_plan", lambda st, s, w: harness.declare_plan_text(st, s, functions, team)[0],
                        functions=functions)

        @server.tool(name="claim", description=harness.COOP_TOOLS["claim"])
        def claim(functions: list[str]) -> str:
            return call("claim", lambda st, s, ws: harness.claim_text(st, s, functions)[0], functions=functions)

        @server.tool(name="mark_committed", description=harness.COOP_TOOLS["mark_committed"])
        def mark_committed(commit: str) -> str:
            return call("mark_committed", lambda st, s, ws: harness.mark_committed_text(st, s, commit), commit=commit)

        @server.tool(name="coordination_status", description=harness.COOP_TOOLS["coordination_status"])
        async def coordination_status(wait_seconds: int = 0) -> str:
            store, sess, ws = ctx()
            if wait_seconds:
                start = harness.next_step(store, sess["name"])[0]
                deadline = time.time() + min(max(wait_seconds, 0), 110)
                while time.time() < deadline and harness.next_step(store, sess["name"])[0] == start:
                    await asyncio.sleep(2)
            return call("coordination_status", lambda st, s, w: harness.status_text(st, s["name"]),
                        wait_seconds=wait_seconds)
    else:
        raise ValueError(f"unknown profile {profile}")
    from . import hooks  # hook endpoints + dashboard API share this app
    hooks.register(server, reg)
    return server


async def wait_notify(reg: Registry, ws: str, name: str, seconds: int) -> list:
    """Block on Redis pub/sub (no polling) until a notification for `name` arrives or `seconds` pass."""
    if seconds <= 0:
        return []
    r = aioredis.Redis(host=reg.host, port=reg.port)
    p = r.pubsub()
    await p.subscribe(f"ws:{ws}:notify:{name}")
    got, deadline = [], time.time() + seconds
    try:
        while time.time() < deadline and not got:
            m = await p.get_message(ignore_subscribe_messages=True, timeout=max(0.1, deadline - time.time()))
            if m:
                got.append(json.loads(m["data"]))
    finally:
        await p.unsubscribe()
        await r.aclose()
    return got


class TokenGate:
    """LAN mode: every request (MCP, hooks, dashboard, API) needs the server token, or a valid agent token.
    Browsers may pass it once as ?token=...; it is then kept in an HttpOnly cookie."""

    def __init__(self, app, reg: Registry, server_token: str):
        self.app, self.reg, self.token = app, reg, server_token

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        import hmac
        from urllib.parse import parse_qs

        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        auth = headers.get("authorization", "")
        given = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        given = given or parse_qs(scope.get("query_string", b"").decode()).get("token", [""])[0]
        given = given or next((c.split("=", 1)[1] for c in headers.get("cookie", "").split("; ")
                               if c.startswith("nodes_token=")), "")
        ok = bool(given) and (hmac.compare_digest(given, self.token) or self.reg.session(given) is not None)
        if not ok:
            from starlette.responses import JSONResponse
            return await JSONResponse({"ok": False, "error": {"code": "UNAUTHORIZED", "message": "token required"}},
                                      status_code=401)(scope, receive, send)
        if "token=" in scope.get("query_string", b"").decode():  # remember it for the browser
            async def send_with_cookie(msg):
                if msg["type"] == "http.response.start":
                    msg.setdefault("headers", []).append(
                        (b"set-cookie", f"nodes_token={given}; HttpOnly; SameSite=Strict; Path=/".encode()))
                await send(msg)
            return await self.app(scope, receive, send_with_cookie)
        return await self.app(scope, receive, send)


def run(reg: Registry, profile: str, host: str, port: int, server_token: str = ""):
    import uvicorn
    from mcp.server.transport_security import TransportSecuritySettings

    public = f"http://127.0.0.1:{port}"
    hosts = [f"{h}:{port}" for h in ("127.0.0.1", "localhost", "host.docker.internal")]
    if server_token:  # LAN: accept any Host, the token is the gate
        security = TransportSecuritySettings(enable_dns_rebinding_protection=False)
    else:
        security = TransportSecuritySettings(enable_dns_rebinding_protection=True, allowed_hosts=hosts,
                                             allowed_origins=[])
    app = build(reg, profile, public).streamable_http_app(stateless_http=True, json_response=True,
                                                          transport_security=security, host=host)
    if server_token:
        app = TokenGate(app, reg, server_token)
    uvicorn.run(app, host=host, port=port, log_level="warning")
