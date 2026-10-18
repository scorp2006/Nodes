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
from mcp.server.mcpserver import MCPServer

from . import harness, ops
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
        with open(self.trace_dir / f"{ws}.trace.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, default=str) + "\n")


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
    server = MCPServer("nodes", token_verifier=Verifier(reg),
                       auth=AuthSettings(issuer_url=public_url, resource_server_url=f"{public_url}/mcp",
                                         validate_token_resource=False))  # tokens are opaque session handles

    def ctx():
        claims = get_access_token().claims
        return reg.store(claims["ws"]), {"session": claims["session"], "name": claims["name"]}, claims["ws"]

    def call(tool, fn, **kw):
        store, sess, ws = ctx()
        out = traced_call(reg, ws, sess, "tool:" + profile, tool, kw, lambda: fn(store, sess, ws))
        return out if isinstance(out, str) else json.dumps(out)

    if profile == "nodes":
        @server.tool(name="context_for")
        def context_for(task_or_query: str = "", budget_tokens: int = 1500) -> str:
            """The context packet for a task or query: units and who owns them, facts with their sources, requests
            to you, recent changes. Budget-capped."""
            return call("context_for", lambda st, s, ws: ops.context_for(st, s, task_or_query, budget_tokens),
                        task_or_query=task_or_query, budget_tokens=budget_tokens)

        @server.tool(name="declare_intent")
        def declare_intent(task: str, summary: str, touches: list[dict], idempotency_key: str = "") -> str:
            """Record what you plan to change. touches: [{uid?, path?, symbol?, mode: create|modify|delete|read}].
            Returns overlaps with teammates' intents and who should own shared units."""
            return call("declare_intent", lambda st, s, ws: ops.declare_intent(st, s, task, summary, touches),
                        task=task, summary=summary, touches=touches)

        @server.tool(name="before_acting")
        def before_acting(action: str, targets: list[str], relies_on_facts: list[str] | None = None,
                          idempotency_key: str = "") -> str:
            """The action gate. Call before every change: action (edit|commit|merge|delete|decide), targets (unit
            uids, qualnames, paths or 'path:10-20'). Returns proceed | defer | handoff | escalate, the reason, and
            the graph path that explains it. Only act on proceed or handoff."""
            return call("before_acting", lambda st, s, ws: ops.before_acting(st, s, action, targets, relies_on_facts),
                        action=action, targets=targets, relies_on_facts=relies_on_facts)

        @server.tool(name="post_fact")
        def post_fact(text: str, source_ref: dict, idempotency_key: str = "") -> str:
            """Share a fact with the team, citing its source: source_ref {kind: file|briefing, ref, quote?}. The
            server checks it against the source and marks it source-checked or unverified."""
            def fn(st, s, ws):
                root = reg.workspace(ws).get("root")
                return ops.post_fact(st, s, text, source_ref, Path(root) if root else None)
            return call("post_fact", fn, text=text, source_ref=source_ref)

        @server.tool(name="coordinate")
        def coordinate(op: str, target: str = "", text: str = "", idempotency_key: str = "") -> str:
            """Coordination acts. op: request (ask a unit's owner for a change) | accept | decline | done (your
            work is committed; put the commit in text) | release | commit_to."""
            return call("coordinate", lambda st, s, ws: ops.coordinate(st, s, op, target, text),
                        op=op, target=target, text=text)

        @server.tool(name="status")
        async def status(wait_seconds: int = 0) -> str:
            """What changed that concerns you: your leases, waits, requests, recent facts. wait_seconds (<= 60)
            waits on the server (Redis pub/sub, no polling) for a notification first."""
            store, sess, ws = ctx()
            changes = await wait_notify(reg, ws, sess["name"], min(max(wait_seconds, 0), 60))
            return call("status", lambda st, s, w: ops.envelope(ops.digest(st, s) | {"changes": changes},
                        "Act on anything that concerns you; call before_acting before each edit."),
                        wait_seconds=wait_seconds)
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


def run(reg: Registry, profile: str, host: str, port: int):
    from mcp.server.transport_security import TransportSecuritySettings

    public = f"http://127.0.0.1:{port}"
    hosts = [f"{h}:{port}" for h in ("127.0.0.1", "localhost", "host.docker.internal")]
    build(reg, profile, public).run("streamable-http", host=host, port=port, stateless_http=True, json_response=True,
                                    transport_security=TransportSecuritySettings(
                                        enable_dns_rebinding_protection=True, allowed_hosts=hosts, allowed_origins=[]))
