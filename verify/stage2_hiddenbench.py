"""Reproduction Stage 2, HiddenBench part: analyze the live product run once, against the original dev run.
Pre-registration: results/repro/preregistration_stage2.md.

  python verify/stage2_hiddenbench.py > results/repro/stage2_hiddenbench.txt
1. settings match (model, rounds, seed, profile, split)          2. accuracy vs the 9-16 bar
3. source-check verdicts: posts, source-checked vs unverified     4. leakage on the live traces
5. tool use per task vs the original (0.5x-1.5x of the mean)
"""
import json
import statistics
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAIN = ROOT.parents[1]
ORIG, LIVE = MAIN / "runs" / "dev1" / "graph", MAIN / "runs" / "repro_dev1" / "graph"
TRACES = ROOT / ".nodes" / "traces"
TOOLS = ("share_fact", "shared_facts", "who_has_not_shared")


def load(d):
    return {f.stem: json.loads(f.read_text(encoding="utf-8")) for f in d.glob("t*_d0.json")}


def right(r):
    return 2 * sum(v["vote"] == r["correct_answer"] for v in r["final_votes"]) > len(r["final_votes"])


def verdicts(r):
    """share_fact verdicts by the harness rule: yes iff the agent holds that fact number."""
    held = {a["agent"]: len(a["visible_facts"]) for a in r["fact_assignments"]}
    calls = [c for c in r["board"]["tool_calls"] if c["tool"] == "share_fact"]
    yes = sum(1 <= c["fact_number"] <= held[c["agent"]] for c in calls)
    return len(calls), yes, len(calls) - yes


def main():
    co = json.loads((ORIG.parent / "graph" / "config.json").read_text()) if (ORIG / "config.json").exists() else {}
    cl = json.loads((LIVE / "config.json").read_text())
    print("1. settings (original dev1 vs live product run):")
    for k in ("model", "rounds", "seed", "profile", "split", "duplications", "condition"):
        print(f"   {k:13} {co.get(k)!s:18} {cl.get(k)!s:18} {'same' if co.get(k) == cl.get(k) else 'DIFFERENT'}")
    print(f"   effort        {co.get('effort')!s:18} {cl.get('effort')!s:18} (None = CLI default)")
    print(f"   server        {co.get('server', 'stdio (per call)')!s:18} {cl.get('server')!s:18} (pre-registered caveat)")
    o, l = load(ORIG), load(LIVE)
    tasks = sorted(set(o) & set(l))
    print(f"\n2. accuracy: tasks complete in both: {len(tasks)} (live missing: {sorted(set(o) - set(l)) or 'none'})")
    ro, rl = sum(right(o[t]) for t in tasks), sum(right(l[t]) for t in tasks)
    print(f"   original dev1 graph: {ro}/{len(tasks)}   live product: {rl}/{len(tasks)}   "
          f"pre-registered bar 9-16 of 20: {'PASS' if 9 <= rl <= 16 else 'FAIL'}")
    print("\n3. source-check verdicts on share_fact (harness rule: source-checked iff the agent holds that fact):")
    for name, runs in (("original", o), ("live", l)):
        n, yes, no = (sum(x) for x in zip(*(verdicts(runs[t]) for t in tasks)))
        posts = sum(len(runs[t]["board"]["posts"]) for t in tasks)
        print(f"   {name:9} share_fact calls {n}, source-checked {yes}, unverified (nothing posted) {no}, board posts {posts}")
    # the live verdicts as the product actually returned them, from the traces
    got, agree, leaks, shown = Counter(), 0, 0, 0
    for t in tasks:
        ws = f"repro-repro_dev1-{t.replace('_', '-')}"  # run file t12_d0 -> workspace t12-d0
        recs = [json.loads(x) for x in (TRACES / f"{ws}.trace.jsonl").read_text(encoding="utf-8").splitlines()]
        r = l[t]
        held = {a["agent"]: a["visible_facts"] for a in r["fact_assignments"]}
        briefing = {f for fs in held.values() for f in fs}
        posted = set()
        for rec in recs:
            if not rec["kind"].startswith("tool"):
                continue
            if rec["name"] == "share_fact":
                verdict = "yes" if rec["response"].startswith("Source-checked: yes") else "no"
                got[verdict] += 1
                n, facts = rec["request"]["fact_number"], held[rec["agent"]]
                agree += (verdict == "yes") == (1 <= n <= len(facts))
                if verdict == "yes":
                    posted.add(facts[n - 1])
            elif rec["name"] in ("shared_facts", "who_has_not_shared"):
                shown += 1
                leaks += sum(f in rec["response"] for f in briefing - posted)
    print(f"   live, as returned by the product (traces): yes {got['yes']}, no {got['no']}; "
          f"agrees with the harness rule on {agree}/{sum(got.values())}")
    print(f"\n4. leakage on live traces: {shown} board/who replies checked; unposted briefing facts shown: {leaks}")
    print("\n5. tool use per run (mean; bar: live within 0.5x-1.5x of the original mean):")
    co_, cl_ = ([Counter(c["tool"] for c in runs[t]["board"]["tool_calls"]) for t in tasks] for runs in (o, l))
    for tool in TOOLS + ("all",):
        mo = statistics.mean((sum(c.values()) if tool == "all" else c[tool]) for c in co_)
        ml = statistics.mean((sum(c.values()) if tool == "all" else c[tool]) for c in cl_)
        print(f"   {tool:19} original {mo:5.1f}  live {ml:5.1f}  ratio {ml / mo:.2f}  {'PASS' if 0.5 <= ml / mo <= 1.5 else 'FAIL'}")
    print("   per task (original/live): " + ", ".join(
        f"{t.split('_')[0]} {sum(a.values())}/{sum(b.values())}" for t, a, b in zip(tasks, co_, cl_)))


if __name__ == "__main__":
    main()
