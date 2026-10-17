"""Scripted two-agent walk-through against a running nodes server (no LLM): watch it at http://127.0.0.1:8781/.

  python -m nodes up --workspace <click checkout> --package <click checkout>/src/click     (terminal 1)
  python -m nodes.demo [--port 8780] [--pause 2]                                            (terminal 2)
Uses functions from pallets/click; with another repo, pass --a / --b function names that call each other.
"""
import argparse
import asyncio

from mcp import ClientSession
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client


async def call(port, agent, tool, args):
    async with create_mcp_http_client(headers={"X-Agent": agent}) as http:
        async with streamable_http_client(f"http://127.0.0.1:{port}/mcp", http_client=http) as (r, w, *_):
            async with ClientSession(r, w) as s:
                await s.initialize()
                text = (await s.call_tool(tool, args)).content[0].text
    print(f"\n[{agent}] {tool}({args})\n{text}")
    return text


async def main(a):
    steps = [
        ("alice", "declare_plan", {"functions": [a.a]}),
        ("bob", "declare_plan", {"functions": [a.b]}),
        ("alice", "claim", {"functions": [a.a, a.b]}),
        ("bob", "claim", {"functions": [a.b]}),
        ("alice", "share_fact", {"text": a.quote, "source": a.source}),
        ("bob", "share_fact", {"text": "edit() never launches an editor", "source": a.source}),
        ("alice", "mark_done", {"commit": "a1b2c3d"}),
        ("bob", "status", {}),
    ]
    for agent, tool, args in steps:
        await call(a.port, agent, tool, args)
        await asyncio.sleep(a.pause)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8780)
    ap.add_argument("--pause", type=float, default=2.0)
    ap.add_argument("--a", default="click.termui.edit")
    ap.add_argument("--b", default="click._termui_impl.Editor.edit_file")
    ap.add_argument("--source", default="src/click/termui.py")
    ap.add_argument("--quote", default="def edit(")
    asyncio.run(main(ap.parse_args()))
