"""Detector v4 signals (results/preregistration_detector4.md). No model calls.

  co-change  (:Function)-[:CO_CHANGES {support, confidence, resolved_by: 'git-history', window}]-(:Function)
             mined from the upstream repo's history BEFORE the task's base commit only
  coverage   (:Test)-[:EXERCISES {resolved_by: 'coverage', confidence: 1.0}]->(:Function)
             from one run of the task's target test files, as they exist at the base commit, with per-test contexts

  python coop/signals_v4.py cochange <repo> <task>     -> results/v4_signals/<repo>_<task>.cochange.json
  python coop/signals_v4.py coverage <repo> <task>     -> results/v4_signals/<repo>_<task>.coverage.json
Runs at below-normal CPU priority; coverage containers are capped (2 CPUs, low CPU shares, 4 GB).
"""
from __future__ import annotations

import ast
import ctypes
import os
import json
import re
import subprocess
import sys
from collections import Counter
from itertools import combinations
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
from prepare_repo import DATASET, PACKAGE  # noqa: E402

OUT = ROOT / "results" / "v4_signals"
MIRRORS = ROOT / "external" / "cc_mirror"
DOCKER = r"C:\Program Files\Docker\Docker\resources\bin\docker.exe"
MAX_COMMITS, MAX_PY_FILES, MAX_FUNCS, MIN_SUPPORT = 2000, 30, 50, 2   # fixed before any test task is mined

if sys.platform == "win32":  # below-normal priority, inherited by every git child process
    ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x4000)


def task_info(repo: str, task: str) -> tuple[str, str]:
    setup = (DATASET / repo / f"task{task}" / "setup.sh").read_text(encoding="utf-8")
    return re.search(r"git clone (\S+)", setup).group(1), re.search(r'BASE_COMMIT="([0-9a-f]{40})"', setup).group(1)


def module_of(path: str, pkg: str) -> str | None:
    """Repo-relative file path -> dotted module, named like world_graph_v2 (relative to the package's parent)."""
    parent = Path(pkg).parent.as_posix()
    rel = path[len(parent) + 1:] if parent != "." else path
    if not path.startswith(pkg + "/") or not rel.endswith(".py"):
        return None
    parts = rel[:-3].split("/")
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


def spans(source: str, mod: str) -> list[tuple[int, int, str]]:
    out = []

    def visit(body, prefix):
        for n in body:
            if isinstance(n, ast.ClassDef):
                visit(n.body, f"{prefix}.{n.name}")
            elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                out.append((min([n.lineno] + [d.lineno for d in n.decorator_list]), n.end_lineno, f"{prefix}.{n.name}"))
    visit(ast.parse(source).body, mod)
    return out


def innermost(items, line):
    inner = [s for s in items if s[0] <= line <= s[1]]
    return max(inner, key=lambda s: s[0])[2] if inner else None


# ------------------------------------------------------------------ co-change -------

def mirror(url: str) -> Path:
    dest = MIRRORS / (url.rstrip("/").split("/")[-1].removesuffix(".git") + ".git")
    if not dest.exists():
        MIRRORS.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", "-q", "--bare", url, str(dest)], check=True)
    return dest


def cochange(repo: str, task: str) -> dict:
    url, base = task_info(repo, task)
    pkg = PACKAGE[repo]
    git = ["git", "-C", str(mirror(url))]
    log = subprocess.run(git + ["log", "-p", "-U0", "--no-merges", "--no-renames", f"-n{MAX_COMMITS}",
                                "--format=@@@COMMIT %H", base, "--", pkg],
                         capture_output=True, text=True, encoding="utf-8", errors="replace").stdout
    commits, cur, path = [], None, None
    for line in log.splitlines():
        if line.startswith("@@@COMMIT "):
            cur = {"sha": line.split()[1], "files": {}}
            commits.append(cur)
        elif line.startswith("+++ "):
            path = line[6:] if line.startswith("+++ b/") else None
            if path and module_of(path, pkg):
                cur["files"].setdefault(path, [])
            else:
                path = None
        elif line.startswith("@@ ") and path:
            m = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", line)
            start, count = int(m.group(1)), int(m.group(2) or 1)
            cur["files"][path] += list(range(start, start + count)) if count else [max(start, 1)]
    cat = subprocess.Popen(git + ["cat-file", "--batch"], stdin=subprocess.PIPE, stdout=subprocess.PIPE)

    def blob(sha, p):
        cat.stdin.write(f"{sha}:{p}\n".encode())
        cat.stdin.flush()
        header = cat.stdout.readline().split()
        if len(header) < 3 or header[1] != b"blob":
            return None
        data = cat.stdout.read(int(header[2]) + 1)[:-1]
        return data.decode("utf-8", errors="replace")

    single, pairs, used, skipped = Counter(), Counter(), 0, 0
    for c in commits:
        if not c["files"] or len(c["files"]) > MAX_PY_FILES:
            skipped += bool(c["files"])
            continue
        fns = set()
        for p, lines in c["files"].items():
            src = blob(c["sha"], p)
            try:
                items = spans(src, module_of(p, pkg)) if src is not None else []
            except SyntaxError:
                continue
            fns |= {f for f in (innermost(items, ln) for ln in lines) if f}
        if not fns or len(fns) > MAX_FUNCS:
            skipped += bool(fns)
            continue
        used += 1
        single.update(fns)
        pairs.update(combinations(sorted(fns), 2))
    cat.stdin.close()
    cat.wait()
    edges = [{"a": a, "b": b, "support": n, "confidence": round(n / min(single[a], single[b]), 4),
              "resolved_by": "git-history", "window": used}
             for (a, b), n in pairs.items() if n >= MIN_SUPPORT]
    return {"repo": repo, "task": task, "base": base, "commits_seen": len(commits), "commits_used": used,
            "commits_skipped": skipped, "functions_changed": len(single), "edges": edges}


