"""Edge coverage for one repository: edge counts by type, and how many Python call sites resolve to a function in the
repo, with the reason for every one that doesn't. Independent of the resolver: it walks every ast.Call itself.

  python verify/edge_coverage.py C:/path/to/repo
"""
import ast
import builtins
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from nodes import indexer  # noqa: E402
from nodes.db import Store  # noqa: E402

BUILTINS = set(dir(builtins))


def classify(repo: Path, units: list[dict], edges: list[dict]) -> dict:
    """Every call inside a Python function: resolved (an edge from that function to a function with that name exists)
    or the reason it isn't: builtin, external (imported from outside the repo), attribute on an object, dynamic."""
    fns = {u["uid"]: u for u in units if u["kind"] == "function" and u.get("lang") == "python"}
    out_names = Counter()
    for e in edges:
        if e["src"] in fns:
            out_names[(e["src"], e["dst"].rsplit("#", 1)[-1].split(".")[-1])] += 1
            if e["dst"].endswith(".__init__"):
                out_names[(e["src"], e["dst"].rsplit("#", 1)[-1].split(".")[-2])] += 1
    repo_names = {u["symbol"].split(".")[-1] for u in fns.values()} | {u["symbol"].split(".")[0] for u in fns.values()}
    by_path = {}
    for u in fns.values():
        by_path.setdefault(u["path"], []).append(u)
    reasons, how = Counter(), Counter()
    for path, us in by_path.items():
        tree = ast.parse((repo / path).read_text(encoding="utf-8", errors="replace"))
        external = set()
        for n in ast.walk(tree):
            if isinstance(n, (ast.Import, ast.ImportFrom)):
                for a in n.names:
                    local = (a.asname or a.name).split(".")[0]
                    if isinstance(n, ast.ImportFrom) and n.level:
                        continue  # relative: inside the repo
                    mod = (n.module or a.name) if isinstance(n, ast.ImportFrom) else a.name
                    if not (repo / mod.split(".")[0]).exists() and not (repo / f"{mod.split('.')[0]}.py").exists():
                        external.add(local)
        spans = sorted(((u["start"], u["end"], u["uid"]) for u in us), key=lambda s: s[1] - s[0])
        for call in (n for n in ast.walk(tree) if isinstance(n, ast.Call)):
            owner = next((uid for s, e, uid in spans if s <= call.lineno <= e), None)
            if not owner:
                continue  # module-level code: not inside a function unit
            f = call.func
            name = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else None
            if name and out_names[(owner, name)]:
                reasons["resolved"] += 1
                how["self.method" if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) and f.value.id == "self"
                    else "plain name" if isinstance(f, ast.Name) else "attribute"] += 1
                continue
            if isinstance(f, ast.Name):
                reasons["builtin" if f.id in BUILTINS else "external import" if f.id in external
                        else "name not defined in the repo" if f.id not in repo_names else "unresolved plain name"] += 1
            elif isinstance(f, ast.Attribute):
                base = f.value
                root = base
                while isinstance(root, ast.Attribute):
                    root = root.value
                if isinstance(root, ast.Name) and root.id in external:
                    reasons["external library call (np., cv2., ...)"] += 1
                elif isinstance(base, ast.Name) and base.id in ("self", "cls"):
                    reasons["self.method not in this class or its repo bases"] += 1
                elif f.attr not in repo_names:
                    reasons["method of a library/builtin object (str, list, dict, ...)"] += 1
                else:
                    reasons["attribute call needing type info"] += 1
            else:
                reasons["dynamic (call result, subscript, lambda)"] += 1
    total = sum(reasons.values())
    return {"python_call_sites": total, "resolved": reasons["resolved"],
            "resolution_rate": round(reasons["resolved"] / total, 3) if total else None,
            "resolved_by_form": dict(how), "unresolved_reasons": dict(reasons.most_common()),
            "in_repo_rate": round(reasons["resolved"] / max(1, reasons["resolved"] + reasons["unresolved plain name"]
                                                          + reasons["self.method not in this class or its repo bases"]
                                                          + reasons["attribute call needing type info"]), 3)}


def main(repo: Path):
    st = Store(f"coverage-{repo.name}")
    st.reset()
    st.setup()
    stats = indexer.index(st, repo, None, repo.name)
    units = stats.pop("_units")
    edges = [{"src": a, "dst": b} for a, b in st.q("x", "MATCH (a:Unit)-[:DEPENDS_ON]->(b:Unit) RETURN a.uid, b.uid", ro=True)]
    rels = dict(st.q("x", "MATCH ()-[r]->() RETURN type(r), count(r)", ro=True))
    calls_by = dict(st.q("x", "MATCH ()-[d:DEPENDS_ON]->() RETURN d.resolved_by, count(d)", ro=True))
    report = {"repo": repo.name, "units": stats["units"], "functions": stats["functions"], "classes": stats["classes"],
              "edges_by_type": rels, "call_edges_by_resolver": calls_by, "files_by_lang": stats["files_by_lang"],
              **classify(repo, units, edges)}
    st.reset()
    return report


if __name__ == "__main__":
    print(json.dumps(main(Path(sys.argv[1])), indent=1))
