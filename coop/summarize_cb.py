"""Summarize CooperBench runs made by run_pair.ps1.

  python coop/summarize_cb.py runs/cooperbench dev1-click-coop dev1-click-placebo dev1-click-graph
"""
import collections
import json
import sys
from pathlib import Path


def one(log_dir: Path, name: str) -> dict:
    res = next((log_dir / name).rglob("result.json"), None)
    ev = next((log_dir / name).rglob("eval.json"), None)
    if not res:
        return {"name": name, "missing": True}
    r = json.loads(res.read_text(encoding="utf-8"))
    e = json.loads(ev.read_text(encoding="utf-8")) if ev else {}
    agents = r["agents"].values()
    tools_file = log_dir / f"{name}.tool_calls.jsonl"
    calls = [json.loads(x) for x in tools_file.read_text(encoding="utf-8").splitlines()] if tools_file.exists() else []
    wall = (log_dir / f"{name}.wall.txt")
    peak = collections.defaultdict(float)
    stats = log_dir / f"{name}.docker_stats.txt"
    if stats.exists():
        for line in stats.read_text(encoding="utf-8-sig").splitlines():
            if "\t" in line:
                cname, mem = line.split("\t", 1)
                v = mem.split("/")[0].strip()
                mib = float(v[:-3]) * (1024 if v.endswith("GiB") else 1) if v[-3:] in ("MiB", "GiB") else 0
                peak[cname] = max(peak[cname], mib)
    # An agent that never took a turn (container start timeout, auth error, ...) is an infrastructure failure,
    # not a coordination failure: such runs are retried, and dropped from every condition if they keep failing.
    infra = [a for a, v in r["agents"].items() if v["status"] == "Error" and v["steps"] == 0]
    return {
        "name": name,
        "infra_failed": infra or None,
        "both_passed": e.get("both_passed"),
        "merge": f"{e.get('merge', {}).get('status')}/{e.get('merge', {}).get('strategy')}",
        "f1": e.get("feature1", {}).get("passed"), "f2": e.get("feature2", {}).get("passed"),
        "agent_s": round(r["duration_seconds"]),
        "wall": wall.read_text(encoding="utf-8-sig").strip() if wall.exists() else None,
        "turns": sum(a["steps"] for a in agents),
        "out_tokens": sum(a["output_tokens"] for a in agents),
        "cache_read": sum(a["cache_read_tokens"] for a in agents),
        "cost": round(r["total_cost"], 4),
        "messages": r.get("messages_sent"),
        "peak_mib": round(max(peak.values())) if peak else None,
        "tools": dict(collections.Counter(f"{c['agent']}:{c['tool']}" for c in calls)),
        "conflict_flagged": any(c.get("conflict") for c in calls),
        "claims": [(c["agent"], c.get("won"), c.get("lost")) for c in calls if c["tool"] == "claim"],
    }


if __name__ == "__main__":
    log_dir = Path(sys.argv[1])
    for n in sys.argv[2:]:
        print(json.dumps(one(log_dir, n)))
