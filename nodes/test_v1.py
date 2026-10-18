"""Tests for the v1 sprint: multi-language units, git import, hook identity, stamping, redaction.
Needs the product FalkorDB (NODES_TEST_DB_PORT, default 6380).   python -m pytest nodes/test_v1.py -q
"""
import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from nodes import gitimport, identity, indexer, ops, redact
from nodes.db import Store

PORT = int(os.environ.get("NODES_TEST_DB_PORT", "6380"))


def git(repo, *a):
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=Ada", "-c", "user.email=ada@x.io", *a],
                          check=True, capture_output=True, text=True).stdout


@pytest.fixture()
def repo(tmp_path):
    (tmp_path / "web").mkdir()
    (tmp_path / "web" / "app.ts").write_text(
        'import { total } from "./cart";\n\nexport const TAX = 0.2;\n\nexport function price(x: number) {\n'
        '  return total(x) * (1 + TAX);\n}\n')
    (tmp_path / "web" / "cart.ts").write_text("export function total(x: number) { return x; }\n")
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-qm", "wip")
    (tmp_path / "web" / "cart.ts").write_text("export function total(x: number) { return x + 0; }\n")
    (tmp_path / "web" / "app.ts").write_text((tmp_path / "web" / "app.ts").read_text().replace("1 + TAX", "1.0 + TAX"))
    git(tmp_path, "commit", "-qam", "Round totals before tax (#42)")
    st = Store(f"v1test-{os.getpid()}-{time.time_ns()}", port=PORT)
    st.reset()
    st.setup()
    yield st, tmp_path
    st.reset()
    st.r.hdel("nodes:workspaces", st.ws)
    st.r.hdel("nodes:git", st.ws)


def index(st, root):
    stats = indexer.index(st, root, None, "r")
    units = stats.pop("_units")
    return stats, gitimport.import_git(st, root, units)


def test_typescript_units_and_region_leases(repo):
    st, root = repo
    stats, _ = index(st, root)
    assert stats["files_by_lang"]["typescript"] == 2
    kinds = dict(st.q("t", "MATCH (u:Unit) RETURN u.uid, u.kind", ro=True))
    assert kinds["region:r:web/app.ts#imports"] == "region" and kinds["region:r:web/app.ts#module@1"] == "region"
    assert kinds["fn:r:web/app.ts#price"] == "function"
    a, b = (dict(zip(("session", "name"), (ops.join(st, n)[1], n))) for n in ("alice", "bob"))
    assert ops.before_acting(st, a, "edit", ["web/app.ts:1"])["decision"] == "proceed"   # the import block
    assert ops.before_acting(st, b, "edit", ["web/app.ts:1"])["decision"] == "defer"
    edge = st.q("t", "MATCH (:Unit {uid: 'fn:r:web/app.ts#price'})-[d:DEPENDS_ON]->(x) RETURN x.uid, d.resolved_by",
                ro=True)
    assert edge == [["fn:r:web/cart.ts#total", "name-match"]]                            # labeled approximate


def test_git_import_prs_rationale_cochange_ownership(repo):
    st, root = repo
    _, g = index(st, root)
    assert g["new_commits"] == 2 and g["prs"] == 1
    rat = dict(st.q("t", "MATCH (u:Unit) WHERE u.rationale IS NOT NULL RETURN u.uid, u.rationale", ro=True))
    assert rat["fn:r:web/cart.ts#total"] == "Round totals before tax (#42) (PR #42)"
    assert rat["region:r:web/app.ts#imports"].startswith("no recorded rationale")       # only the "wip" commit
    assert st.q("t", "MATCH (:Unit {path: 'web/app.ts', kind: 'file'})-[c:CO_CHANGES]-(:Unit {path: 'web/cart.ts'}) "
                "RETURN c.support", ro=True) == [[2]]
    own = st.q("t", "MATCH (p:Principal)-[o:OWNS_CODE]->(:Unit {uid: 'fn:r:web/app.ts#price'}) RETURN p.uid, o.share",
               ro=True)
    assert own == [["person:ada@x.io", 1.0]]


