"""MCP server + live web view for one team graph.

Any MCP client connects to http://127.0.0.1:<port>/mcp and names itself with an X-Agent header (or, over stdio,
the NODES_AGENT environment variable). The team graph and the workspace are fixed by the server, never by the client.
The web view (http://127.0.0.1:<web_port>/) polls /api/state.
"""
from __future__ import annotations

import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from falkordb import FalkorDB
from mcp.server.mcpserver import Context, MCPServer

from . import core, gate

WEB = Path(__file__).with_name("web")


def build(g, workspace: Path, log_dir: Path | None = None) -> MCPServer:
    server = MCPServer("nodes")

    def who(ctx: Context) -> str:
        agent = (ctx.headers or {}).get("x-agent") or os.environ.get("NODES_AGENT", "")
        core.touch(g, agent)
        return agent

    def logged(agent, tool, text, **kw):
        if log_dir:  # the server picks the file; clients can't name a path
            with open(log_dir / "tools.jsonl", "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"t": time.time(), "agent": agent, "tool": tool, **kw}) + "\n")
        return text

    def note(problems):
        return ("\nNot recorded (no unique match): " + "; ".join(
            f"{k} -> {', '.join(v) if v else 'no such function'}" for k, v in problems.items())) if problems else ""

    @server.tool(name="declare_plan")
    def _plan(functions: list[str], ctx: Context) -> str:
        """Record the functions you intend to change (e.g. ["pkg.module.func"]); replaces your previous plan.
        Returns overlaps with teammates' plans (within 3 calls) and who owns what."""
        agent = who(ctx)
        found, problems = core.resolve(g, functions)
        core.set_plan(g, agent, found)
        return logged(agent, "declare_plan", f"Plan recorded: {', '.join(found) or 'nothing'}." + note(problems)
                      + "\n" + core.digest(g, agent), resolved=found)

    @server.tool(name="claim")
    def _claim(functions: list[str], ctx: Context) -> str:
        """Claim functions so only you change them. Atomic: the first agent to claim a function owns it."""
        agent = who(ctx)
        found, problems = core.resolve(g, functions)
        mine, others = core.claim(g, agent, found)
        return logged(agent, "claim", "You own: " + (", ".join(mine) or "nothing new") + "." + "".join(
            f"\n{q} is owned by {o}: do not edit it." for q, o in others.items()) + note(problems), won=mine)

    @server.tool(name="release")
    def _release(functions: list[str], ctx: Context) -> str:
        """Give up functions you claimed, so teammates can claim them."""
        agent = who(ctx)
        found, problems = core.resolve(g, functions)
        done = core.release(g, agent, found)
        return logged(agent, "release", f"Released: {', '.join(done) or 'nothing'}." + note(problems), released=done)

    @server.tool(name="mark_done")
    def _done(commit: str, ctx: Context) -> str:
        """Say your work is committed (pass the commit hash) so teammates can build on it."""
        agent = who(ctx)
        n = core.mark_done(g, agent, commit[:80])
        return logged(agent, "mark_done", f"Recorded {commit[:80]} ({n} owned function(s)).\n" + core.digest(g, agent))

    @server.tool(name="status")
    def _status(ctx: Context) -> str:
        """Your plan, your claims, your overlaps with teammates, and who owns what you touch."""
        agent = who(ctx)
        return logged(agent, "status", core.digest(g, agent))

    @server.tool(name="impact_of")
    def _impact(function: str, ctx: Context) -> str:
        """Functions that call, or are called by, `function` within 2 calls."""
        agent = who(ctx)
        return logged(agent, "impact_of", core.impact(g, function), function=function)

    @server.tool(name="share_fact")
    def _share(text: str, source: str, ctx: Context) -> str:
        """Post a fact to the team board, quoting a workspace file: source is 'path' or 'path:10-20'.
        The board marks it source-checked only if the quoted text is in that file (or line range)."""
        agent = who(ctx)
        ok = core.share_fact(g, workspace, agent, text, source)
        return logged(agent, "share_fact", f"Posted. Source-checked: {'yes' if ok else 'no (the quote is not in ' + source + ')'}.",
                      checked=ok)

    @server.tool(name="before_acting")
    def _gate(functions: list[str], ctx: Context, evidence: list[dict] | None = None, description: str = "") -> str:
        """Call before every change. functions: what you will change; evidence: [{"text": quote, "source": "path"}]
        that justifies it. Returns the decision (proceed / defer / hand_off / escalate), the reason, and the graph
        path that explains it. Only act on "proceed" or "hand_off"."""
        agent = who(ctx)
        r = gate.before_acting(g, workspace, agent, functions, evidence, description)
        return logged(agent, "before_acting", json.dumps(r, indent=1), decision=r["decision"])

    @server.tool(name="board")
    def _board(ctx: Context) -> str:
        """The team board, newest first, with each fact's source and whether it was source-checked."""
        agent = who(ctx)
        rows = core.facts(g)
        return logged(agent, "board", "\n".join(f"- {a} [{'checked' if c else 'NOT checked'}: {s}]: {t}"
                                                 for a, t, s, c in rows) or "The board is empty.")
    return server


def serve_web(g, port: int, workspace: Path | None = None) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            path = self.path.split("?")[0]
            if path == "/api/state":
                body, kind = json.dumps(core.state(g) | {"actions": gate.recent(g)}).encode(), "application/json"
            elif path in ("/", "/replay") or path.startswith("/replays/"):
                f = (WEB / ("index.html" if path == "/" else "replay.html" if path == "/replay" else path[1:])).resolve()
                if not (f.is_file() and f.is_relative_to(WEB.resolve())):
                    return self.send_error(404)
                body = f.read_bytes()
                kind = "application/json" if f.suffix == ".json" else "text/html; charset=utf-8"
            else:
                return self.send_error(404)
            self.send_response(200)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):  # noqa: N802
            if self.path != "/api/before_acting" or workspace is None:
                return self.send_error(404)
            try:
                req = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length", 0)), 100_000)))
                fns, ev = req["functions"], req.get("evidence") or []
                if not (isinstance(fns, list) and all(isinstance(f, str) for f in fns) and isinstance(ev, list)
                        and all(isinstance(e, dict) for e in ev)):
                    raise ValueError("functions: list of strings; evidence: list of {text, source}")
                body = json.dumps(gate.before_acting(g, workspace, req["agent"], fns, ev,
                                                     str(req.get("description", "")))).encode()
            except (ValueError, KeyError, TypeError) as e:
                return self.send_error(400, str(e)[:200])
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def run(team: str, workspace: Path, db_host="localhost", db_port=6379, port=8780, web_port=8781,
        log_dir: Path | None = None, transport="streamable-http"):
    from mcp.server.transport_security import TransportSecuritySettings

    g = FalkorDB(host=db_host, port=db_port).select_graph(team)
    if transport == "stdio":
        return build(g, workspace, log_dir).run("stdio")
    serve_web(g, web_port, workspace)
    hosts = [f"{h}:{port}" for h in ("127.0.0.1", "localhost", "host.docker.internal")]
    security = TransportSecuritySettings(enable_dns_rebinding_protection=True, allowed_hosts=hosts, allowed_origins=[])
    build(g, workspace, log_dir).run("streamable-http", host="127.0.0.1", port=port, transport_security=security)
