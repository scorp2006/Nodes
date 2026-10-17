"""Print a compact case file for one HiddenBench test task: graph vs a control, side by side. Read-only.

  python verify/case_file.py <runs/test1> t7_d0 placebo
"""
import json
import sys
from pathlib import Path


def short(s, n=230):
    s = " ".join(s.split())
    return s if len(s) <= n else s[:n] + "..."


def show(run, label):
    ans = run["correct_answer"]
    print(f"--- {label}: correct = {ans}")
    print("  initial votes:", [v["vote"] for v in run["initial_votes"]])
    print("  final votes:  ", [v["vote"] for v in run["final_votes"]])
    if "board" in run:
        print("  board posts (agent, fact):", run["board"]["posts"])
        tools = {}
        for c in run["board"]["tool_calls"]:
            tools[c["tool"]] = tools.get(c["tool"], 0) + 1
        print("  tool calls:", tools)
    # first and last round messages, plus any message that mentions the board or changes the speaker's position
    rounds = {}
    for m in run["discussion_transcript"]:
        rounds.setdefault(m["round"], []).append(m)
    for r in (1, 2, max(rounds)):
        for m in rounds.get(r, []):
            print(f"  R{r} {m['agent']}: {short(m['response'])}")
    for v in run["final_votes"]:
        print(f"  final {v['agent']} -> {v['vote']}: {short(v['rationale'], 200)}")


if __name__ == "__main__":
    root, task, control = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
    g = json.loads((root / "graph" / f"{task}.json").read_text(encoding="utf-8"))
    c = json.loads((root / control / f"{task}.json").read_text(encoding="utf-8"))
    print(f"===== {task}: {g['scenario']}")
    print("  hidden facts by agent:")
    for a in g["fact_assignments"]:
        print(f"   {a['agent']}: {short(a['assigned_hidden_information'][0], 200)}")
    show(g, "graph")
    show(c, control)
