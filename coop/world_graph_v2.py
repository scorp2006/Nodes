"""World graph v2 for the offline detector test (results/preregistration_detector2.md). The live batch keeps using
world_graph.py; this file must not be imported by it.

Adds over v1:
  - call resolution through inheritance (self.m / cls.m / super().m), parameters annotated with a repo class,
    and self.attr holding a repo-class instance (assigned in any method of the class)
  - shared-state edges: (:Function)-[:SHARES_STATE]-(:Function) when two functions read or write the same class
    attribute (scoped to the class family) or the same module global
"""
from __future__ import annotations

import ast
import builtins
from collections import defaultdict
from itertools import combinations
from pathlib import Path

from falkordb import FalkorDB
from redis.exceptions import ResponseError

BUILTINS = set(dir(builtins))


def module_name(path: Path, root: Path) -> str:
    parts = list(path.relative_to(root.parent).with_suffix("").parts)
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


def _imports(tree, mod):
    out = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module is not None:
            base = node.module if node.level == 0 else ".".join(mod.split(".")[: -node.level] + [node.module])
            for a in node.names:
                out[a.asname or a.name] = f"{base}.{a.name}"
        elif isinstance(node, ast.ImportFrom) and node.level:
            base = ".".join(mod.split(".")[: -node.level])
            for a in node.names:
                out[a.asname or a.name] = f"{base}.{a.name}"
        elif isinstance(node, ast.Import):
            for a in node.names:
                out[a.asname or a.name] = a.name
    return out


def _ann_name(ann):
    if isinstance(ann, ast.Constant) and isinstance(ann.value, str):
        return ann.value.split("[")[0].strip().strip('"')
    if isinstance(ann, ast.Name):
        return ann.id
    if isinstance(ann, ast.Attribute):
        return ann.attr
    if isinstance(ann, ast.Subscript):  # Optional[X], t.Optional[X]
        return _ann_name(ann.slice)
    if isinstance(ann, ast.BinOp):  # X | None
        return _ann_name(ann.left)
    return None


