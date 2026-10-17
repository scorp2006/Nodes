"""Load a Python repo into FalkorDB as a world graph, and check two plans for conflicts.

  (:File {path})-[:CONTAINS]->(:Function {qualname, name, file, line})
  (:Function)-[:CALLS]->(:Function)

The source is only parsed (ast), never imported or executed.

  python coop/world_graph.py <repo_dir> <src_subdir> <graph_name>
"""
from __future__ import annotations

import ast
import sys
import time
from pathlib import Path

from falkordb import FalkorDB
from redis.exceptions import ResponseError


def module_name(path: Path, root: Path) -> str:
    rel = path.relative_to(root).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def parse_repo(root: Path) -> tuple[list[dict], list[dict], dict]:
    """Return (functions, calls, stats). Call resolution is static and best-effort.

    ponytail: resolves plain names, self.method, imported names/modules, Class(...) as
    Class.__init__, and `x = Class(...); x.method()` inside one function. Calls through
    attributes, parameters, inheritance or dynamic dispatch stay unresolved. A
    type-aware resolver (e.g. jedi/pyright) is the upgrade path.
    """
    funcs: list[dict] = []
    raw_calls: list[tuple[str, str, str | None, dict]] = []  # caller, callee name, kind, scope
    for path in sorted(root.rglob("*.py")):
        mod = module_name(path, root.parent)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imports: dict[str, str] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module is not None:
                base = node.module if node.level == 0 else ".".join(mod.split(".")[: -node.level] + [node.module])
                for a in node.names:
                    imports[a.asname or a.name] = f"{base}.{a.name}"
            elif isinstance(node, ast.Import):
                for a in node.names:
                    imports[a.asname or a.name] = a.name

        def visit(body, prefix: str, cls: str | None):
            for node in body:
                if isinstance(node, ast.ClassDef):
                    visit(node.body, f"{prefix}.{node.name}", node.name)
                elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    q = f"{prefix}.{node.name}"
                    funcs.append({"qualname": q, "name": node.name, "file": str(path.relative_to(root.parent)),
                                  "line": node.lineno})
                    scope = imports | {"__mod__": mod}
                    # local `x = Name(...)`: remember the constructor name to resolve x.method() later
                    local_types = {
                        sub.targets[0].id: sub.value.func.id
                        for sub in ast.walk(node)
                        if isinstance(sub, ast.Assign) and len(sub.targets) == 1
                        and isinstance(sub.targets[0], ast.Name) and isinstance(sub.value, ast.Call)
                        and isinstance(sub.value.func, ast.Name)
                    }
                    for sub in ast.walk(node):
                        if isinstance(sub, ast.Call):
                            f = sub.func
                            if isinstance(f, ast.Name):
                                raw_calls.append((q, f.id, None, scope))  # also tried as f.id.__init__
                            elif isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name):
                                if f.value.id in ("self", "cls") and cls:
                                    raw_calls.append((q, f"{prefix}.{f.attr}", "method", {}))
                                elif f.value.id in imports:
                                    raw_calls.append((q, f"{imports[f.value.id]}.{f.attr}", "qualified", {}))
                                elif f.value.id in local_types:
                                    raw_calls.append((q, f"{local_types[f.value.id]}.{f.attr}", None, scope))

        visit(tree.body, mod, None)

    known = {f["qualname"] for f in funcs}
    calls, unresolved = set(), 0
    for caller, name, kind, scope in raw_calls:
        if kind in ("method", "qualified"):
            target = name if name in known else None
        else:
            head, _, rest = name.partition(".")
            stems = [f"{scope['__mod__']}.{name}"] + ([scope[head] + (f".{rest}" if rest else "")] if head in scope else [])
            target = next((t for s in stems for t in (s, f"{s}.__init__") if t in known), None)
        if target and target != caller:
            calls.add((caller, target))
        else:
            unresolved += 1
    stats = {"files": len({f["file"] for f in funcs}), "functions": len(funcs),
             "calls_resolved": len(calls), "call_sites_unresolved": unresolved}
    return funcs, [{"src": a, "dst": b} for a, b in sorted(calls)], stats


def load(graph: str, funcs: list[dict], calls: list[dict], batch: int = 500):
    g = FalkorDB(host="localhost", port=6379).select_graph(graph)
    try:
        g.delete()
    except ResponseError:
        pass
    g.query("CREATE INDEX FOR (f:Function) ON (f.qualname)")
    g.query("CREATE INDEX FOR (f:File) ON (f.path)")
    g.query("CREATE INDEX FOR (a:Agent) ON (a.id)")
    for i in range(0, len(funcs), batch):
        g.query("""
            UNWIND $rows AS row
            MERGE (file:File {path: row.file})
            MERGE (fn:Function {qualname: row.qualname})
              ON CREATE SET fn.name = row.name, fn.file = row.file, fn.line = row.line, fn.version = 0
            MERGE (file)-[:CONTAINS]->(fn)
            """, {"rows": funcs[i:i + batch]})
    for i in range(0, len(calls), batch):
        g.query("""
            UNWIND $rows AS row
            MATCH (a:Function {qualname: row.src}), (b:Function {qualname: row.dst})
            MERGE (a)-[:CALLS]->(b)
            """, {"rows": calls[i:i + batch]})
    return g


def add_plan(g, agent: str, step: str, touches: list[str]) -> int:
    res = g.query("""
        MERGE (a:Agent {id: $agent})
        MERGE (s:Step {id: $step}) SET s.status = 'planned'
        MERGE (a)-[:PLANS]->(s)
        WITH s
        UNWIND $touches AS q
        MATCH (fn:Function {qualname: q})
        MERGE (s)-[:TOUCHES]->(fn)
        RETURN count(fn)
        """, {"agent": agent, "step": step, "touches": touches}).result_set
    return res[0][0] if res else 0


# Q3 from the design doc: plans that meet within `max_hops` calls of each other.
CONFLICTS = """
MATCH (me:Agent {id: $me})-[:PLANS]->(:Step {status: 'planned'})-[t1:TOUCHES]->(f:Function)
MATCH p = (f)-[:CALLS*0..3]-(g:Function)<-[t2:TOUCHES]-(s:Step {status: 'planned'})<-[:PLANS]-(other:Agent)
WHERE other <> me
RETURN other.id, f.qualname AS mine, g.qualname AS theirs, length(p) - 2 AS call_hops,
       [n IN nodes(p) | coalesce(n.qualname, n.id)] AS via
ORDER BY call_hops LIMIT 20
"""


if __name__ == "__main__":
    repo, src, graph = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
    t0 = time.perf_counter()
    funcs, calls, stats = parse_repo(repo / src)
    t1 = time.perf_counter()
    g = load(graph, funcs, calls)
    t2 = time.perf_counter()
    print(f"parse {t1 - t0:.2f}s, load {t2 - t1:.2f}s, stats {stats}")
