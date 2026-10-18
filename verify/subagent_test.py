"""One Claude Code session runs 3 parallel subagents on separate tasks; check that Nodes tells them apart.

Checks (from the graph and the server trace, after the run):
  - each subagent got its own Session (cc:<session_id>:<agent_id>) linked by SPAWNED to the parent session
  - Nodes MCP calls made inside a subagent were stamped with that subagent's agent_id by the PreToolUse hook
  - each subagent's edits were gated (Decision nodes) under its own identity
Uses the person's Claude login (one headless session). Needs `python -m nodes up` on :8780.

  python verify/subagent_test.py [--dir C:/dev/nodes-subagents] [--model claude-sonnet-5-5]
"""
import argparse
import json
import os
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROMPT = """Use the Agent tool to launch exactly 3 general-purpose subagents IN PARALLEL (one message, three tool
calls). Give each one of these tasks verbatim, and tell each to first call the Nodes MCP tool declare_intent for the
code it will change, and to stop when its task is done:
1. In src/shop/models.py add a function `discount_cents(order: Order, pct: int) -> int` returning order_total(order) * pct // 100.
2. In src/shop/api.py add a function `order_count(orders: list) -> int` returning len(orders).
3. Create src/shop/util.py with a function `cents_to_dollars(c: int) -> str` returning f"${c / 100:.2f}".
After all three finish, reply with one line: done."""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default="C:/dev/nodes-subagents")
    ap.add_argument("--model", default="claude-sonnet-5-5")
    ap.add_argument("--port", type=int, default=8780)
    a = ap.parse_args()
    base = Path(a.dir)
    if base.exists():
        shutil.rmtree(base, onexc=lambda f, p, _: (os.chmod(p, stat.S_IWRITE), f(p)))
    repo = base / "shop"
    shutil.copytree(ROOT / "demo" / "shop", repo)
    for c in (["init", "-q", "-b", "main"], ["add", "-A"], ["-c", "user.name=t", "-c", "user.email=t@x", "commit", "-qm", "init"]):
        subprocess.run(["git", "-C", str(repo), *c], check=True, capture_output=True)
    init = subprocess.run([sys.executable, "-m", "nodes", "init", "--repo", str(repo), "--workspace", "subtest",
                           "--fresh", "--port", str(a.port)], cwd=ROOT, capture_output=True, text=True, check=True)
    print("init:", json.loads(init.stdout)["index"]["units"], "units")
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    t0 = time.time()
    out = subprocess.run(["claude", "-p", "--output-format", "json", "--model", a.model, "--effort", "low",
                          "--permission-mode", "acceptEdits",
                          "--allowedTools", "Read,Edit,Write,Glob,Grep,Agent,Task,mcp__nodes__*"],
                         cwd=repo, env=env, input=PROMPT, capture_output=True, text=True, timeout=1200)
    print(f"claude finished in {time.time() - t0:.0f} s, exit {out.returncode}")
    sys.path.insert(0, str(ROOT))
    from nodes.db import Store
    st = Store("subtest")
    sessions = st.q("x", "MATCH (s:Session) WHERE s.uid STARTS WITH 'cc:' OPTIONAL MATCH (p:Session)-[:SPAWNED]->(s) "
                    "RETURN s.uid, s.name, s.agent_type, p.uid ORDER BY s.uid", ro=True)
    decisions = st.q("x", "MATCH (s:Session)-[:MADE]->(d:Decision) RETURN s.name, d.verdict, d.path ORDER BY d.at", ro=True)
    intents = st.q("x", "MATCH (s:Session)-[:DECLARED]->(i:Intent) RETURN s.name, i.summary", ro=True)
    trace = ROOT / ".nodes" / "traces" / "subtest.trace.jsonl"
    stamped = [json.loads(x) for x in trace.read_text(encoding="utf-8").splitlines()] if trace.exists() else []
    mcp = [(r["agent"], r["name"], (r["request"].get("caller") or {}).get("agent_id", "")) for r in stamped
           if r["kind"].startswith("tool")]
    subs = [s for s in sessions if s[3]]
    print(json.dumps({"sessions": sessions, "subagent_sessions": len(subs),
                      "mcp_calls(agent, tool, stamped agent_id)": mcp, "intents": intents,
                      "decisions": decisions}, indent=1))
    print("PASS" if len(subs) >= 3 and all(m[2] for m in mcp if m[0] != "main") and
          len({d[0] for d in decisions} & {s[1] for s in subs}) >= 3 else "CHECK")


if __name__ == "__main__":
    main()
