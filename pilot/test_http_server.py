"""The long-lived HTTP tool server: right tools per mode, identity from headers, no leaks. No LLM."""
import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import graph_tools  # noqa: E402
from run_hiddenbench import start_http_server  # noqa: E402
from test_graph_tools import setup  # noqa: E402


async def call(mode, graph, agent, tool, args=None, log=None):
    from mcp import ClientSession
    from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

    headers = {"X-Board-Graph": graph, "X-Board-Agent": agent} | ({"X-Board-Log": log} if log else {})
    url = f"http://127.0.0.1:{graph_tools.HTTP_PORTS[mode]}/mcp"
    async with create_mcp_http_client(headers=headers) as http:
        async with streamable_http_client(url, http_client=http) as (read, write, *_):
            async with ClientSession(read, write) as session:
                await session.initialize()
                names = sorted(t.name for t in (await session.list_tools()).tools)
                if tool is None:
                    return names
                res = await session.call_tool(tool, args or {})
                return res.content[0].text


@pytest.fixture(scope="module", autouse=True)
def servers():
    procs = [start_http_server(m) for m in ("board", "notepad")]
    yield
    for p in procs:
        if p:
            p.kill()


def test_each_mode_exposes_only_its_own_tools():
    assert asyncio.run(call("board", "x", "x", None)) == ["share_fact", "shared_facts", "who_has_not_shared"]
    assert asyncio.run(call("notepad", "x", "x", None)) == ["read_notes", "review_my_facts", "write_note"]


def test_identity_comes_from_headers_and_nothing_leaks(tmp_path):
    task, agents, assignments, g = setup("test_http_board")
    p1, p2 = assignments[0], assignments[1]
    num = p1["visible_facts"].index(p1["assigned_hidden_information"][0]) + 1
    log = str(tmp_path / "calls.jsonl")

    reply = asyncio.run(call("board", "test_http_board", p1["agent"], "share_fact", {"fact_number": num}, log))
    assert reply.startswith("Source-checked: yes")
    board = asyncio.run(call("board", "test_http_board", p2["agent"], "shared_facts"))
    assert p1["assigned_hidden_information"][0] in board          # posted fact is visible to others
    assert p2["assigned_hidden_information"][0] not in board      # unposted fact never is
    assert board == graph_tools.shared_facts(g)                   # same as stdio mode
    assert json.loads(Path(log).read_text().splitlines()[0])["agent"] == p1["agent"]
    g.delete()


def test_digest_matches_the_read_tools():
    task, agents, assignments, g = setup("test_digest")
    a = assignments[0]["agent"]
    d = graph_tools.digest("test_digest", a, "board")
    assert d == graph_tools.shared_facts(g) + "\n" + graph_tools.who_has_not_shared(g)
    assert all(x["assigned_hidden_information"][0] not in d for x in assignments)
    g.delete()
