"""Pre-registered analysis of a CooperBench batch (see results/preregistration_cb1.md). Run once, at the end.

  python coop/analyze_cb.py coop/plans/test1.csv runs/cooperbench
"""
import csv
import json
import math
import sys
from collections import defaultdict
from pathlib import Path

PRIMARY = ("graph", "protocol")
SECONDARY = [("graph", "coop", "pass"), ("protocol", "coop", "pass"), ("graph", "protocol", "coordinated")]


def sign_test(wins: int, losses: int) -> float:
    """Exact two-sided sign test on discordant pairs."""
    n = wins + losses
    if n == 0:
        return 1.0
    return min(1.0, 2 * sum(math.comb(n, k) for k in range(min(wins, losses) + 1)) / 2 ** n)


def holm(pvals: list[float]) -> list[float]:
    order = sorted(range(len(pvals)), key=lambda i: pvals[i])
    out, running = [0.0] * len(pvals), 0.0
    for rank, i in enumerate(order):
        running = max(running, min(1.0, (len(pvals) - rank) * pvals[i]))
        out[i] = running
    return out


def load_run(log_dir: Path, name: str) -> dict | None:
    res = next((log_dir / name).rglob("result.json"), None) if (log_dir / name).exists() else None
    ev = next((log_dir / name).rglob("eval.json"), None) if (log_dir / name).exists() else None
    if not res or not ev:
        return None
    r, e = json.loads(res.read_text(encoding="utf-8")), json.loads(ev.read_text(encoding="utf-8"))
    infra = any(v["status"] == "Error" and v["steps"] == 0 for v in r["agents"].values()) or bool(e.get("error"))
    return {
        "infra": infra,
        "pass": int(bool(e.get("both_passed"))),
        # coordination succeeded = the two submissions combine without conflict (identical trees or clean merge)
        "coordinated": int(e.get("merge", {}).get("status") in ("identical", "clean")),
        "turns": sum(v["steps"] for v in r["agents"].values()),
        "out_tokens": sum(v["output_tokens"] for v in r["agents"].values()),
        "agent_s": r["duration_seconds"],
    }


def analyze(plan: Path, log_dir: Path) -> str:
    runs = defaultdict(dict)  # pair -> condition -> run
    for row in csv.DictReader(open(plan, encoding="utf-8")):
        pair = (row["Repo"], row["Task"], row["Features"])
        runs[pair][row["Condition"]] = load_run(log_dir, row["Name"])
    conditions = sorted({c for p in runs.values() for c in p})
    # Drop-from-all rule: a pair counts only if every condition has a valid (non-infra) run.
    ok = {p: c for p, c in runs.items() if all(c.get(k) and not c[k]["infra"] for k in conditions)}
    dropped = sorted(set(runs) - set(ok))
    lines = [f"Pairs planned: {len(runs)} | analyzed (valid in all {len(conditions)} conditions): {len(ok)}"]
    if dropped:
        lines.append("Dropped from every condition: " + "; ".join(f"{r}/{t} f{f}" for r, t, f in dropped))
    lines.append("")
    lines.append(f"{'condition':10} {'pairs passed':>14} {'coordinated':>13} {'turns':>7} {'out tokens':>11} {'agent s':>8}")
    for c in conditions:
        xs = [p[c] for p in ok.values()]
        n = len(xs) or 1
        lines.append(f"{c:10} {sum(x['pass'] for x in xs):>6}/{len(xs):<3} {sum(x['pass'] for x in xs) / n:>4.0%} "
                     f"{sum(x['coordinated'] for x in xs):>6}/{len(xs):<3} "
                     f"{sum(x['turns'] for x in xs) / n:>7.1f} {sum(x['out_tokens'] for x in xs) / n:>11.0f} "
                     f"{sum(x['agent_s'] for x in xs) / n:>8.0f}")

    def paired(a, b, metric):
        w = sum(p[a][metric] > p[b][metric] for p in ok.values())
        l = sum(p[a][metric] < p[b][metric] for p in ok.values())
        return w, l, sign_test(w, l)

    w, l, p = paired(*PRIMARY, "pass")
    lines += ["", "Exact two-sided paired sign test, threshold p < 0.05",
              f"  [PRIMARY]   {PRIMARY[0]} vs {PRIMARY[1]} on pass: better on {w}, worse on {l}, p = {p:.3f}"]
    sec = [paired(a, b, m) for a, b, m in SECONDARY]
    for (a, b, m), (w, l, p), adj in zip(SECONDARY, sec, holm([s[2] for s in sec])):
        lines.append(f"  [secondary] {a} vs {b} on {m}: better on {w}, worse on {l}, raw p = {p:.3f}, "
                     f"Holm p = {adj:.3f} ({'survives' if adj < 0.05 else 'does not survive'})")
    return "\n".join(lines)


def demo() -> None:
    assert abs(sign_test(5, 0) - 0.0625) < 1e-9 and sign_test(2, 2) == 1.0
    assert [round(x, 3) for x in holm([0.01, 0.04, 0.03])] == [0.03, 0.06, 0.06]


if __name__ == "__main__":
    demo()
    print(analyze(Path(sys.argv[1]), Path(sys.argv[2])))