def test_reinit_is_identical_and_one_commit_updates_only_changed_units(repo):
    st, root = repo
    index(st, root)
    snap = lambda: sorted(st.q("t", "MATCH (u:Unit) RETURN u.uid, u.hash, u.updated_at, u.status", ro=True))  # noqa
    before = snap()
    s2, g2 = index(st, root)
    assert snap() == before and s2["changed_units"] == 0 and g2["new_commits"] == 0     # idempotent
    (root / "web" / "cart.ts").write_text("export function total(x: number) { return x * 1; }\n")
    git(root, "commit", "-qam", "Use multiplication for totals")
    s3, g3 = index(st, root)
    assert g3["new_commits"] == 1
    changed = {u for u, h, t, s in snap()} and [u for (u, h, t, s), (u2, h2, t2, s2_) in zip(snap(), before) if h != h2]
    assert sorted(changed) == ["file:r:web/cart.ts", "fn:r:web/cart.ts#total"], changed


def test_hook_identity_routes_by_repo_and_ignores_uninitialized(repo, tmp_path_factory):
    st, root = repo
    index(st, root)
    common = subprocess.run(["git", "-C", str(root), "rev-parse", "--path-format=absolute", "--git-common-dir"],
                            capture_output=True, text=True).stdout.strip()
    st.r.hset("nodes:workspaces", st.ws, json.dumps({"root": str(root), "git_common_dir": common}))

    class Reg:  # the minimal registry surface identity needs
        r = st.r

        @staticmethod
        def store(ws):
            return st
    identity.git_common_dir.cache_clear()
    main = identity.session_for_hook(Reg, {"session_id": "abc123456", "cwd": str(root / "web")})
    sub = identity.session_for_hook(Reg, {"session_id": "abc123456", "cwd": str(root), "agent_id": "ag7",
                                          "agent_type": "Explore"})
    assert main["session"] == "cc:abc123456" and sub["session"] == "cc:abc123456:ag7" and sub["ws"] == st.ws
    assert st.q("t", "MATCH (:Session {uid: 'cc:abc123456'})-[:SPAWNED]->(s:Session) RETURN s.uid", ro=True) == \
        [["cc:abc123456:ag7"]]
    other = tmp_path_factory.mktemp("other")
    git(other, "init", "-q")
    n = st.q("t", "MATCH (s:Session) RETURN count(s)", ro=True)[0][0]
    assert identity.session_for_hook(Reg, {"session_id": "zzz", "cwd": str(other)}) is None   # not initialized
    assert st.q("t", "MATCH (s:Session) RETURN count(s)", ro=True)[0][0] == n                  # nothing stored


def test_redaction():
    out = redact.value({"headers": {"Authorization": "Bearer nds_abcdefghijk"}, "k": "sk-ant-api03-ABCDEFGHIJKLMNOPQR",
                        "tokens_used": 3, "cmd": "curl -H 'Authorization: Bearer abcdefghijkl'"})
    assert "nds_" not in json.dumps(out) and "sk-ant" not in json.dumps(out) and "abcdefghijkl" not in json.dumps(out)
    assert out["tokens_used"] == 3


def test_import_sessions_extracts_rationale_with_source_line(repo, tmp_path_factory):
    from nodes.sessions_import import import_sessions
    st, root = repo
    home = tmp_path_factory.mktemp("home")
    proj = home / ".claude" / "projects" / "x"
    proj.mkdir(parents=True)
    lines = [{"type": "user", "cwd": str(root), "message": {"content": "go"}},
             {"type": "assistant", "cwd": str(root), "message": {"content": [{"type": "text", "text":
              "I used a lease instead of a lock because a crashed agent would block everyone forever. "
              "Token was sk-ant-api03-ABCDEFGHIJKLMNOPQRSTUV because tests needed it. Done."}]}}]
    (proj / "s1.jsonl").write_text("\n".join(json.dumps(x) for x in lines), encoding="utf-8")
    common = subprocess.run(["git", "-C", str(root), "rev-parse", "--path-format=absolute", "--git-common-dir"],
                            capture_output=True, text=True).stdout.strip()
    identity.git_common_dir.cache_clear()
    out = import_sessions(st, common, home=home)
    assert out == {"transcripts": 1, "rationale_facts": 2}
    facts = dict(st.q("t", "MATCH (f:Fact {status: 'extracted'})-[:SOURCED_FROM]->(s:Source) RETURN f.text, s.uid", ro=True))
    assert facts["I used a lease instead of a lock because a crashed agent would block everyone forever."] == \
        "transcript:s1.jsonl#L2"
    assert all("sk-ant" not in t for t in facts)                                             # redacted
