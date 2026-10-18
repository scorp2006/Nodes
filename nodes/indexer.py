"""Index a Python package into Units (the observer path: deterministic, no LLM).

Units (uid scheme `{kind}:{repo}:{path}#{symbol}`):
  file:{repo}:{path}                            a source file
  fn:{repo}:{path}#{Class.func}                 a function or method (qualname kept for the harness profile)
  region:{repo}:{path}#imports                  the module's import statements: a leasable unit, because the
                                                study's real breakages were textual collisions in such blocks
  region:{repo}:{path}#module@{n}               the n-th run of other module-level statements (aliases, constants)
  region:{repo}:{path}#{Class}.__body__         a class body's non-method statements (class attributes)
Edges: (file)-[:CONTAINS]->(unit); (fn)-[:DEPENDS_ON {kind:'calls', resolved_by:'ast-static', confidence}]->(fn).
Functions and calls come from the frozen study parser (coop/world_graph.py), so names resolve exactly as in the
harness; regions and line spans come from this module's own AST pass.
"""
from __future__ import annotations

import ast
import hashlib
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "coop"))
from world_graph import parse_repo  # noqa: E402  (frozen v1 parser: same names as the CooperBench harness)


def _regions(tree: ast.Module):
    """Yield (symbol, start, end) for import blocks, module-level statement runs and class bodies."""
    imports = [n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    if imports:
        yield "imports", min(n.lineno for n in imports), max(n.end_lineno for n in imports)
    run, k = [], 0
    for n in tree.body + [None]:
        plain = n is not None and not isinstance(n, (ast.Import, ast.ImportFrom, ast.FunctionDef,
                                                     ast.AsyncFunctionDef, ast.ClassDef))
        if plain:
            run.append(n)
        elif run:
            k += 1  # numbered by order, not line: an edit elsewhere never renames a block
            yield f"module@{k}", run[0].lineno, run[-1].end_lineno
            run = []
    for cls in (n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)):
        body = [s for s in cls.body if not isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
        if body:
            yield f"{cls.name}.__body__", body[0].lineno, body[-1].end_lineno


def _function_spans(tree: ast.Module):
    out = {}

    def visit(body, prefix):
        for n in body:
            if isinstance(n, ast.ClassDef):
                visit(n.body, f"{prefix}{n.name}.")
            elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                start = min([n.lineno] + [d.lineno for d in n.decorator_list])
                out[f"{prefix}{n.name}"] = (start, n.end_lineno)
    visit(tree.body, "")
    return out


def scan(repo_root: Path, package: str, repo: str) -> tuple[list[dict], list[dict]]:
    """Return (units, call edges) for `package` (a path relative to repo_root)."""
    pkg_dir = repo_root / package
    funcs, calls, _ = parse_repo(pkg_dir)
    parent = pkg_dir.parent
    by_qual = {}
    units: list[dict] = []
    for path in sorted(pkg_dir.rglob("*.py")):
        rel = path.relative_to(repo_root).as_posix()
        mod_parts = list(path.relative_to(parent).with_suffix("").parts)
        module = ".".join(mod_parts[:-1] if mod_parts[-1] == "__init__" else mod_parts)
        tree = ast.parse(path.read_text(encoding="utf-8"))
        units.append({"uid": f"file:{repo}:{rel}", "kind": "file", "path": rel, "symbol": "", "qualname": "",
                      "start": 1, "end": len(path.read_text(encoding="utf-8").splitlines()), "file": "", "line": 0})
        for cls in (n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)):
            units.append({"uid": f"class:{repo}:{rel}#{cls.name}", "kind": "class", "path": rel, "symbol": cls.name,
                          "qualname": "", "start": cls.lineno, "end": cls.end_lineno, "file": "", "line": 0})
        spans = _function_spans(tree)
        for sym, (start, end) in spans.items():
            qual = f"{module}.{sym}"
            u = {"uid": f"fn:{repo}:{rel}#{sym}", "kind": "function", "path": rel, "symbol": sym, "qualname": qual,
                 "start": start, "end": end, "file": "", "line": 0}
            units.append(u)
            by_qual[qual] = u
        for sym, start, end in _regions(tree):
            units.append({"uid": f"region:{repo}:{rel}#{sym}", "kind": "region", "path": rel, "symbol": sym,
                          "qualname": "", "start": start, "end": end, "file": "", "line": 0})
    for f in funcs:  # the study parser's file/line, used verbatim by the harness profile's impact_of text
        if f["qualname"] in by_qual:
            by_qual[f["qualname"]].update(file=f["file"], line=f["line"])
    edges = [{"src": by_qual[c["src"]]["uid"], "dst": by_qual[c["dst"]]["uid"], "by": "ast-static", "confidence": 0.8}
             for c in calls if c["src"] in by_qual and c["dst"] in by_qual]
    for u in units:
        u["lang"] = "python"
    return units, edges