def parse_repo(root: Path, shared_state: bool = True):
    """Return (functions, call_edges, state_edges, stats)."""
    modules = []
    for path in sorted(root.rglob("*.py")):
        mod = module_name(path, root)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        modules.append((path, mod, tree, _imports(tree, mod)))

    funcs, classes = [], {}  # classes: qualname -> {"bases": [raw], "methods": set, "mod": mod, "imports": ...}
    func_info = []  # (qualname, node, cls_qualname, mod, imports, module_globals)
    for path, mod, tree, imports in modules:
        globals_ = {t.id for n in tree.body if isinstance(n, (ast.Assign, ast.AnnAssign, ast.AugAssign))
                    for t in (n.targets if isinstance(n, ast.Assign) else [n.target]) if isinstance(t, ast.Name)}

        def visit(body, prefix, cls):
            for n in body:
                if isinstance(n, ast.ClassDef):
                    q = f"{prefix}.{n.name}"
                    classes[q] = {"bases": [b for b in n.bases], "methods": set(), "mod": mod, "imports": imports}
                    visit(n.body, q, q)
                elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    q = f"{prefix}.{n.name}"
                    funcs.append({"qualname": q, "name": n.name, "file": str(path.relative_to(root.parent)),
                                  "line": n.lineno})
                    if cls:
                        classes[cls]["methods"].add(n.name)
                    func_info.append((q, n, cls, mod, imports, globals_))

        visit(tree.body, mod, None)

    known = {f["qualname"] for f in funcs}

    def resolve_class(name, mod, imports):
        if not name:
            return None
        for cand in (f"{mod}.{name}", imports.get(name), imports.get(name.split(".")[0], "") + name[len(name.split(".")[0]):]
                     if "." in name else None):
            if cand and cand in classes:
                return cand
        hits = [c for c in classes if c.endswith("." + name)]
        return hits[0] if len(hits) == 1 else None

    bases = {}
    for q, c in classes.items():
        bs = []
        for b in c["bases"]:
            nm = b.id if isinstance(b, ast.Name) else (ast.unparse(b) if isinstance(b, ast.Attribute) else None)
            r = resolve_class(nm, c["mod"], c["imports"])
            if r:
                bs.append(r)
        bases[q] = bs

    def mro(cls):
        seen, order, stack = set(), [], [cls]
        while stack:
            c = stack.pop(0)
            if c in seen or c not in classes:
                continue
            seen.add(c)
            order.append(c)
            stack.extend(bases.get(c, []))
        return order

    def find_method(cls, name, skip_self=False):
        for c in mro(cls)[1 if skip_self else 0:]:
            if name in classes[c]["methods"]:
                return f"{c}.{name}"
        return None

    def family_root(cls):
        return mro(cls)[-1] if cls else None

    # attribute types: self.attr = RepoClass(...) anywhere in the class
    attr_types = defaultdict(dict)
    for q, node, cls, mod, imports, _ in func_info:
        if not cls:
            continue
        for sub in ast.walk(node):
            if (isinstance(sub, ast.Assign) and len(sub.targets) == 1 and isinstance(sub.targets[0], ast.Attribute)
                    and isinstance(sub.targets[0].value, ast.Name) and sub.targets[0].value.id == "self"
                    and isinstance(sub.value, ast.Call) and isinstance(sub.value.func, ast.Name)):
                r = resolve_class(sub.value.func.id, mod, imports)
                if r:
                    attr_types[cls][sub.targets[0].attr] = r

    calls, resolved, unresolved = set(), 0, 0
    access = defaultdict(set)  # state key -> functions
    for q, node, cls, mod, imports, globals_ in func_info:
        local_types = {}
        for a in node.args.args + node.args.kwonlyargs:
            r = resolve_class(_ann_name(a.annotation), mod, imports) if a.annotation is not None else None
            if r:
                local_types[a.arg] = r
        for sub in ast.walk(node):
            if (isinstance(sub, ast.Assign) and len(sub.targets) == 1 and isinstance(sub.targets[0], ast.Name)
                    and isinstance(sub.value, ast.Call) and isinstance(sub.value.func, ast.Name)):
                r = resolve_class(sub.value.func.id, mod, imports)
                if r:
                    local_types[sub.targets[0].id] = r
        for sub in ast.walk(node):
            if shared_state and cls and isinstance(sub, ast.Attribute) and isinstance(sub.value, ast.Name) \
                    and sub.value.id == "self" and sub.attr not in classes[cls]["methods"]:
                access[("attr", family_root(cls), sub.attr)].add(q)
            if shared_state and isinstance(sub, ast.Name) and sub.id in globals_:
                access[("global", mod, sub.id)].add(q)
            if not isinstance(sub, ast.Call):
                continue
            f, target = sub.func, None
            if isinstance(f, ast.Name):
                if f.id in BUILTINS and f"{mod}.{f.id}" not in known:
                    continue  # builtins are never in-repo; excluded from both counts
                for cand in (f"{mod}.{f.id}", imports.get(f.id)):
                    if cand in known:
                        target = cand
                        break
                    if cand in classes:
                        target = find_method(cand, "__init__")
                        break
            elif isinstance(f, ast.Attribute):
                v = f.value
                if isinstance(v, ast.Name) and v.id in ("self", "cls") and cls:
                    target = find_method(cls, f.attr)
                elif isinstance(v, ast.Call) and isinstance(v.func, ast.Name) and v.func.id == "super" and cls:
                    target = find_method(cls, f.attr, skip_self=True)
                elif isinstance(v, ast.Name) and v.id in local_types:
                    target = find_method(local_types[v.id], f.attr)
                elif (isinstance(v, ast.Attribute) and isinstance(v.value, ast.Name) and v.value.id == "self" and cls
                      and v.attr in attr_types.get(cls, {})):
                    target = find_method(attr_types[cls][v.attr], f.attr)
                elif isinstance(v, ast.Name) and v.id in imports:
                    cand = f"{imports[v.id]}.{f.attr}"
                    target = cand if cand in known else (find_method(cand, "__init__") if cand in classes else None)
            if target and target != q:
                calls.add((q, target))
                resolved += 1
            else:
                unresolved += 1

    state_edges = set()
    for key, fs in access.items():
        for a, b in combinations(sorted(fs), 2):
            state_edges.add((a, b))
    stats = {"functions": len(funcs), "classes": len(classes), "call_edges": len(calls),
             "call_sites_resolved": resolved, "call_sites_unresolved": unresolved,
             "resolution_rate_excl_builtins": round(resolved / max(resolved + unresolved, 1), 3),
             "state_keys": len(access), "shares_state_edges": len(state_edges)}
    return funcs, [{"src": a, "dst": b} for a, b in sorted(calls)], \
        [{"a": a, "b": b} for a, b in sorted(state_edges)], stats


def load(graph: str, funcs, calls, state_edges, batch: int = 1000):
    g = FalkorDB(host="localhost", port=6379).select_graph(graph)
    try:
        g.delete()
    except ResponseError:
        pass
    g.query("CREATE INDEX FOR (f:Function) ON (f.qualname)")
    for i in range(0, len(funcs), batch):
        g.query("UNWIND $rows AS r MERGE (f:Function {qualname: r.qualname}) SET f.file = r.file, f.line = r.line",
                {"rows": funcs[i:i + batch]})
    for i in range(0, len(calls), batch):
        g.query("UNWIND $rows AS r MATCH (a:Function {qualname: r.src}), (b:Function {qualname: r.dst}) "
                "MERGE (a)-[:CALLS]->(b)", {"rows": calls[i:i + batch]})
    for i in range(0, len(state_edges), batch):
        g.query("UNWIND $rows AS r MATCH (a:Function {qualname: r.a}), (b:Function {qualname: r.b}) "
                "MERGE (a)-[:SHARES_STATE]->(b)", {"rows": state_edges[i:i + batch]})
    return g


def distance(g, set1, set2, rel: str, max_hops: int = 6) -> int | None:
    """Shortest undirected path length between any function in set1 and any in set2 over `rel` edges."""
    best = None
    for a in set1:
        for b in set2:
            if a == b:
                return 0
            # FalkorDB has no undirected shortestPath; algo.SPpaths does undirected (relDirection 'both')
            rows = g.ro_query(
                "MATCH (x:Function {qualname: $a}), (y:Function {qualname: $b}) "
                "CALL algo.SPpaths({sourceNode: x, targetNode: y, relTypes: $rels, relDirection: 'both', "
                "maxLen: $max}) YIELD path RETURN min(length(path))",
                {"a": a, "b": b, "rels": rel.split("|"), "max": max_hops}).result_set
            if rows and rows[0][0] is not None:
                best = rows[0][0] if best is None else min(best, rows[0][0])
    return best
