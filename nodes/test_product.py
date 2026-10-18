"""Tests for Nodes v0.1. Needs the product FalkorDB (port from NODES_TEST_DB_PORT, default 6380).

  python -m pytest nodes/test_product.py -q
"""
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from nodes import harness, indexer, ops
from nodes.db import Store

PORT = int(os.environ.get("NODES_TEST_DB_PORT", "6380"))
SRC = '''import os
import sys

VERSION = "1"
ALIASES = {}


def helper():
    return 1


def edit():
    return helper()


class Editor:
    mode = "x"

    def edit_file(self):
        return edit()
'''


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture()
def ws(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("")
    (tmp_path / "pkg" / "m.py").write_text(SRC)
    st = Store(f"test-{os.getpid()}-{time.time_ns()}", port=PORT)
    st.reset()
    st.setup()
    indexer.index(st, tmp_path, "pkg", "t")
    yield st, tmp_path
    st.reset()
    st.r.hdel("nodes:workspaces", st.ws)


def agent(st, name):
    _, sid = ops.join(st, name)
    return {"session": sid, "name": name}


def test_database_enforces_uniqueness_and_provenance(ws):
    st, _ = ws
    for bad in ("CREATE (:Unit {uid: 'fn:t:pkg/m.py#edit'})",              # duplicate uid
                "CREATE (:Fact {uid: 'f1', text: 'no source'})",           # fact without provenance
                "CREATE (:Decision {uid: 'd1', verdict: 'proceed'})"):     # decision without a reason
        with pytest.raises(Exception):
            st.q("t", bad)


def test_imports_and_module_blocks_are_leasable_units(ws):
    st, _ = ws
    kinds = dict(st.q("t", "MATCH (u:Unit) RETURN u.uid, u.kind", ro=True))
    assert kinds["region:t:pkg/m.py#imports"] == "region"
    assert kinds["region:t:pkg/m.py#module@1"] == "region"
    assert kinds["region:t:pkg/m.py#Editor.__body__"] == "region"
    assert indexer.units_for_lines(st, "pkg/m.py", {1}) == ["region:t:pkg/m.py#imports"]


def test_gate_four_decisions_and_handoff_snapshot(ws):
    st, _ = ws
    a, b = agent(st, "alice"), agent(st, "bob")
    assert ops.before_acting(st, a, "edit", ["pkg/m.py:1-2", "pkg.m.edit"])["decision"] == "proceed"
    r = ops.before_acting(st, b, "edit", ["pkg/m.py:2", "pkg.m.helper"])
    assert r["decision"] == "defer" and r["data"]["path"][-1] == "alice"
    assert st.q("t", "MATCH (u:Unit {uid: 'fn:t:pkg/m.py#helper'}) RETURN u.lease_owner", ro=True)[0][0] is None
    assert ops.before_acting(st, b, "edit", ["pkg.m.helper"])["decision"] == "proceed"
    r = ops.before_acting(st, a, "edit", ["pkg.m.helper"])
    assert r["decision"] == "escalate" and r["data"]["path"] == ["alice", "-WAITS_FOR->", "bob", "-WAITS_FOR->", "alice"]
    ops.coordinate(st, a, "done", text="abc123")
    r = ops.before_acting(st, b, "edit", ["pkg/m.py:1"])
    assert r["decision"] == "handoff" and "abc123" in r["data"]["reason"] and r["data"]["snapshot"]
    assert st.db.select_graph(r["data"]["snapshot"]).ro_query("MATCH (u:Unit) RETURN count(u)").result_set[0][0] > 0
    assert [v for v, in st.q("t", "MATCH (d:Decision) RETURN d.verdict ORDER BY d.at", ro=True)] == \
        ["proceed", "defer", "proceed", "escalate", "handoff"]


def test_lease_race_one_winner_and_epochs(ws):
    st, _ = ws
    sessions = [agent(st, f"a{i}") for i in range(20)]
    won = []

    def go(s):
        s2 = Store(st.ws, port=PORT)
        if ops.before_acting(s2, s, "edit", ["pkg.m.edit", "pkg.m.helper"])["decision"] == "proceed":
            won.append(s["name"])

    threads = [threading.Thread(target=go, args=(s,)) for s in sessions]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(won) == 1
    epochs = st.q("t", "MATCH (u:Unit) WHERE u.uid IN ['fn:t:pkg/m.py#edit', 'fn:t:pkg/m.py#helper'] "
                  "RETURN u.lease_epoch", ro=True)
    assert [e for e, in epochs] == [1, 1]


def test_expired_lease_can_be_taken_with_a_new_epoch(ws):
    st, _ = ws
    a, b = agent(st, "alice"), agent(st, "bob")
    ops.before_acting(st, a, "edit", ["pkg.m.edit"], ttl_ms=1)
    time.sleep(0.01)
    r = ops.before_acting(st, b, "edit", ["pkg.m.edit"])
    assert r["decision"] == "proceed" and r["data"]["lease"][0]["epoch"] == 2


def test_facts_are_checked_against_their_source(ws):
    st, root = ws
    a = agent(st, "alice")
    assert ops.post_fact(st, a, 'VERSION = "1"', {"kind": "file", "ref": "pkg/m.py"}, root)["data"]["status"] == "source-checked"
    assert ops.post_fact(st, a, "VERSION is 2", {"kind": "file", "ref": "pkg/m.py"}, root)["data"]["status"] == "unverified"
    (root.parent / "secret.txt").write_text("top secret")
    assert ops.post_fact(st, a, "top secret", {"kind": "file", "ref": "../secret.txt"}, root)["data"]["status"] == "unverified"


def test_harness_board_texts_and_no_leak(ws):
    st, _ = ws
    sess = harness.seed_board(st, [{"agent": "P1", "visible_facts": ["shared", "secret one"]},
                                   {"agent": "P2", "visible_facts": ["shared", "secret two"]}])
    assert harness.shared_facts(st) == "The board is empty. Nobody has posted a fact yet."
    assert harness.share_fact(st, sess["P1"], 9) == "Source-checked: no. You have no Fact 9, so nothing was posted."
    assert harness.share_fact(st, sess["P1"], 2) == "Source-checked: yes. Your Fact 2 is now on the board."
    board = harness.shared_facts(st)
    assert "secret one" in board and "secret two" not in board and "shared" not in board
    assert harness.who_has_not_shared(st).endswith("P2")


def test_mcp_requires_a_bearer_token_and_hooks_deny_on_defer(ws, tmp_path):
    st, root = ws
    st.r.hset("nodes:workspaces", st.ws, json.dumps({"root": str(root), "repo": "t", "package": "pkg"}))
    ta, sa = ops.join(st, "alice", worktree=str(root))
    tb, sb = ops.join(st, "bob", worktree=str(root))
    port = free_port()
    code = (f"import sys; sys.path.insert(0, r'{Path(__file__).resolve().parents[1]}'); from pathlib import Path; "
            f"from nodes.server import Registry, run; run(Registry('localhost', {PORT}, Path(r'{tmp_path}')), "
            f"'nodes', '127.0.0.1', {port})")
    proc = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(50):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state?ws={st.ws}")
                break
            except Exception:  # noqa: BLE001
                time.sleep(0.2)
        # v1 local mode: no SDK auth on /mcp; identity is the hook stamp or a bearer token (LAN mode is tested below)

        def hook(token, body):
            req = urllib.request.Request(f"http://127.0.0.1:{port}/hooks/pre-tool-use", data=json.dumps(body).encode(),
                                         method="POST", headers={"Authorization": f"Bearer {token}",
                                                                 "Content-Type": "application/json"})
            return json.loads(urllib.request.urlopen(req).read())
        edit = {"tool_name": "Edit", "cwd": str(root), "tool_input": {"file_path": str(root / "pkg" / "m.py"),
                                                                       "old_string": "def edit():", "new_string": "def edit():  # x"}}
        assert "permissionDecision" not in hook(ta, edit)["hookSpecificOutput"]          # proceed: no override
        out = hook(tb, edit)["hookSpecificOutput"]
        assert out["permissionDecision"] == "deny" and "DEFER" in out["permissionDecisionReason"]
        assert hook("not-a-token", edit) == {}                                           # unknown caller: never block
        trace = (tmp_path / f"{st.ws}.trace.jsonl").read_text(encoding="utf-8").splitlines()
        assert len(trace) == 2 and all(json.loads(t)["queries"] for t in trace)          # every hook call traced
        # regression (first live demo run): when the owner finishes and its session ends, its leases must stay,
        # so the next agent gets HANDOFF (merge first), not a plain proceed that skips the finished work
        ops.coordinate(st, {"session": sa, "name": "alice"}, "done", text="c0ffee")
        urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{port}/hooks/session-end", data=b"{}",
                                                      method="POST", headers={"Authorization": f"Bearer {ta}",
                                                                              "Content-Type": "application/json"}))
        ctx = hook(tb, edit)["hookSpecificOutput"]["additionalContext"]
        assert "HANDOFF" in ctx and "c0ffee" in ctx
    finally:
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True)