def python_packages(root: Path) -> list[str]:
    """Top-level Python packages: dirs with __init__.py whose parent has none (tracked files only)."""
    from .lang import tracked_files
    inits = {str(Path(f).parent.as_posix()) for f in tracked_files(root) if f.endswith("__init__.py")}
    return sorted(d for d in inits if d != "." and str(Path(d).parent.as_posix()) not in inits)


def scan_all(repo_root: Path, package: str | None, repo: str) -> tuple[list[dict], list[dict], dict]:
    """Every language. Python packages use the precise resolver; all other files use tree-sitter (approximate calls)."""
    from .lang import scan_repo
    units, edges, done = [], [], set()
    for pkg in ([package] if package else python_packages(repo_root)):
        try:
            u, e = scan(repo_root, pkg, repo)
        except SyntaxError:
            continue  # a package that doesn't parse as Python falls back to tree-sitter
        units += u
        edges += e
        done |= {x["path"] for x in u if x["kind"] == "file"}
    tu, te, langs = scan_repo(repo_root, repo, done)
    langs["python (precise resolver)"] = len(done)
    return units + tu, edges + [{**e, "by": "name-match"} for e in te], langs


def index(store, repo_root: Path, package: str | None, repo: str, batch: int = 1000, all_languages: bool = True) -> dict:
    units, edges, langs = scan_all(repo_root, package, repo) if all_languages else (*scan(repo_root, package, repo), {})
    seen = Counter()
    for u in units:  # same symbol twice in one file (property setters, try/except fallbacks): later ones get @2, @3
        seen[u["uid"]] += 1
        if seen[u["uid"]] > 1:
            u["uid"] += f"@{seen[u['uid']]}"
    texts = {}
    for u in units:  # content hash per unit: re-indexing only touches units whose text changed
        if u["path"] not in texts:
            texts[u["path"]] = (repo_root / u["path"]).read_bytes().decode("utf-8", "replace").splitlines()
        body = "\n".join(texts[u["path"]][u["start"] - 1:u["end"]])
        u["hash"] = hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]
    now = int(time.time() * 1000)
    changed = 0
    for i in range(0, len(units), batch):
        changed += store.q("index.units", """
            UNWIND $rows AS r
            MERGE (u:Unit {uid: r.uid})
            ON CREATE SET u.status = 'active', u.lease_epoch = 0
            WITH u, r, (u.hash IS NULL OR u.hash <> r.hash OR u.status <> 'active') AS changed
            SET u.kind = r.kind, u.path = r.path, u.symbol = r.symbol, u.qualname = r.qualname, u.lang = r.lang,
                u.start = r.start, u.end = r.end, u.file = r.file, u.line = r.line, u.hash = r.hash, u.status = 'active',
                u.updated_at = CASE WHEN changed THEN $now ELSE u.updated_at END
            RETURN sum(CASE WHEN changed THEN 1 ELSE 0 END)
            """, {"rows": units[i:i + batch], "now": now})[0][0]
    gone = store.q("index.deleted", """
        MATCH (u:Unit) WHERE u.kind IN ['file', 'function', 'class', 'region'] AND u.status = 'active'
              AND NOT u.uid IN $uids
        SET u.status = 'deleted', u.updated_at = $now RETURN count(u)""",
        {"uids": [u["uid"] for u in units], "now": now})[0][0]
    store.q("index.contains", """
        MATCH (f:Unit {kind: 'file'}) MATCH (u:Unit) WHERE u.path = f.path AND u.kind <> 'file'
        MERGE (f)-[:CONTAINS]->(u)
        """)
    for i in range(0, len(edges), batch):
        store.q("index.calls", """
            UNWIND $rows AS r
            MATCH (a:Unit {uid: r.src}), (b:Unit {uid: r.dst})
            MERGE (a)-[d:DEPENDS_ON {kind: 'calls'}]->(b)
            SET d.resolved_by = r.by, d.confidence = r.confidence
            """, {"rows": edges[i:i + batch]})
    return {"units": len(units), "functions": sum(u["kind"] == "function" for u in units),
            "classes": sum(u["kind"] == "class" for u in units), "regions": sum(u["kind"] == "region" for u in units),
            "calls": len(edges), "approximate_calls": sum(e["by"] == "name-match" for e in edges), "files_by_lang": langs,
            "changed_units": changed, "deleted_units": gone, "_units": units}


def units_for_lines(store, path: str, lines: set[int]) -> list[str]:
    """The innermost unit (function, else region, else file) covering each changed line."""
    rows = store.q("units.for_path", "MATCH (u:Unit {path: $p}) WHERE u.start IS NOT NULL "
                   "RETURN u.uid, u.kind, u.start, u.end",
                   {"p": path}, ro=True)
    out = set()
    for ln in lines:
        cover = [r for r in rows if r[2] <= ln <= r[3] and r[1] != "file"]
        if cover:
            out.add(min(cover, key=lambda r: r[3] - r[2])[0])
        else:
            out.update(r[0] for r in rows if r[1] == "file")
    return sorted(out)
