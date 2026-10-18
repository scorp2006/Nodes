"""Audit fixes: Bash writes are gated, fail-closed hook config, STALE_EPOCH on writes after a lease ended, waiters
notified when a lease frees, full-text fact search.   python -m pytest nodes/test_audit.py -q   (FalkorDB on 6380)
"""
import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

from nodes import ops
from nodes.__main__ import config_files
from nodes.shellwrites import write_targets
from nodes.test_product import PORT, free_port, ws  # noqa: F401  (fixture)


@pytest.mark.parametrize("cmd, expected", [
    ("sed -i 's/a/b/' pkg/m.py pkg/n.py", ["pkg/m.py", "pkg/n.py"]),
    ("sed -i.bak -e 's/a/b/' pkg/m.py", ["pkg/m.py"]),
    ("perl -pi -e 's/a/b/' pkg/m.py", ["pkg/m.py"]),
    ("echo x > pkg/m.py && echo y >> pkg/n.py 2>&1", ["pkg/m.py", "pkg/n.py"]),
    ("cat notes | tee -a pkg/m.py", ["pkg/m.py"]),
    ("cp /tmp/x.py pkg/m.py", ["pkg/m.py"]),
    ("mv pkg/old.py pkg/new.py", ["pkg/new.py", "pkg/old.py"]),
    ("python -c \"open('pkg/m.py', 'w').write('x')\"", ["pkg/m.py"]),
    ("python - <<'EOF'\nfrom pathlib import Path\nPath('pkg/m.py').write_text('x')\nEOF", ["pkg/m.py"]),
    ("cat > pkg/new.py <<EOF\nx = 1 > 0\nEOF", ["pkg/new.py"]),
    ("git checkout -- pkg/m.py", ["pkg/m.py"]),
    ("git restore --source HEAD~1 pkg/m.py", ["pkg/m.py"]),
    ("git checkout main", []),
    ("python -c \"print(2 > 1)\"", []),
    ("pytest -q > /dev/null 2>&1", []),
])
def test_shell_write_targets(cmd, expected):
    assert write_targets(cmd)[0] == expected


def test_script_writing_unnamed_files_is_opaque():
    assert write_targets("python -c \"open(name, 'w').write(x)\"") == ([], True)


def test_hooks_fail_closed_by_default():
    for mode, want in (("closed", "block"), ("open", "continue")):
        _, settings = config_files(8780, fail_mode=mode)
        pre = [h for m in settings["hooks"]["PreToolUse"] for h in m["hooks"]]
        assert pre and all(h["onFailure"] == want for h in pre)


@pytest.fixture()
def server(ws, tmp_path):  # noqa: F811
    st, root = ws
    st.r.hset("nodes:workspaces", st.ws, json.dumps({"root": str(root), "repo": "t", "package": "pkg"}))
    port = free_port()
    code = (f"import sys; sys.path.insert(0, r'{Path(__file__).resolve().parents[1]}'); from pathlib import Path; "
            f"from nodes.server import Registry, run; run(Registry('localhost', {PORT}, Path(r'{tmp_path}')), "
            f"'nodes', '127.0.0.1', {port})")
    proc = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(150):  # up to 30 s: imports are slow when the machine is busy
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state?ws={st.ws}")
            break
        except Exception:  # noqa: BLE001
            time.sleep(0.2)

    def hook(path, token, body):
        req = urllib.request.Request(f"http://127.0.0.1:{port}/hooks/{path}", data=json.dumps(body).encode(),
                                     method="POST", headers={"Authorization": f"Bearer {token}",
                                                             "Content-Type": "application/json"})
        return json.loads(urllib.request.urlopen(req).read()).get("hookSpecificOutput", {})
    yield st, root, hook
    subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True)


def test_bash_writes_are_gated_like_edits(server):
    st, root, hook = server
    ta, _ = ops.join(st, "alice", worktree=str(root))
    tb, _ = ops.join(st, "bob", worktree=str(root))
    edit = {"tool_name": "Edit", "cwd": str(root), "tool_input": {"file_path": str(root / "pkg" / "m.py"),
                                                                  "old_string": "def edit():", "new_string": "def edit():"}}
    assert "permissionDecision" not in hook("pre-tool-use", ta, edit)               # alice holds pkg/m.py#edit
    for cmd in ("sed -i 's/edit/edit2/' pkg/m.py", "echo x >> pkg/m.py", "cp /tmp/y.py pkg/m.py",
                "python -c \"open('pkg/m.py','w').write('')\"", "git checkout -- pkg/m.py"):
        out = hook("pre-tool-use", tb, {"tool_name": "Bash", "cwd": str(root), "tool_input": {"command": cmd}})
        assert out.get("permissionDecision") == "deny" and "DEFER" in out["permissionDecisionReason"], cmd
    out = hook("pre-tool-use", tb, {"tool_name": "Bash", "cwd": str(root), "tool_input": {"command": "pytest -q"}})
    assert "permissionDecision" not in out                                           # reads are never gated