def test_lan_mode_requires_the_token_on_every_endpoint(tmp_path):
    port, token = free_port(), "t" * 32
    code = (f"import sys; sys.path.insert(0, r'{Path(__file__).resolve().parents[1]}'); from pathlib import Path; "
            f"from nodes.server import Registry, run; run(Registry('localhost', {PORT}, Path(r'{tmp_path}')), "
            f"'nodes', '127.0.0.1', {port}, server_token='{token}')")
    proc = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        def get(path, method="GET", hdr=None):
            req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method, headers=hdr or {},
                                         data=b"{}" if method == "POST" else None)
            try:
                return urllib.request.urlopen(req).status
            except urllib.error.HTTPError as e:
                return e.code
            except urllib.error.URLError:
                return 0  # not listening yet
        for _ in range(50):
            if get("/") in (200, 401):
                break
            time.sleep(0.2)
        for path, method in (("/", "GET"), ("/api/state", "GET"), ("/mcp", "POST"), ("/hooks/pre-tool-use", "POST")):
            assert get(path, method, {"Content-Type": "application/json"}) == 401, path      # no token: refused
        assert get("/api/state", hdr={"Authorization": f"Bearer {token}"}) == 200            # header works
        assert get(f"/?token={token}") == 200                                                # browser link works
    finally:
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True)
