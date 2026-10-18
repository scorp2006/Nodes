"""Graph reasoning tools, extra call/import edges, harvested facts, plain-English context, subagent names.
Needs the product FalkorDB (6380).   python -m pytest nodes/test_track1.py -q
"""
import json
import os
import subprocess
import time

import pytest

from nodes import gitimport, graphtools, indexer, ops
from nodes.db import Store

PORT = int(os.environ.get("NODES_TEST_DB_PORT", "6380"))
FILES = {
    "app/__init__.py": "",
    "app/camera.py": '"""Camera input."""\nclass Camera:\n    def read(self):\n        return 1\n',
    "app/base.py": "class Base:\n    def hello(self):\n        return 'hi'\n",
    "app/pipeline.py": ("from app.camera import Camera\nfrom app.base import Base\n\n\nclass Pipeline(Base):\n"
                        "    def __init__(self):\n        self.camera = Camera()\n\n    def step(self):\n"
                        "        frame = self.camera.read()\n        return self.hello(), frame\n\n\n"
                        "def main():\n    p = Pipeline()\n    return p.step()\n"),
}


def git(repo, *a):
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=Ada", "-c", "user.email=ada@x.io", *a],
                          check=True, capture_output=True, text=True).stdout


@pytest.fixture()
def repo(tmp_path):
    for rel, text in FILES.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(text)
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-qm", "Add the camera pipeline so frames flow to the narrator")
    st = Store(f"t1-{os.getpid()}-{time.time_ns()}", port=PORT)
    st.reset()
    st.setup()
    st.r.hdel("nodes:git", st.ws)
    stats = indexer.index(st, tmp_path, None, "r")
    gitimport.import_git(st, tmp_path, stats.pop("_units"))
    yield st, tmp_path, {"session": ops.join(st, "alice")[1], "name": "alice"}
    st.reset()
    st.r.hdel("nodes:git", st.ws)


def test_self_attribute_inherited_and_import_edges(repo):
    st, _, _ = repo
    edges = dict(st.q("t", "MATCH (a:Unit)-[d:DEPENDS_ON]->(b:Unit) RETURN a.symbol + '>' + b.symbol, d.resolved_by", ro=True))
    assert edges["Pipeline.step>Camera.read"] == "self-attr"      # self.camera = Camera(); self.camera.read()
    assert edges["Pipeline.step>Base.hello"] == "inherited"       # self.hello() defined on the base class
    imports = {a + ">" + b for a, b in st.q("t", "MATCH (a:Unit)-[:IMPORTS]->(b:Unit) RETURN a.path, b.path", ro=True)}
    assert {"app/pipeline.py>app/camera.py", "app/pipeline.py>app/base.py"} <= imports


def test_graph_tools_answer_with_their_cypher(repo):
    st, _, me = repo
    r = graphtools.impact_of(st, me, "app.camera.Camera.read", 3)["data"]
    hit = {x["unit"].split("#")[-1]: x for x in r["impact"]}
    assert hit["Pipeline.step"]["direction"] == "caller" and hit["main"]["hops"] == 2 and r["cypher"]
    p = graphtools.path_between(st, me, "app.pipeline.main", "app.camera.Camera.read")["data"]
    assert p["connected"] and p["path"][0].endswith("#main") and p["path"][-1].endswith("#Camera.read")
    assert graphtools.hotspots(st, me, 3)["data"]["hotspots"]
    assert graphtools.communities(st, me, 5)["data"]["communities"]
    rows = graphtools.query_graph(st, me, "MATCH (u:Unit {kind: 'class'}) RETURN u.symbol ORDER BY u.symbol")["data"]["rows"]
    assert rows == [["Base"], ["Camera"], ["Pipeline"]]
    for bad in ("MATCH (u:Unit) SET u.x = 1", "CREATE (:Unit {uid: 'x'})", "MATCH (u) DETACH DELETE u"):
        assert graphtools.query_graph(st, me, bad)["error"]["code"] == "READ_ONLY"
    assert any(e["kind"] == "query" and e["tool"] == "impact_of" for e in st.events(20))  # shows on the dashboard


def test_commit_messages_become_checked_facts_about_files(repo):
    st, _, _ = repo
    rows = st.q("t", "MATCH (f:Fact)-[:SOURCED_FROM]->(s:Source {kind: 'commit'}) MATCH (f)-[:ABOUT]->(u:Unit) "
                "RETURN f.text, f.status, count(u)", ro=True)
    assert rows[0][0].startswith("Add the camera pipeline") and rows[0][1] == "source-checked" and rows[0][2] == 4


def test_context_for_takes_plain_english_and_returns_rationale(repo):
    st, _, me = repo
    d = ops.context_for(st, me, "how does the camera read frames", 3000)["data"]
    top = d["units"][0]
    assert top["uid"].endswith("#Camera.read") and top["rationale"].startswith("Add the camera pipeline")
    assert d["facts_about_these_files"]


def test_test_runs_become_observed_facts(repo):
    from nodes.hooks import observed_fact
    st, _, me = repo
    observed_fact(st, me, "python -m pytest -q", {"stdout": "....\n4 passed in 0.03s\n", "stderr": ""})
    t, status, out = st.q("t", "MATCH (f:Fact {status: 'observed'})-[:SOURCED_FROM]->(s:Source {kind: 'command'}) "
                          "RETURN f.text, f.status, s.output", ro=True)[0]
    assert "4 passed" in t and "pytest" in t and "4 passed" in out


def test_subagents_get_short_distinct_names(repo):
    from nodes import identity
    st, root, _ = repo
    common = subprocess.run(["git", "-C", str(root), "rev-parse", "--path-format=absolute", "--git-common-dir"],
                            capture_output=True, text=True).stdout.strip()
    st.r.hset("nodes:workspaces", st.ws, json.dumps({"root": str(root), "git_common_dir": common}))

    class Reg:
        r = st.r

        @staticmethod
        def store(ws):
            return st
    identity.git_common_dir.cache_clear()
    a = identity.session_for_hook(Reg, {"session_id": "s1234567", "cwd": str(root), "agent_id": "a3f2c1",
                                        "agent_type": "general-purpose"})
    b = identity.session_for_hook(Reg, {"session_id": "s1234567", "cwd": str(root), "agent_id": "b77e01",
                                        "agent_type": "general-purpose"})
    assert (a["name"], b["name"]) == ("sub-a3f2", "sub-b77e")
    ops.declare_intent(st, {"session": a["session"], "name": a["name"]}, "add discount codes", "s", [])
    assert st.q("t", "MATCH (s:Session {uid: $s}) RETURN s.task", {"s": a["session"]}, ro=True)[0][0] == "add discount codes"
    st.r.hdel("nodes:workspaces", st.ws)
