"""Tests for nodes/ (hardening items each have one). Needs FalkorDB; port from NODES_TEST_DB_PORT (default 6379).

  python -m pytest nodes/test_nodes.py -q
"""
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

import pytest
from falkordb import FalkorDB

from nodes import core, server

PORT = int(os.environ.get("NODES_TEST_DB_PORT", "6379"))
SRC = '''
def helper():
    return 1

def edit():
    return helper()

class Editor:
    def edit_file(self):
        return edit()

def other_edit():
    return 2
'''


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture()
def team(tmp_path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "m.py").write_text(SRC)
    (tmp_path / "NOTES.md").write_text("The cache is flushed every 30 seconds.\nSecond line.\n")
    g = FalkorDB(port=PORT).select_graph(f"nodes_test_{os.getpid()}_{time.time_ns()}")
    core.load_repo(g, pkg)
    yield g, tmp_path
    g.delete()


def test_agent_names_are_validated(team):
    g, _ = team
    for bad in ("", "a b", "../x", "x" * 65, "a'}) DETACH DELETE n //"):
        with pytest.raises(ValueError):
            core.touch(g, bad)
    core.touch(g, "alice")


def test_resolve_exact_suffix_and_ambiguous(team):
    g, _ = team
    found, problems = core.resolve(g, ["pkg.m.edit", "Editor.edit_file", "pkg/m.py:helper", "nope"])
    assert found == ["pkg.m.edit", "pkg.m.Editor.edit_file", "pkg.m.helper"]
    assert problems == {"nope": []}