# ------------------------------------------------------------------ coverage -------

def test_files(repo: str, task: str) -> list[str]:
    files = set()
    for p in (DATASET / repo / f"task{task}").glob("feature*/tests.patch"):
        files |= set(re.findall(r"^\+\+\+ b/(\S+)", p.read_text(encoding="utf-8", errors="replace"), re.M))
    return sorted(f for f in files if f.endswith(".py"))


def install_dir(pkg: str) -> str:
    """The package's project dir: llama-index-core for llama_index, else the repo root."""
    return pkg.split("/")[0] if pkg.startswith("llama-index-core") else "."


def coverage(repo: str, task: str) -> dict:
    url, base = task_info(repo, task)
    pkg, files = PACKAGE[repo], test_files(repo, task)
    work = install_dir(pkg)
    rel = [f[len(work) + 1:] if work != "." else f for f in files]
    image = "akhatua/cooperbench-" + repo.replace("_task", "").replace("_", "-") + f":task{task}"
    out = OUT / "cov_raw" / f"{repo}_{task}"
    out.mkdir(parents=True, exist_ok=True)
    script = f"""
cd /workspace/repo && git checkout -q --force {base} && cd {work}
printf '[run]\\ndynamic_context = test_function\\nsource = /workspace/repo/{pkg}\\n' > /tmp/covrc
uv venv -q /tmp/venv && . /tmp/venv/bin/activate
if [ -f uv.lock ]; then uv sync -q --active >/dev/null 2>&1 || true; fi
uv pip install -q -e ".[test]" >/dev/null 2>&1 || uv pip install -q -e . >/dev/null 2>&1
uv pip install -q pytest pytest_mock coverage >/dev/null 2>&1
EXIST=""; for f in {' '.join(rel)}; do [ -f "$f" ] && EXIST="$EXIST $f"; done
echo "TEST_FILES_AT_BASE:$EXIST"
[ -n "$EXIST" ] && COVERAGE_FILE=/out/.coverage timeout 1800 python -m coverage run --rcfile=/tmp/covrc -m pytest -q -p no:cacheprovider $EXIST > /out/pytest.txt 2>&1
COVERAGE_FILE=/out/.coverage python -m coverage json --rcfile=/tmp/covrc --show-contexts -o /out/cov.json > /out/json.txt 2>&1
echo DONE
"""
    env = os.environ | {"PATH": str(Path(DOCKER).parent) + os.pathsep + os.environ["PATH"]}  # credential helper
    if subprocess.run([DOCKER, "image", "inspect", image], capture_output=True).returncode and \
            subprocess.run([DOCKER, "pull", "-q", image], capture_output=True, env=env).returncode:
        return {"repo": repo, "task": task, "base": base, "test_files": files, "status": "image unavailable",
                "exercises": {}}
    proc = subprocess.run([DOCKER, "run", "--rm", "--cpus", "2", "--cpu-shares", "256", "--memory", "4g",
                           "-v", f"{out}:/out", "--entrypoint", "sh", image, "-c", script],
                          capture_output=True, text=True, timeout=3600, env=env)
    res = {"repo": repo, "task": task, "base": base, "test_files": files,
           "at_base": (re.search(r"TEST_FILES_AT_BASE:(.*)", proc.stdout) or [None, ""])[1].split()}
    cov_file = out / "cov.json"
    if not cov_file.exists():
        return res | {"status": "coverage unavailable", "exercises": {}}
    data = json.loads(cov_file.read_text(encoding="utf-8"))
    src_root = ROOT / "external" / "cb_repos" / f"{repo}-{task}"
    exercises: dict[str, set] = {}
    for fname, fd in data.get("files", {}).items():
        path = (f"{work}/{fname}" if work != "." else fname).replace("\\", "/")
        mod = module_of(path, pkg)
        if not mod or not (src_root / path).exists():
            continue
        items = spans((src_root / path).read_text(encoding="utf-8"), mod)
        for line, ctxs in fd.get("contexts", {}).items():
            fn = innermost(items, int(line))
            for ctx in ctxs:
                if fn and ctx:
                    exercises.setdefault(ctx, set()).add(fn)
    return res | {"status": "ok", "tests": len(exercises),
                  "exercises": {t: sorted(fs) for t, fs in exercises.items()},
                  "edge_props": {"resolved_by": "coverage", "confidence": 1.0}}


if __name__ == "__main__":
    kind, repo, task = sys.argv[1:4]
    OUT.mkdir(parents=True, exist_ok=True)
    result = {"cochange": cochange, "coverage": coverage}[kind](repo, task)
    (OUT / f"{repo}_{task}.{kind}.json").write_text(json.dumps(result), encoding="utf-8")
    print({k: v for k, v in result.items() if k not in ("edges", "exercises")} |
          {"n_edges": len(result.get("edges", [])), "n_tests": len(result.get("exercises", {}))})
