"""Stage 1 (REPRODUCTION-PLAN.md): differential replay, zero LLM calls.

Every recorded tool call from the harness runs is replayed, in recorded order, into BOTH the frozen harness code
(pilot/graph_tools.py, coop/coop_tools.py on FalkorDB :6379) and the product's harness profile (nodes/harness.py on
the product's own FalkorDB :6380, a fresh workspace per run). For each call we compare:
  - the full reply text (harness vs product), exact
  - recorded fields from the original run logs: HiddenBench final board posts; CooperBench resolved names,
    conflict flag, claim winners/losers, owned-function counts, role step
  - leakage: no product reply contains a briefing fact that nobody had posted at that point
Only the harness runs whose code matches the frozen harness are replayed: HiddenBench board runs (dev1, test1,
hb_sonnet) and CooperBench cb1b graph runs (45). Older dev CooperBench logs used earlier tool versions.

  python verify/replay_equivalence.py > results/repro/replay_equivalence.txt
"""
import csv
import glob
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
MAIN = ROOT.parents[1]  # the main checkout holds runs/ and external/
sys.path[:0] = [str(ROOT), str(ROOT / "coop"), str(MAIN / "pilot")]
import coop_tools as ct  # noqa: E402  frozen CooperBench harness
import graph_tools as gt  # noqa: E402  frozen HiddenBench harness (board mode)
from world_graph import load as wg_load, parse_repo  # noqa: E402

from nodes import harness, indexer, ops  # noqa: E402
from nodes.db import Store  # noqa: E402
from prepare_repo import PACKAGE, prepare  # noqa: E402

TASKS = {t["id"]: t for t in json.loads((MAIN / "external" / "HiddenBench_ICML" / "data" / "benchmark.json")
                                        .read_text(encoding="utf-8"))}
mismatches, counts = [], {"calls": 0, "text_equal": 0, "recorded_checks": 0, "recorded_equal": 0, "leaks": 0}


def check(kind, run, i, a, b, what):
    counts["recorded_checks" if kind == "recorded" else "calls"] += 1
    if a == b:
        counts["recorded_equal" if kind == "recorded" else "text_equal"] += 1
    else:
        mismatches.append({"kind": kind, "run": run, "call": i, "what": what, "harness": a, "product": b})


def hiddenbench(path: Path):
    r = json.loads(path.read_text(encoding="utf-8"))
    run = f"{path.parent.parent.name}/{path.stem}"
    task = SimpleNamespace(**TASKS[r["task_id"]])
    hg = f"replay_h_{path.parent.parent.name}_{path.stem}"
    gt.seed(hg, task, r["fact_assignments"])
    g = gt.connect(hg)
    store = Store(f"replay-{path.parent.parent.name}-{path.stem}")
    store.reset()
    store.setup()
    sess = harness.seed_board(store, r["fact_assignments"])
    briefing = {t for a in r["fact_assignments"] for t in a["visible_facts"]}
    posted = set()
    for i, c in enumerate(r["board"]["tool_calls"]):
        agent, tool = c["agent"], c["tool"]
        if tool == "share_fact":
            a, b = gt.share_fact(g, agent, c["fact_number"]), harness.share_fact(store, sess[agent], c["fact_number"])
            posted |= {row[0] for row in g.ro_query("MATCH ()-[:SHARED]->(f:Fact) RETURN f.text").result_set}
        elif tool == "shared_facts":
            a, b = gt.shared_facts(g), harness.shared_facts(store)
        elif tool == "who_has_not_shared":
            a, b = gt.who_has_not_shared(g), harness.who_has_not_shared(store)
        else:
            continue
        check("text", run, i, a, b, tool)
        counts["leaks"] += sum(t in b for t in briefing - posted)
    rec = [list(p) for p in r["board"]["posts"]]
    harness_posts = [[n, fid] for n, fid in g.ro_query("MATCH (a:Agent)-[s:SHARED]->(f:Fact) RETURN a.name, f.id "
                                                       "ORDER BY s.seq").result_set]
    ids = {text: fid for text, fid in g.ro_query("MATCH (f:Fact) RETURN f.text, f.id").result_set}
    product_posts = [[n, ids[t]] for n, t in store.q("x", "MATCH (s:Session)-[a:ASSERTED]->(f:Fact) RETURN s.name, f.text "
                                                     "ORDER BY a.seq", ro=True)]
    check("recorded", run, -1, rec, harness_posts, "final board posts (recorded vs harness replay)")
    check("recorded", run, -1, rec, product_posts, "final board posts (recorded vs product replay)")
    g.delete()
    store.reset()


