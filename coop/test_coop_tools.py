"""Offline checks for the CooperBench tool logic. Needs FalkorDB on localhost:6379, no LLM."""
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import coop_tools as ct  # noqa: E402
from world_graph import load  # noqa: E402

FUNCS = [{"qualname": q, "name": q.rsplit(".", 1)[1], "file": "pkg/m.py", "line": i}
         for i, q in enumerate(["pkg.m.edit", "pkg.m.Editor.edit_file", "pkg.m.Editor.edit", "pkg.m.unrelated"])]
CALLS = [{"src": "pkg.m.edit", "dst": "pkg.m.Editor.edit_file"}, {"src": "pkg.m.Editor.edit", "dst": "pkg.m.Editor.edit_file"}]


def fresh(name):
    g = load(name, FUNCS, CALLS)
    for a in ("agent1", "agent2"):
        g.query("MERGE (:Agent {id: $a})", {"a": a})
    return g


def test_claim_is_first_wins_even_when_racing():
    g = fresh("test_cb_claim")
    results = {}

    def go(agent):
        results[agent] = ct.claim(g, agent, ["pkg.m.edit", "pkg.m.Editor.edit_file"])

    threads = [threading.Thread(target=go, args=(a,)) for a in ("agent1", "agent2")]
    [t.start() for t in threads]
    [t.join() for t in threads]
    owners = {q for a in results for q in results[a][0]}
    assert owners == {"pkg.m.edit", "pkg.m.Editor.edit_file"}
    winner = next(a for a in results if results[a][0])
    loser = "agent2" if winner == "agent1" else "agent1"
    assert results[loser][0] == [] and set(results[loser][1].values()) == {winner}  # one owner, other is told
    g.delete()


def test_roles_walk_the_one_way_handoff():
    g = fresh("test_cb_status")
    ct.set_plan(g, "agent1", ["pkg.m.edit"])
    ct.set_plan(g, "agent2", ["pkg.m.Editor.edit_file"])
    assert ct.next_step(g, "agent2")[0] == "unclaimed"                    # 1 call apart, nobody owns it
    assert "Overlaps with your partner" in ct.status_text(g, "agent2")
    ct.claim(g, "agent1", ["pkg.m.edit", "pkg.m.Editor.edit_file"])
    assert ct.next_step(g, "agent1")[0] == "owner-work"
    key, text = ct.next_step(g, "agent2")
    assert key == "builder-wait" and "do NOT edit" in text
    assert ct.mark_committed(g, "agent1", "abc123") == 2
    assert ct.next_step(g, "agent1")[0] == "owner-wait"
    key, text = ct.next_step(g, "agent2")
    assert key == "builder-work" and "git merge --no-edit team/agent1" in text and "abc123" in text
    assert ct.mark_committed(g, "agent2", "def456") == 0                  # builder owns nothing; still recorded
    key, text = ct.next_step(g, "agent1")
    assert key == "owner-final" and "--ff-only team/agent2" in text
    assert ct.next_step(g, "agent2")[0] == "builder-final"
    g.delete()


def test_no_overlap_means_independent():
    g = fresh("test_cb_indep")
    ct.set_plan(g, "agent1", ["pkg.m.edit"])
    ct.set_plan(g, "agent2", ["pkg.m.unrelated"])
    assert ct.next_step(g, "agent1")[0] == "independent"
    g.delete()


def test_wait_returns_as_soon_as_the_partner_commits():
    import asyncio
    import time

    g = fresh("test_cb_wait")
    ct.set_plan(g, "agent1", ["pkg.m.edit"])
    ct.set_plan(g, "agent2", ["pkg.m.Editor.edit_file"])
    ct.claim(g, "agent1", ["pkg.m.edit", "pkg.m.Editor.edit_file"])
    threading.Timer(3, lambda: ct.mark_committed(g, "agent1", "abc")).start()
    t0 = time.time()
    asyncio.run(ct.wait_until_changed(g, "agent2", 60))
    assert 2 < time.time() - t0 < 10 and ct.next_step(g, "agent2")[0] == "builder-work"
    g.delete()


def test_name_resolution():
    g = fresh("test_cb_names")
    found, problems = ct.resolve_names(g, ["Editor.edit_file", "pkg/m.py:edit", "nope", "edit"])
    assert found[:2] == ["pkg.m.Editor.edit_file", "pkg.m.edit"]
    assert "nope" in problems and problems["nope"] == []
    assert "pkg.m.edit" in found  # 'edit' resolves to the exact-shortest match
    g.delete()


def test_placebo_is_private_and_shares_nothing():
    g = fresh("test_cb_placebo")
    ct.record_private_plan(g, "agent1", ["pkg.m.edit"])
    ct.write_note(g, "agent1", "I will touch edit")
    assert ct.private_plan(g, "agent1") == ["pkg.m.edit"]
    assert ct.private_plan(g, "agent2") == [] and ct.read_notes(g, "agent2") == []
    assert ct.overlaps(g, "agent2") == []  # placebo writes never create shared plan edges
    g.delete()


def test_one_agent_waiting_does_not_block_the_other():
    """Through the real HTTP server: agent2 waits server-side while agent1 claims and commits."""
    import asyncio
    import subprocess
    import time

    from mcp import ClientSession
    from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

    g = fresh("test_cb_http")
    ct.set_plan(g, "agent1", ["pkg.m.edit"])
    ct.set_plan(g, "agent2", ["pkg.m.Editor.edit_file"])
    proc = subprocess.Popen([sys.executable, "-I", str(Path(__file__).with_name("coop_tools.py")), "--mode", "graph"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        time.sleep(4)

        async def call(agent, tool, args):
            headers = {"X-World-Graph": "test_cb_http", "X-Agent": agent}
            async with create_mcp_http_client(headers=headers) as http:
                async with streamable_http_client(f"http://127.0.0.1:{ct.PORTS['graph']}/mcp", http_client=http) as (r, w, *_):
                    async with ClientSession(r, w) as s:
                        await s.initialize()
                        t0 = time.time()
                        res = await s.call_tool(tool, args)
                        return time.time() - t0, res.content[0].text

        async def scenario():
            waiter = asyncio.create_task(call("agent2", "coordination_status", {"wait_seconds": 60}))
            await asyncio.sleep(1)
            claim_s, _ = await call("agent1", "claim", {"functions": ["pkg.m.edit", "pkg.m.Editor.edit_file"]})
            commit_s, _ = await call("agent1", "mark_committed", {"commit": "abc"})
            wait_s, text = await waiter
            return claim_s, commit_s, wait_s, text

        claim_s, commit_s, wait_s, text = asyncio.run(scenario())
        assert claim_s < 3 and commit_s < 3, (claim_s, commit_s)   # not stuck behind agent2's wait
        assert wait_s < 15 and "Your role: builder" in text, (wait_s, text)
    finally:
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True)
        g.delete()


def test_each_mode_has_five_tools():
    import asyncio

    from falkordb import FalkorDB

    for mode in ("graph", "placebo"):
        server = ct.build_server(FalkorDB(), mode)
        tools = asyncio.run(server.list_tools())
        assert len(tools) == 5, (mode, [t.name for t in tools])
