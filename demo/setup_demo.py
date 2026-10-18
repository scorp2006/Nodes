"""Set up the two-agent demo: a fresh `shop` git repo, a venv with pytest, the Nodes workspace, and one git worktree +
bearer token + launcher per agent. Needs a running server: `python -m nodes up` (port 8780).

  python demo/setup_demo.py [--dir PATH] [--port 8780]
Then: demo/start_agents.ps1 (two Claude Code windows) or `python demo/e2e_test.py` (headless, scripted check).
"""
import argparse
import json
import os
import shutil
import stat
import subprocess
import sys
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
NODES = HERE.parent
AGENTS = {"alice": "TASK_A.md", "bob": "TASK_B.md"}
PROMPT = ("You are {agent}, one of two coding agents working in parallel on this repo, each in its own git worktree "
          "and branch. Your task is in {task}. You coordinate through the Nodes MCP tools: start by calling "
          "declare_intent with the units you will change (the User model, the module-level __all__ block, functions). "
          "Every edit is gated automatically: if the gate says DEFER, a teammate owns that code, so do your other "
          "parts first and then call the Nodes status tool with wait_seconds=60 until it frees up; if it says "
          "HANDOFF, run `git merge <their branch>` first, then make your change on top. Run the tests with "
          "`{python} -m pytest -q`. When your tests pass, commit, then call coordinate with op='done' and the commit "
          "sha as text. Do not edit files outside this worktree.")


def run(*cmd, cwd=None):
    return subprocess.run(list(cmd), cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=str(Path.home() / "nodes-demo"))
    ap.add_argument("--port", type=int, default=8780)
    a = ap.parse_args()
    base = Path(a.dir).resolve()
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{a.port}/api/state", timeout=3)
    except Exception:  # noqa: BLE001
        sys.exit(f"No Nodes server on port {a.port}. Start it first:  python -m nodes up")
    if base.exists():
        for wt in AGENTS:  # remove old worktrees first so git forgets them
            subprocess.run(["git", "-C", str(base / "shop"), "worktree", "remove", "--force", str(base / wt)],
                           capture_output=True)
        shutil.rmtree(base, onexc=lambda f, p, _: (os.chmod(p, stat.S_IWRITE), f(p)))  # git objects are read-only
    repo = base / "shop"
    shutil.copytree(HERE / "shop", repo, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
    run("git", "init", "-q", "-b", "main", cwd=repo)
    (repo / ".git" / "info" / "exclude").write_text(".mcp.json\n.claude/\n__pycache__/\n.pytest_cache/\n")
    run("git", "add", "-A", cwd=repo)
    run("git", "-c", "user.name=demo", "-c", "user.email=demo@example.com", "commit", "-q", "-m", "initial", cwd=repo)
    venv = base / ".venv"
    subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
    py = venv / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    subprocess.run([str(py), "-m", "pip", "install", "-q", "pytest"], check=True)
    nodes = [sys.executable, "-m", "nodes"]
    print(run(*nodes, "init", "--repo", str(repo), "--package", "src/shop", "--workspace", "shop",
              "--port", str(a.port), cwd=NODES))
    launch = {}
    for agent, task in AGENTS.items():
        token = run(*nodes, "join", "--workspace", "shop", "--agent", agent, "--task", task, "--worktree",
                    str(base / agent), "--branch", agent, "--quiet", cwd=NODES).splitlines()[-1]
        for f in ("user.name", "user.email"):
            run("git", "config", f, agent if f == "user.name" else f"{agent}@example.com", cwd=base / agent)
        prompt = PROMPT.format(agent=agent, task=task, python=py.as_posix())
        launch[agent] = {"token": token, "worktree": str(base / agent), "prompt": prompt}
    (base / "agents.json").write_text(json.dumps(launch, indent=1), encoding="utf-8")
    print(f"demo ready in {base}\n  agents: {', '.join(AGENTS)} (worktrees {base / 'alice'}, {base / 'bob'})\n"
          f"  dashboard: http://127.0.0.1:{a.port}/?ws=shop\n  next: powershell -File {HERE / 'start_agents.ps1'}")


if __name__ == "__main__":
    main()
