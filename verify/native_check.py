"""Sprint step 7 (exploratory): HiddenBench dev tasks through the product's six native tools (`--profile nodes`),
compared with the Stage 2 harness-board run (runs/repro_dev1, 13/20).

  python verify/native_check.py [runs/native_dev1] > results/v1/native_check.txt
1. settings vs Stage 2      2. accuracy vs 13/20 and the 9-16 bar     3. per-task tool use
4. source-check verdicts (product vs the rule: checked iff the agent holds that fact number)
5. leakage: no reply shows a briefing fact that nobody had posted (or tried to post) at that point
"""
import json
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAIN = ROOT.parents[1]
LIVE = ROOT / (sys.argv[1] if len(sys.argv) > 1 else "runs/native_dev1") / "graph"
STAGE2 = MAIN / "runs" / "repro_dev1" / "graph"
TRACES = ROOT / ".nodes" / "traces"
READS = ("status", "context_for")


def load(d):
    return {f.stem: json.loads(f.read_text(encoding="utf-8")) for f in d.glob("t*_d0.json")}


def right(r):
    return 2 * sum(v["vote"] == r["correct_answer"] for v in r["final_votes"]) > len(r["final_votes"])


def js(x):
    return json.dumps(x, ensure_ascii=False)


def main():
    c2, cl = (json.loads((d / "config.json").read_text()) for d in (STAGE2, LIVE))
    print("1. settings (Stage 2 repro_dev1 vs native run):")
    for k in ("model", "rounds", "seed", "profile", "split", "duplications", "condition", "effort", "workers", "server"):
        print(f"   {k:13} {c2.get(k)!s:18} {cl.get(k)!s:18} {'same' if c2.get(k) == cl.get(k) else 'DIFFERENT'}")
    s2, live = load(STAGE2), load(LIVE)
    tasks = sorted(live, key=lambda t: int(re.search(r"\d+", t).group()))
    r2, rl = sum(right(s2[t]) for t in tasks if t in s2), sum(right(live[t]) for t in tasks)
    print(f"\n2. accuracy: {len(tasks)} tasks finished (missing vs Stage 2: {sorted(set(s2) - set(live)) or 'none'})")
    print(f"   Stage 2 harness-board: {r2}/{len(tasks)}   native: {rl}/{len(tasks)}   "
          f"9-16 bar: {'inside' if 9 <= rl <= 16 else 'OUTSIDE'} (exploratory, not pre-registered)")

    print("\n3. per task: right (stage2/native), post_fact calls (checked/unverified), reads (status/context_for), "
          "other tools, posts on board, replies truncated by the budget")
    tot, verdict, agree, leaks, shown, trunc = Counter(), Counter(), 0, 0, 0, 0
    for t in tasks:
        r = live[t]
        held = {a["agent"]: a["visible_facts"] for a in r["fact_assignments"]}
        briefing = {f for fs in held.values() for f in fs}
        recs = [json.loads(x) for x in (TRACES / f"native-{LIVE.parent.name}-{t.replace('_', '-')}.trace.jsonl")
                .read_text(encoding="utf-8").splitlines()]
        n, posted = Counter(), set()
        for rec in recs:
            if not rec["kind"].startswith("tool"):
                continue
            name, resp, req = rec["name"], rec["response"], rec["request"]
            n[name] += 1
            if name == "post_fact":
                st = resp.get("data", {}).get("status", "error") if isinstance(resp, dict) else "error"
                verdict[st] += 1
                n["pf_" + st] += 1
                src = req.get("source_ref") or {}
                num = re.sub(r"\D", "", str(src.get("ref", "")))
                facts = held[rec["agent"]]
                holds = src.get("kind") == "briefing" and num != "" and 1 <= int(num) <= len(facts)
                agree += (st == "source-checked") == holds
                if st == "source-checked":
                    posted.add(facts[int(num) - 1])
                posted.add(req.get("text", ""))  # the agent chose to share this text itself
            body = js(resp)
            if name in READS or name == "post_fact":
                shown += 1
                leaks += sum(js(f)[1:-1] in body for f in briefing - posted)
            trunc += '"truncated"' in body
        tot.update(n)
        reads = n["status"] + n["context_for"]
        other = sum(v for k, v in n.items() if k in ("declare_intent", "before_acting", "coordinate"))
        print(f"   {t:7} {'Y' if right(s2[t]) else '-'}/{'Y' if right(r) else '-'}  post_fact {n['post_fact']:3} "
              f"({n['pf_source-checked']}/{n['pf_unverified']})  reads {reads:3} ({n['status']}/{n['context_for']})  "
              f"other {other}  posts {len(r['board']['posts'])}")
    k = len(tasks)
    s2_share = sum(sum(c["tool"] == "share_fact" for c in s2[t]["board"]["tool_calls"]) for t in tasks if t in s2)
    s2_reads = sum(sum(c["tool"] != "share_fact" for c in s2[t]["board"]["tool_calls"]) for t in tasks if t in s2)
    print(f"   means per task: post_fact {tot['post_fact'] / k:.1f} (Stage 2 share_fact {s2_share / k:.1f}); "
          f"reads {(tot['status'] + tot['context_for']) / k:.1f} (Stage 2 shared_facts+who_has_not_shared "
          f"{s2_reads / k:.1f}); declare_intent {tot['declare_intent']}, before_acting {tot['before_acting']}, "
          f"coordinate {tot['coordinate']} in total")
    print(f"   replies carrying a 'truncated' marker (budget cap): {trunc}")
    print(f"\n4. source-check verdicts on post_fact: {dict(verdict)}; agree with the rule on {agree}/{sum(verdict.values())}")
    print(f"\n5. leakage: {shown} status/context_for/post_fact replies checked; unposted briefing facts shown: {leaks}")


if __name__ == "__main__":
    main()
