"""Load click (CooperBench dev task 2068's base commit), add two fake plans, run the conflict check.

  python coop/demo_conflicts.py <path to click checkout>

No LLM is involved: the plans are hand-written, shaped like the task's two features.
"""
import sys
import time
from pathlib import Path

from world_graph import CONFLICTS, add_plan, load, parse_repo

repo = Path(sys.argv[1])
t0 = time.perf_counter()
funcs, calls, stats = parse_repo(repo / "src" / "click")
t1 = time.perf_counter()
g = load("world_click_2068", funcs, calls)
t2 = time.perf_counter()
print(f"parse {t1 - t0:.3f}s  load {t2 - t1:.3f}s  {stats}")

# agent1: "edit several files at once"   agent2: "only reset readonly if the saved file is the opened one"
n1 = add_plan(g, "agent1", "step-a1", ["click.termui.edit"])
n2 = add_plan(g, "agent2", "step-a2", ["click._termui_impl.Editor.edit_file"])
# agent3 works somewhere unrelated, to show an empty answer is a real answer
n3 = add_plan(g, "agent3", "step-a3", ["click.formatting.wrap_text"])
assert (n1, n2, n3) == (1, 1, 1), (n1, n2, n3)

for me in ("agent1", "agent3"):
    times = []
    for _ in range(20):
        s = time.perf_counter()
        rows = g.ro_query(CONFLICTS, {"me": me}).result_set
        times.append(time.perf_counter() - s)
    times.sort()
    print(f"\nconflicts for {me}: {len(rows)} rows, median {times[10] * 1000:.1f} ms, max {times[-1] * 1000:.1f} ms")
    for other, mine, theirs, hops, via in rows[:5]:
        print(f"  vs {other}: {mine} -> {theirs} in {hops} call hop(s) via {' -> '.join(via)}")

profile = g.profile(CONFLICTS, {"me": "agent1"})
print("\nplan:\n" + "\n".join(f"  {line}" for line in profile))