def test_claim_race_has_one_winner_per_function(team):
    g, _ = team
    names = ["pkg.m.helper", "pkg.m.edit", "pkg.m.Editor.edit_file", "pkg.m.other_edit"]
    won = {}

    def go(i):
        mine, _ = core.claim(FalkorDB(port=PORT).select_graph(g.name), f"a{i}", names)
        won[f"a{i}"] = mine

    threads = [threading.Thread(target=go, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    winners = [(f, a) for a, fs in won.items() for f in fs]
    assert sorted(f for f, _ in winners) == sorted(names)          # each function won exactly once
    assert g.ro_query("MATCH (f:Function) WHERE f.version > 1 RETURN count(f)").result_set[0][0] == 0


def test_release_lets_a_teammate_claim(team):
    g, _ = team
    assert core.claim(g, "alice", ["pkg.m.edit"])[0] == ["pkg.m.edit"]
    assert core.claim(g, "bob", ["pkg.m.edit"]) == ([], {"pkg.m.edit": "alice"})
    assert core.release(g, "bob", ["pkg.m.edit"]) == []           # only the owner can release
    assert core.release(g, "alice", ["pkg.m.edit"]) == ["pkg.m.edit"]
    assert core.claim(g, "bob", ["pkg.m.edit"])[0] == ["pkg.m.edit"]


def test_overlap_found_through_calls(team):
    g, _ = team
    core.set_plan(g, "alice", ["pkg.m.helper"])
    core.set_plan(g, "bob", ["pkg.m.Editor.edit_file"])
    (other, mine, theirs, hops, via), = core.conflicts(g, "alice")
    assert (other, mine, theirs, hops) == ("bob", "pkg.m.helper", "pkg.m.Editor.edit_file", 2)
    core.set_plan(g, "bob", ["pkg.m.other_edit"])
    assert core.conflicts(g, "alice") == []


def test_digest_stays_small_as_the_team_grows(team):
    g, _ = team
    core.set_plan(g, "alice", ["pkg.m.edit"])
    for i in range(200):
        core.touch(g, f"agent{i:03}")
    core.claim(g, "agent000", ["pkg.m.helper", "pkg.m.other_edit"])
    text = core.digest(g, "alice")
    assert len(text) < 1500, len(text)                              # team list is capped at 30 agents
    assert "pkg.m.other_edit" not in text                           # unrelated claims are not listed


def test_fact_source_check(team):
    g, ws = team
    assert core.share_fact(g, ws, "alice", "the cache is  flushed every 30 seconds", "NOTES.md")
    assert core.share_fact(g, ws, "alice", "Second line.", "NOTES.md:2")
    assert not core.share_fact(g, ws, "alice", "Second line.", "NOTES.md:1")
    assert not core.share_fact(g, ws, "bob", "flushed every 5 seconds", "NOTES.md")
    outside = ws.parent / "secret.txt"
    outside.write_text("top secret")
    assert not core.share_fact(g, ws, "eve", "top secret", "../secret.txt")      # no reading outside the workspace
    assert not core.share_fact(g, ws, "eve", "top secret", str(outside))
    rows = core.facts(g)
    assert [r[3] for r in rows] == [False, False, False, False, True, True]  # newest first


def test_web_state_and_no_path_traversal(team):
    g, ws = team
    core.set_plan(g, "alice", ["pkg.m.helper"])
    core.set_plan(g, "bob", ["pkg.m.edit"])
    port = free_port()
    httpd = server.serve_web(g, port, ws)
    try:
        st = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state").read())
        assert {a["id"] for a in st["agents"]} == {"alice", "bob"}
        assert st["conflicts"][0]["hops"] == 1
        post = lambda body: urllib.request.urlopen(urllib.request.Request(  # noqa: E731
            f"http://127.0.0.1:{port}/api/before_acting", data=json.dumps(body).encode(), method="POST"))
        assert json.loads(post({"agent": "carol", "functions": ["pkg.m.other_edit"]}).read())["decision"] == "proceed"
        for bad_body in ({"agent": "../x", "functions": ["a"]}, {"agent": "carol", "functions": "notalist"}):
            with pytest.raises(urllib.error.HTTPError):
                post(bad_body)
        for bad in ("/replays/../server.py", "/replays/..%2Fserver.py", "/etc/passwd"):
            with pytest.raises(urllib.error.HTTPError):
                urllib.request.urlopen(f"http://127.0.0.1:{port}{bad}")
    finally:
        httpd.shutdown()


def test_mcp_over_http_end_to_end(team, tmp_path):
    import asyncio

    from mcp import ClientSession
    from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

    g, ws = team
    port = free_port()
    code = (f"import sys; sys.path.insert(0, r'{Path(__file__).resolve().parent.parent}');"
            f"from pathlib import Path; from nodes import server;"
            f"server.run({g.name!r}, Path(r'{ws}'), db_port={PORT}, port={port}, web_port={free_port()}, "
            f"log_dir=Path(r'{tmp_path}'))")
    proc = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        time.sleep(5)

        async def call(agent, tool, args, log=None):
            headers = {"X-Agent": agent} | ({"X-Log": log} if log else {})
            async with create_mcp_http_client(headers=headers) as http:
                async with streamable_http_client(f"http://127.0.0.1:{port}/mcp", http_client=http) as (r, w, *_):
                    async with ClientSession(r, w) as sess:
                        await sess.initialize()
                        return (await sess.call_tool(tool, args)).content[0].text

        evil = tmp_path.parent / "evil.log"
        assert "You own: pkg.m.edit" in asyncio.run(call("alice", "claim", {"functions": ["edit"]}, log=str(evil)))
        assert "owned by alice" in asyncio.run(call("bob", "claim", {"functions": ["pkg.m.edit"]}))
        assert not evil.exists()                                    # clients can't choose where the server writes
        assert (tmp_path / "tools.jsonl").exists()
    finally:
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True)


def test_action_gate_decisions(team):
    from nodes import gate

    g, ws = team
    good = [{"text": "The cache is flushed every 30 seconds.", "source": "NOTES.md"}]
    r = gate.before_acting(g, ws, "alice", ["pkg.m.edit"], good, "speed up edit")
    assert r["decision"] == "proceed" and r["provenance"]["status"] == "verified"
    assert gate.before_acting(g, ws, "bob", ["pkg.m.helper"])["decision"] == "proceed"
    # bob needs alice's function while she works: defer, and he holds nothing extra (all-or-nothing)
    r = gate.before_acting(g, ws, "bob", ["pkg.m.edit", "pkg.m.other_edit"])
    assert r["decision"] == "defer" and r["claim"]["blocked_by"] == {"pkg.m.edit": "alice"}
    assert g.ro_query("MATCH (f:Function {qualname: 'pkg.m.other_edit'}) RETURN f.claimed_by").result_set[0][0] is None
    # alice now needs bob's function: waiting would close a cycle -> escalate with the cycle as the path
    r = gate.before_acting(g, ws, "alice", ["pkg.m.helper"])
    assert r["decision"] == "escalate" and "deadlock" in r["reason"]
    assert r["explanation"] == ["alice", "-WAITS_FOR->", "bob", "-WAITS_FOR->", "alice"]
    # evidence that isn't in its source -> escalate, nothing claimed
    r = gate.before_acting(g, ws, "carol", ["pkg.m.other_edit"], [{"text": "flushed every 5 seconds", "source": "NOTES.md"}])
    assert r["decision"] == "escalate" and r["provenance"]["status"] == "unverified" and r["claim"]["won"] == []
    # alice finishes: bob's retry gets ownership handed over, with her commit
    core.mark_done(g, "alice", "abc123")
    r = gate.before_acting(g, ws, "bob", ["pkg.m.edit"])
    assert r["decision"] == "hand_off" and r["claim"]["handed_from"] == {"pkg.m.edit": "alice"} and "abc123" in r["reason"]
    assert g.ro_query("MATCH (:Agent {id: 'bob'})-[w:WAITS_FOR]->() RETURN count(w)").result_set[0][0] == 0
    # every decision is stored and traceable to its sources
    log = gate.recent(g)
    assert [x["decision"] for x in log][::-1] == ["proceed", "proceed", "defer", "escalate", "escalate", "hand_off"]
    assert log[-1]["cites"] == ["NOTES.md"]
