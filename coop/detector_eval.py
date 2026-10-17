"""Offline accuracy of our conflict check on CooperBench's published coop trajectories (light repos, Claude, k1).

For each published pair: take each agent's final patch, map changed lines to the functions they fall in (repo at
the task's base commit), declare those as the agents' plans in a world graph, run the same overlap query that
check_conflicts uses (CALLS*0..3), and compare "flagged" with what actually happened when the two patches were
merged (merge_analysis.status in the published merge report). A naive baseline flags pairs that touch a common file.
No model calls. Downloaded files are only read as text.

  python coop/detector_eval.py
"""
import ast
import json
import re
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import coop_tools as ct  # noqa: E402
from prepare_repo import prepare  # noqa: E402
from world_graph import load, parse_repo  # noqa: E402

BASE = "https://huggingface.co/datasets/CooperBench/trajectories/resolve/main/coop"
CACHE = HERE.parent / "external" / "cb_trajectories"
TASKS = [("pallets_click_task", "2068"), ("pallets_click_task", "2800"), ("pallets_click_task", "2956"),
         ("pallets_jinja_task", "1465"), ("pallets_jinja_task", "1559"), ("pallets_jinja_task", "1621"),
         ("samuelcolvin_dirty_equals_task", "43")]


def fetch(rel: str) -> str | None:
    dest = CACHE / rel
    if not dest.exists():
        try:
            data = urllib.request.urlopen(f"{BASE}/{rel}", timeout=60).read()
        except Exception:
            return None
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
    return dest.read_text(encoding="utf-8", errors="replace")


def changed_lines(patch: str) -> dict[str, set[int]]:
    """{file: old-file line numbers touched}. Deletions count their line; insertions count the line before."""
    out, file, old = {}, None, 0
    for line in patch.splitlines():
        if line.startswith("--- "):
            continue
        if line.startswith("+++ "):
            file = line[6:] if line.startswith("+++ b/") else None
            continue
        m = re.match(r"@@ -(\d+)(?:,\d+)? \+\d+(?:,\d+)? @@", line)
        if m:
            old = int(m.group(1))
            continue
        if file is None or not line or line[0] not in " +-":
            continue
        if line[0] == "-":
            out.setdefault(file, set()).add(old)
            old += 1
        elif line[0] == "+":
            out.setdefault(file, set()).add(max(old - 1, 1))
        else:
            old += 1
    return out


def function_spans(repo: Path, pkg: str) -> dict[str, list[tuple[int, int, str]]]:
    """{repo-relative file: [(start, end, qualname)]}, qualnames matching world_graph.parse_repo."""
    root = repo / pkg
    spans = {}
    for path in root.rglob("*.py"):
        rel_mod = path.relative_to(root.parent).with_suffix("")
        parts = list(rel_mod.parts)
        mod = ".".join(parts[:-1] if parts[-1] == "__init__" else parts)
        tree = ast.parse(path.read_text(encoding="utf-8"))
        items = []

        def visit(body, prefix):
            for n in body:
                if isinstance(n, ast.ClassDef):
                    visit(n.body, f"{prefix}.{n.name}")
                elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    start = min([n.lineno] + [d.lineno for d in n.decorator_list])
                    items.append((start, n.end_lineno, f"{prefix}.{n.name}"))

        visit(tree.body, mod)
        spans[path.relative_to(repo).as_posix()] = items
    return spans


def touched_functions(patch: str, spans) -> tuple[set[str], set[str]]:
    funcs, files = set(), set()
    for file, lines in changed_lines(patch).items():
        if not file.endswith(".py") or "/tests/" in f"/{file}" or file.startswith("tests/"):
            continue
        files.add(file)
        for ln in lines:
            inner = [s for s in spans.get(file, []) if s[0] <= ln <= s[1]]
            if inner:
                funcs.add(max(inner, key=lambda s: s[0])[2])  # innermost
    return funcs, files


def pairs_for(repo: str, task: str) -> list[tuple[int, int]]:
    sp = json.loads((HERE / "splits.json").read_text(encoding="utf-8"))
    return [tuple(p) for p in sp["pairs"][f"{repo}/{task}"]]


def main():
    rows = []
    for repo, task in TASKS:
        repo_dir, pkg = prepare(repo, task)
        funcs, calls, _ = parse_repo(repo_dir / pkg)
        spans = function_spans(repo_dir, pkg)
        graph = f"cbdet_{repo}_{task}"
        g = load(graph, funcs, calls)
        for a in ("agent1", "agent2"):
            g.query("MERGE (:Agent {id: $a})", {"a": a})

        def get(pair):
            i, j = pair
            d = f"{repo}/task{task}/feature{i}_feature{j}"
            return pair, (fetch(f"{d}/patch_claude_k1_feature{i}.patch"), fetch(f"{d}/patch_claude_k1_feature{j}.patch"),
                          fetch(f"{d}/claude_{repo}_task{task}_merge_report_f{i}_into_f{j}_k1.json"))

        with ThreadPoolExecutor(8) as pool:
            fetched = list(pool.map(get, pairs_for(repo, task)))
        for (i, j), (p1, p2, rep) in fetched:
            if not (p1 and p2 and rep):
                continue
            try:
                status = json.loads(rep)["merge_analysis"]["status"]
            except (ValueError, KeyError):
                continue
            f1, files1 = touched_functions(p1, spans)
            f2, files2 = touched_functions(p2, spans)
            ct.set_plan(g, "agent1", sorted(f1))
            ct.set_plan(g, "agent2", sorted(f2))
            flagged = bool(ct.overlaps(g, "agent1"))
            same_fn = bool(f1 & f2)
            rows.append({"repo": repo, "task": task, "pair": [i, j], "conflict": status != "clean",
                         "flag_graph": flagged, "flag_same_function": same_fn, "flag_same_file": bool(files1 & files2),
                         "n_funcs": [len(f1), len(f2)]})
        g.delete()
    (HERE.parent / "results").mkdir(exist_ok=True)
    (HERE.parent / "results" / "detector_eval_rows.json").write_text(json.dumps(rows, indent=1), encoding="utf-8")
    report(rows)


def report(rows):
    n = len(rows)
    pos = sum(r["conflict"] for r in rows)
    print(f"pairs with published Claude coop runs (light repos): {n}; actually conflicted: {pos} ({pos / max(n, 1):.0%})")
    for key, label in (("flag_graph", "graph overlap (check_conflicts, <=3 calls)"),
                       ("flag_same_function", "same function edited"), ("flag_same_file", "same file edited (naive)")):
        tp = sum(r[key] and r["conflict"] for r in rows)
        fp = sum(r[key] and not r["conflict"] for r in rows)
        fn = sum(not r[key] and r["conflict"] for r in rows)
        tn = n - tp - fp - fn
        prec = tp / (tp + fp) if tp + fp else float("nan")
        rec = tp / (tp + fn) if tp + fn else float("nan")
        print(f"  {label:44} recall {rec:5.0%}  precision {prec:5.0%}  accuracy {(tp + tn) / max(n, 1):5.0%}  "
              f"(tp {tp}, fp {fp}, fn {fn}, tn {tn})")


def demo():
    p = "--- a/x.py\n+++ b/x.py\n@@ -10,3 +10,4 @@\n a\n-b\n+c\n+d\n e\n"
    assert changed_lines(p) == {"x.py": {11}}, changed_lines(p)


if __name__ == "__main__":
    demo()
    main()