def test_write_after_release_or_done_is_stale(server):
    st, root, hook = server
    ta, sa = ops.join(st, "alice", worktree=str(root))
    edit = {"tool_name": "Edit", "cwd": str(root), "tool_input": {"file_path": str(root / "pkg" / "m.py"),
                                                                  "old_string": "return helper()",
                                                                  "new_string": "return helper()"}}
    alice = {"session": sa, "name": "alice"}
    for i, end in enumerate((lambda: ops.coordinate(st, alice, "release"),
                             lambda: ops.coordinate(st, alice, "done", text="c0ffee"))):
        body = edit | {"tool_use_id": f"toolu_{i}"}
        assert "permissionDecision" not in hook("pre-tool-use", ta, body)   # gate grants the lease + a ticket
        end()                                                                 # lease ends before the write lands
        assert "STALE_EPOCH" in hook("post-tool-use", ta, body).get("additionalContext", "")
    st.q("t", "MATCH (s:Session {uid: $s}) SET s.status = 'active'", {"s": sa})
    body = edit | {"tool_use_id": "toolu_ok"}
    hook("pre-tool-use", ta, body)
    assert hook("post-tool-use", ta, body) == {}                              # gated and still held: fine


def test_deferred_agent_is_notified_when_the_lease_frees(ws):  # noqa: F811
    st, _ = ws
    _, sa = ops.join(st, "alice")
    _, sb = ops.join(st, "bob")
    alice, bob = {"session": sa, "name": "alice"}, {"session": sb, "name": "bob"}
    assert ops.before_acting(st, alice, "edit", ["pkg.m.edit"])["decision"] == "proceed"
    assert ops.before_acting(st, bob, "edit", ["pkg.m.edit"])["decision"] == "defer"   # WAITS_FOR edge
    sub = st.r.pubsub()
    sub.subscribe(f"ws:{st.ws}:notify:bob")
    sub.get_message(timeout=1)
    ops.coordinate(st, alice, "release")
    msg = next(m for m in iter(lambda: sub.get_message(timeout=2), None) if m["type"] == "message")
    assert json.loads(msg["data"])["kind"] == "release"
    assert ops.before_acting(st, bob, "edit", ["pkg.m.edit"])["decision"] == "proceed"


def test_context_for_finds_facts_by_full_text(ws):  # noqa: F811
    st, root = ws
    _, sa = ops.join(st, "alice")
    alice = {"session": sa, "name": "alice"}
    ops.post_fact(st, alice, 'VERSION = "1"', {"kind": "file", "ref": "pkg/m.py"}, root)
    ops.post_fact(st, alice, "The billing retries use exponential backoff", {"kind": "note", "ref": "x"})
    got = ops.context_for(st, alice, "how does billing retry?")["data"]["facts_matching"]
    assert [f["text"] for f in got] == ["The billing retries use exponential backoff"]
    assert ops.context_for(st, alice, "")["data"]["facts_matching"] == []


def test_plugin_files_match_the_init_config():
    from nodes.__main__ import plugin_files
    root = Path(__file__).resolve().parents[1] / "plugin"
    hooks, mcp = plugin_files()
    assert json.loads((root / "hooks" / "hooks.json").read_text(encoding="utf-8")) == hooks
    assert json.loads((root / ".mcp.json").read_text(encoding="utf-8")) == mcp


def test_plugin_gate_fails_closed_only_in_initialized_repos(tmp_path):
    gate = Path(__file__).resolve().parents[1] / "plugin" / "hooks" / "gate.sh"
    dead = str(free_port())  # nothing listens here: the server is "down"

    def run(repo):
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        env = {**__import__("os").environ, "NODES_PORT": dead, "CLAUDE_PROJECT_DIR": str(repo)}
        return subprocess.run(["bash", str(gate)], input='{"tool_name": "Edit"}', capture_output=True, text=True,
                              env=env).stdout
    assert run(tmp_path / "plain") == ""                                   # never initialized: no opinion
    repo = tmp_path / "inited"
    run(repo)
    (repo / ".git" / "nodes.json").write_text('{"fail_mode": "closed"}')
    out = json.loads(run(repo))["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny" and "unreachable" in out["permissionDecisionReason"]
    (repo / ".git" / "nodes.json").write_text('{"fail_mode": "open"}')
    assert run(repo) == ""