def cooperbench(log: Path, plan: dict):
    name = log.name.removesuffix(".tool_calls.jsonl")
    row = plan[name]
    repo, task = row["Repo"], row["Task"]
    repo_dir, pkg = prepare(repo, task)
    hg = f"replay_cb_{name}"
    funcs, calls, _ = parse_repo(repo_dir / pkg)
    g = wg_load(hg, funcs, calls)
    store = Store(f"replay-{name}")
    store.reset()
    store.setup()
    indexer.index(store, repo_dir, pkg, repo)
    sess = {}
    for i, c in enumerate(json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()):
        agent, tool = c["agent"], c["tool"]
        if agent not in sess:
            _, sid = ops.join(store, agent, task="cooperbench", client="harness")
            sess[agent] = {"session": sid, "name": agent}
        s = sess[agent]
        g.query("MERGE (:Agent {id: $a})", {"a": agent})  # the harness's who() does this on every call
        if tool == "declare_plan":
            found, problems = ct.resolve_names(g, c["functions"])
            ct.set_plan(g, agent, found)
            text = ct.status_text(g, agent)
            if len(ct.planners(g)) < 2:
                text = "Your partner has not declared a plan yet; call coordination_status again before editing.\n" + text
            a = f"Plan recorded: {', '.join(found) or 'nothing'}." + ct._problems_note(problems) + "\n" + text
            b, pfound = harness.declare_plan_text(store, s, c["functions"])
            check("recorded", name, i, c["resolved"], pfound, "declare_plan resolved")
            check("recorded", name, i, c["conflict"], "Overlaps with" in b, "declare_plan conflict flag")
        elif tool == "claim":
            found, problems = ct.resolve_names(g, c["functions"])
            mine, others = ct.claim(g, agent, found)
            a = ("You own: " + (", ".join(mine) or "nothing new") + "."
                 + "".join(f"\n{q} is owned by {o}: do not edit it; merge their branch once they commit."
                           for q, o in others.items()) + ct._problems_note(problems))
            b, pmine, pothers = harness.claim_text(store, s, c["functions"])
            check("recorded", name, i, (c["won"], c["lost"]), (pmine, pothers), "claim winners")
        elif tool == "mark_committed":
            n = ct.mark_committed(g, agent, c["commit"])
            a = f"Recorded your commit {c['commit']} ({n} owned function(s)).\n" + ct.status_text(g, agent)
            b = harness.mark_committed_text(store, s, c["commit"])
            check("recorded", name, i, c["functions"], int(b.split("(")[1].split()[0]), "owned function count")
        elif tool == "coordination_status":
            a, b = ct.status_text(g, agent), harness.status_text(store, agent)
            check("recorded", name, i, c["step"], harness.next_step(store, agent)[0], "role step")
            check("recorded", name, i, c["conflict"], "Overlaps with" in b, "status conflict flag")
        elif tool == "impact_of":
            a, b = ct.impact_text(g, c["function"]), harness.impact_text(store, c["function"])
        else:
            continue
        check("text", name, i, a, b, tool)
    g.delete()
    store.reset()


def main():
    t0 = time.time()
    hb = sorted(Path(p) for b in ("dev1", "test1", "hb_sonnet") for p in glob.glob(str(MAIN / "runs" / b / "graph" / "t*_d0.json")))
    import faulthandler
    faulthandler.dump_traceback_later(180, repeat=True)  # a stall prints its stack to stderr
    for k, p in enumerate(hb, 1):
        hiddenbench(p)
        print(f"  hiddenbench {k}/{len(hb)}", file=sys.stderr, flush=True)
    hb_calls = counts["calls"]
    plan = {r["Name"]: r for r in csv.DictReader(open(ROOT / "coop" / "plans" / "test1b.csv", encoding="utf-8"))}
    logs = sorted(Path(p) for p in glob.glob(str(ROOT / "runs" / "cooperbench" / "t1b-*-graph.tool_calls.jsonl")))
    for k, log in enumerate(logs, 1):
        cooperbench(log, plan)
        print(f"  cooperbench {k}/{len(logs)}", file=sys.stderr, flush=True)
    print(f"HiddenBench board runs replayed: {len(hb)} ({hb_calls} tool calls)")
    print(f"CooperBench cb1b graph runs replayed: {len(logs)} ({counts['calls'] - hb_calls} tool calls)")
    print(f"reply text identical (harness vs product): {counts['text_equal']}/{counts['calls']}")
    print(f"recorded fields matched: {counts['recorded_equal']}/{counts['recorded_checks']}")
    print(f"leaks (an unposted briefing fact in a product reply): {counts['leaks']}")
    print(f"mismatches: {len(mismatches)}   ({time.time() - t0:.0f} s)")
    for m in mismatches[:40]:
        print(json.dumps(m)[:1200])


if __name__ == "__main__":
    main()
