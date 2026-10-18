"""Leakage audit for the HiddenBench test1 graph conditions. Read-only. No model calls.

1. Board invariants, on every stored test board graph (board_test1_*):
   a. every SHARED edge goes from an agent that KNOWS that fact (nobody can post a fact they don't hold)
   b. the facts a shared_facts() reply can show are exactly the SHARED targets (re-derived with the tool's own code)
   c. who_has_not_shared() replies contain no fact text at all
2. Transcripts (all 5 conditions): a private fact counts as "leaked" if an agent who doesn't hold it reproduces an
   8-word run of it, while its holder never posted it (graph) and never used it in chat. The expected count is 0.

  python verify/leakage_audit.py <path to runs/test1>
"""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]  # main checkout
sys.path.insert(0, str(ROOT / "pilot"))
import graph_tools  # noqa: E402  (main's frozen board code, the one the batch used)
from falkordb import FalkorDB  # noqa: E402


def words(s):
    return re.findall(r"[a-z0-9']+", s.lower())


def ngrams(s, n=8):
    w = words(s)
    return {tuple(w[i:i + n]) for i in range(len(w) - n + 1)}


def board_invariants():
    db = FalkorDB()
    graphs = [g for g in db.list_graphs() if g.startswith("board_test1_")]
    bad_a = bad_b = bad_c = 0
    checked = 0
    for name in graphs:
        g = db.select_graph(name)
        bad_a += g.ro_query("MATCH (a:Agent)-[:SHARED]->(f:Fact) WHERE NOT (a)-[:KNOWS]->(f) "
                            "RETURN count(*)").result_set[0][0]
        posted = {r[0] for r in g.ro_query("MATCH ()-[:SHARED]->(f:Fact) RETURN f.text").result_set}
        reply = graph_tools.shared_facts(g)
        all_facts = {r[0] for r in g.ro_query("MATCH (f:Fact) RETURN f.text").result_set}
        bad_b += sum(1 for t in all_facts - posted if t in reply)
        who = graph_tools.who_has_not_shared(g)
        bad_c += sum(1 for t in all_facts if t in who)
        checked += 1
    return checked, bad_a, bad_b, bad_c


def transcript_leaks(root: Path):
    tasks = {t["id"]: t for t in json.loads(
        (ROOT / "external" / "HiddenBench_ICML" / "data" / "benchmark.json").read_text(encoding="utf-8"))}
    leaks, checked = [], 0
    for cond_dir in sorted(p for p in root.iterdir() if p.is_dir() and p.name != "work"):
        for f in cond_dir.glob("t*_d0.json"):
            run = json.loads(f.read_text(encoding="utf-8"))
            hidden = tasks[run["task_id"]]["hidden_information"]
            # graph_tools.seed gives hidden fact i the board id "h<i>"
            posted_texts = {hidden[int(fid[1:])] for _, fid in run.get("board", {}).get("posts", []) if fid.startswith("h")}
            holder_of = {}
            for a in run["fact_assignments"]:
                for h in a["assigned_hidden_information"]:
                    holder_of[h] = a["agent"]
            said = {}
            for m in run["discussion_transcript"]:
                said.setdefault(m["agent"], []).append(m["response"])
            for fact, holder in holder_of.items():
                grams = ngrams(fact)
                if not grams:
                    continue
                holder_used = any(grams & ngrams(t) for t in said.get(holder, []))
                fact_posted = fact in posted_texts
                for agent, texts in said.items():
                    if agent == holder:
                        continue
                    checked += 1
                    if any(grams & ngrams(t) for t in texts) and not holder_used and not fact_posted:
                        leaks.append((cond_dir.name, f.stem, agent, holder, fact[:80]))
    return checked, leaks


if __name__ == "__main__":
    n, a, b, c = board_invariants()
    print(f"board graphs checked: {n}")
    print(f"  posts by an agent that doesn't hold the fact: {a}")
    print(f"  unposted facts visible in a shared_facts() reply: {b}")
    print(f"  fact text in who_has_not_shared() replies: {c}")
    m, leaks = transcript_leaks(Path(sys.argv[1]))
    print(f"(non-holder, private fact) combinations checked in transcripts: {m}")
    print(f"  non-holder reproduced a private fact its holder never posted or said: {len(leaks)}")
    for x in leaks[:20]:
        print("   ", x)
