"""End-to-end check of the demo with two real headless Claude Code agents running at the same time.

Each agent runs `claude -p` in its own worktree (branch alice / bob), so it loads the same .mcp.json and hooks
as an interactive session; the hooks identify it, no token. Afterwards we check, from the graph and from git:
  - which gate decisions happened (we expect the second agent to be deferred, then handed off)
  - both branches committed, both features present after merging, and the merged tree's tests pass
Uses the person's Claude login (model and effort below). Run setup_demo.py first; needs `python -m nodes up`.

  python demo/e2e_test.py [--dir PATH] [--model claude-sonnet-5-5] [--effort low]
"""
import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

TOOLS = "Read,Edit,Write,MultiEdit,Glob,Grep,Bash(git:*),Bash(*pytest*),mcp__nodes__*"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=str(Path.home() / "nodes-demo"))
    ap.add_argument("--model", default="claude-sonnet-5-5")
    ap.add_argument("--effort", default="low")
    ap.add_argument("--port", type=int, default=8780)
    ap.add_argument("--timeout", type=int, default=1500)
    a = ap.parse_args()
    base = Path(a.dir)
    agents = {k: v for k, v in json.loads((base / "agents.json").read_text(encoding="utf-8")).items()
              if not k.startswith("_")}
    procs = {}
    t0 = time.time()
    for name, info in agents.items():
        env = dict(os.environ)
        env.pop("ANTHROPIC_API_KEY", None)
        log = open(base / f"{name}.claude.json", "w", encoding="utf-8")
        procs[name] = (subprocess.Popen(["claude", "-p", "--output-format", "json", "--model", a.model, "--effort",
                                         a.effort, "--permission-mode", "acceptEdits", "--allowedTools", TOOLS],
                                        cwd=info["worktree"], env=env, stdin=subprocess.PIPE, stdout=log,
                                        stderr=subprocess.STDOUT, text=True), log)
        procs[name][0].stdin.write(info["prompt"])
        procs[name][0].stdin.close()
        time.sleep(5)  # alice starts first, so she reaches the shared code first
    for name, (p, log) in procs.items():
        try:
            p.wait(timeout=max(1, a.timeout - (time.time() - t0)))
        except subprocess.TimeoutExpired:
            p.kill()
        log.close()
    wall = round(time.time() - t0)
    state = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{a.port}/api/state?ws=shop").read())
    repo = base / "shop"
    git = lambda *c, cwd=repo: subprocess.run(["git", *c], cwd=cwd, capture_output=True, text=True)  # noqa: E731
    commits = {n: git("log", "--oneline", f"main..{n}").stdout.strip().splitlines() for n in agents}
    git("checkout", "-q", "-b", "e2e-merge", "main")
    merges = {n: git("-c", "user.name=e2e", "-c", "user.email=e2e@x", "merge", "--no-edit", n).returncode == 0
              for n in agents}
    py = base / ".venv" / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    tests = subprocess.run([str(py), "-m", "pytest", "-q"], cwd=repo, capture_output=True, text=True)
    models = (repo / "src" / "shop" / "models.py").read_text(encoding="utf-8")
    summary = {
        "wall_seconds": wall, "model": a.model, "effort": a.effort,
        "decisions": [[d["agent"], d["verdict"], d["path"]] for d in reversed(state["decisions"])],
        "verdict_counts": {v: sum(d["verdict"] == v for d in state["decisions"])
                           for v in ("proceed", "defer", "handoff", "escalate")},
        "sessions": [[s["name"], s["status"], s["commit"]] for s in state["sessions"]],
        "branch_commits": commits, "merged_cleanly": merges,
        "both_features_present": all(k in models for k in ("email_verified", "points", "verify_email", "award_points")),
        "merged_tests": tests.stdout.strip().splitlines()[-1:] if tests.stdout else tests.stderr[-300:],
        "agents_exit": {n: p.returncode for n, (p, _) in procs.items()},
    }
    print(json.dumps(summary, indent=1))


if __name__ == "__main__":
    main()
