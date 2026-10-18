"""Scripted action-gate demo (no LLM): three agents, real MCP calls with bearer tokens, all four decisions.
Uses its own workspace (`gatedemo`) on a fresh copy of the shop repo; watch it at http://127.0.0.1:8780/?ws=gatedemo

  python -m nodes up            (terminal 1)
  python demo/gate_demo.py [--pause 2] [--port 8780]
"""
import argparse
import asyncio
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from mcp import ClientSession
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

HERE = Path(__file__).resolve().parent
USER, ALL, TOTAL = "region:gatedemo:src/shop/models.py#User.__body__", "region:gatedemo:src/shop/models.py#module@4", \
    "fn:gatedemo:src/shop/models.py#order_total"


async def call(port, token, tool, args):
    async with create_mcp_http_client(headers={"Authorization": f"Bearer {token}"}) as http:
        async with streamable_http_client(f"http://127.0.0.1:{port}/mcp", http_client=http) as (r, w, *_):
            async with ClientSession(r, w) as s:
                await s.initialize()
                return json.loads((await s.call_tool(tool, args)).content[0].text)


async def main(a):
    repo = Path(tempfile.mkdtemp()) / "shop"
    shutil.copytree(HERE / "shop", repo)
    nodes = [sys.executable, "-m", "nodes"]
    subprocess.run([*nodes, "init", "--repo", str(repo), "--package", "src/shop", "--workspace", "gatedemo",
                    "--port", str(a.port)], cwd=HERE.parent, check=True, capture_output=True)
    tok = {n: subprocess.run([*nodes, "join", "--workspace", "gatedemo", "--agent", n, "--quiet"], cwd=HERE.parent,
                             check=True, capture_output=True, text=True).stdout.strip().splitlines()[-1]
           for n in ("alice", "bob", "carol")}
    fact = None
    steps = [
        ("alice", "before_acting", {"action": "edit", "targets": [USER, ALL]}, "alice edits the User model + __all__"),
        ("bob", "before_acting", {"action": "edit", "targets": [USER]}, "bob also needs the User model"),
        ("bob", "before_acting", {"action": "edit", "targets": [TOTAL]}, "bob works on order_total meanwhile"),
        ("alice", "before_acting", {"action": "edit", "targets": [TOTAL]}, "alice now needs order_total too"),
        ("carol", "post_fact", {"text": "orders are capped at 10 items", "source_ref": {"kind": "file",
                                "ref": "src/shop/models.py"}}, "carol posts a claim the code doesn't support"),
        ("carol", "before_acting", None, "carol acts on that claim"),
        ("alice", "coordinate", {"op": "done", "text": "a1b2c3d"}, "alice commits and is done"),
        ("bob", "before_acting", {"action": "edit", "targets": [USER]}, "bob retries the User model"),
    ]
    for agent, tool, args, what in steps:
        if args is None:
            args = {"action": "edit", "targets": [TOTAL], "relies_on_facts": [fact]}
        r = await call(a.port, tok[agent], tool, args)
        d = r.get("data", {})
        if tool == "post_fact":
            fact = d["fact_uid"]
            print(f"\n{what}\n  -> fact {d['status']}")
        elif tool == "before_acting":
            print(f"\n{what}\n  -> {d['decision'].upper()}: {d['reason']}\n     path: {' '.join(d['path'])}")
        else:
            print(f"\n{what}\n  -> {json.dumps(d)}")
        await asyncio.sleep(a.pause)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8780)
    ap.add_argument("--pause", type=float, default=2.0)
    asyncio.run(main(ap.parse_args()))
