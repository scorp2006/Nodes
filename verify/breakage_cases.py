"""Changed lines and graph paths for the 4 light-repo disjoint breakages (illustrations for
results/detector_breakage_cases.md). Uses the frozen v2 graph, loaded into falkordb-scale (port 6390). No model calls.

  python verify/breakage_cases.py > results/verify/breakage_cases_raw.txt
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "coop"))
import world_graph_v2 as v2  # noqa: E402
from detector_eval import CACHE, changed_lines  # noqa: E402
from falkordb import FalkorDB  # noqa: E402
from prepare_repo import prepare  # noqa: E402

DB = FalkorDB(port=6390)
v2.FalkorDB = lambda **kw: DB  # v2.load hard-codes 6379; keep the main database untouched
ROWS = Path(__file__).resolve().parents[1] / "results" / "detector2_rows.json"
PATH = ("MATCH (x:Function {qualname: $a}), (y:Function {qualname: $b}) CALL algo.SPpaths({sourceNode: x, "
        "targetNode: y, relTypes: $rels, relDirection: 'both', maxLen: 6}) YIELD path "
        "RETURN [n IN nodes(path) | n.qualname], [r IN relationships(path) | type(r)] LIMIT 1")

cases = [r for r in json.loads(ROWS.read_text(encoding="utf-8")) if r["disjoint"] and r["interaction_failure"]]
graphs = {}
for r in cases:
    repo, task, (i, j) = r["repo"], r["task"], r["pair"]
    if (repo, task) not in graphs:
        rd, pkg = prepare(repo, task)
        funcs, calls, state, _ = v2.parse_repo(rd / pkg)
        graphs[(repo, task)] = v2.load(f"case_{task}", funcs, calls, state)
    g = graphs[(repo, task)]
    d = CACHE / repo / f"task{task}" / f"feature{i}_feature{j}"
    lines = []
    for k in (i, j):
        patch = (d / f"patch_claude_k1_feature{k}.patch").read_bytes().decode("utf-8").replace("\r\n", "\n")
        lines.append({f: sorted(v) for f, v in changed_lines(patch).items() if not f.startswith("tests")})
    shared = {f: sorted(set(lines[0][f]) & set(lines[1].get(f, []))) for f in lines[0] if f in lines[1]}
    print(f"===== {repo} {task} features {i}+{j}: merge {r['merge_status']}")
    print(f"  edited functions: {r['funcs'][0]} | {r['funcs'][1]}")
    print(f"  changed lines touched by both patches: {shared}")
    for rels in (["CALLS"], ["CALLS", "SHARES_STATE"]):
        best = None
        for a in r["funcs"][0]:
            for b in r["funcs"][1]:
                for nodes, types in g.ro_query(PATH, {"a": a, "b": b, "rels": rels}).result_set:
                    if best is None or len(types) < len(best[1]):
                        best = (nodes, types)
        print(f"  shortest {'+'.join(rels)} path: {best or 'none'}")
for g in graphs.values():
    g.delete()
