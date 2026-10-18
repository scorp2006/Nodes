"""Can we pull any information back out of the graph? Ask it real questions and check every answer independently.

Code + git (a throwaway clone, indexed into its own workspace, deleted afterwards), for random samples:
  Q1 history     "which commits changed file F?"      graph Revision-[:CHANGED]->file   vs  git log -- F
  Q2 last change "which commit last touched unit U, and why?"  u.last_rev / u.rationale  vs  git blame + that commit's message
  Q3 ownership   "who wrote unit U?"                   Principal-[:OWNS_CODE {lines}]   vs  git blame author lines
  Q4 co-change   "what changes together with F?"       CO_CHANGES {support}             vs  git log pair counts
  Q5 calls       "what does function U call?"          DEPENDS_ON {kind:'calls'}        vs  the caller's source text
Agent context (an existing workspace, read-only):
  Q6 decisions   every Decision has its Session, cites units that exist, and matches the event stream one to one
  Q7 facts       every Fact has a Source; transcript facts: the cited transcript line really contains the sentence

  python verify/graph_questions.py C:/dev/ingest/C [--context-ws shop] [--facts-ws import-medusa]
"""
import argparse
import json
import random
import re
import subprocess
import sys
from collections import Counter
from itertools import combinations
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from nodes import gitimport, indexer  # noqa: E402
from nodes.db import Store  # noqa: E402

N = 30


def git(repo, *a):
    return subprocess.run(["git", "-C", str(repo), *a], capture_output=True, check=True).stdout.decode("utf-8", "replace")


def report(name, ok, total, note=""):
    print(f"{name:<52} {ok}/{total}" + (f"   {note}" if note else ""))
    return ok == total


def code_questions(repo: Path, rng) -> bool:
    st = Store(f"questions-{repo.name}")
    st.reset()
    st.setup()
    st.r.hdel("nodes:git", st.ws)
    stats = indexer.index(st, repo, None, repo.name)
    units = stats.pop("_units")
    gitimport.import_git(st, repo, units)
    good = True
    # Q1: per-file history (non-merge commits list their files; the import reads the same 5000 most recent commits)
    files = [p for (p,) in st.q("x", "MATCH (u:Unit {kind: 'file', status: 'active'}) RETURN u.path", ro=True)]
    ok = 0
    for f in rng.sample(files, min(N, len(files))):
        graph = {s for (s,) in st.q("x", "MATCH (r:Revision)-[:CHANGED]->(:Unit {kind: 'file', path: $p}) RETURN r.sha",
                                    {"p": f}, ro=True)}
        raw = set(git(repo, "log", "-n5000", "--full-history", "--no-merges", "--format=%H", "--", f).split()) & set(git(repo, "log", "-n5000", "--format=%H").split())
        ok += graph == raw
    good &= report("Q1 commits that changed a file", ok, min(N, len(files)))
    # Q2 + Q3: last change with its message, and line ownership, from git blame
    rows = st.q("x", "MATCH (u:Unit) WHERE u.kind IN ['function', 'class'] AND u.last_rev IS NOT NULL "
                "RETURN u.uid, u.path, u.start, u.end, u.last_rev, u.rationale", ro=True)
    ok2 = ok3 = 0
    sample = rng.sample(rows, min(N, len(rows)))
    for uid, path, s, e, sha, why in sample:
        blame = git(repo, "blame", "--line-porcelain", "-L", f"{s},{e}", "--", path)
        at, mail, cur, lines = {}, {}, None, []
        for ln in blame.splitlines():
            if re.match(r"^[0-9a-f]{40} ", ln):
                cur = ln.split()[0]
            elif ln.startswith("committer-time "):
                at[cur] = int(ln.split()[1])
            elif ln.startswith("author-mail "):
                mail[cur] = ln[12:].strip("<>").lower()
            elif ln.startswith("\t"):
                lines.append(cur)
        newest = max(set(lines), key=lambda c: at[c])
        msg = git(repo, "log", "-1", "--format=%B", newest)
        ok2 += sha == newest and why == gitimport.rationale(msg.strip()[:2000], None) or (
            sha == newest and why.startswith(gitimport.rationale(msg.strip()[:2000], None)[:40]))
        owners = dict(st.q("x", "MATCH (p:Principal)-[o:OWNS_CODE]->(:Unit {uid: $u}) RETURN p.uid, o.lines",
                           {"u": uid}, ro=True))
        ok3 += owners == {f"person:{m}": n for m, n in Counter(mail[c] for c in lines).items()}
    good &= report("Q2 last commit touching a unit + its message", ok2, len(sample))
    good &= report("Q3 who wrote a unit (blame lines per author)", ok3, len(sample))
    # Q4: co-change support recomputed from the raw log (same 30-file cap on the whole commit)
    pairs = st.q("x", "MATCH (a:Unit {kind: 'file'})-[c:CO_CHANGES]->(b:Unit {kind: 'file'}) RETURN a.path, b.path, c.support",
                 ro=True)
    if pairs:
        log = git(repo, "log", "-n5000", "--name-only", "--format=%x1e")
        counts = Counter()
        indexed = set(files)
        for blk in log.split("\x1e"):
            fs = [x for x in blk.split("\n") if x]
            if fs and len(fs) <= gitimport.MAX_FILES_PER_COMMIT:
                counts.update(combinations(sorted(set(fs) & indexed), 2))
        sample = rng.sample(pairs, min(N, len(pairs)))
        good &= report("Q4 files that change together (support)",
                       sum(counts[tuple(sorted((a, b)))] == s for a, b, s in sample), len(sample))
    # Q5: every sampled call edge is visible in the caller's own source
    edges = st.q("x", "MATCH (a:Unit)-[d:DEPENDS_ON {kind: 'calls'}]->(b:Unit) RETURN a.path, a.start, a.end, b.symbol, "
                 "d.resolved_by", ro=True)
    sample = rng.sample(edges, min(N, len(edges)))
    ok = 0
    for path, s, e, sym, by in sample:
        body = "\n".join((repo / path).read_text(encoding="utf-8", errors="replace").splitlines()[s - 1:e])
        parts = sym.split(".")
        name = parts[-2] if parts[-1] == "__init__" and len(parts) > 1 else parts[-1]  # Foo(...) runs Foo.__init__
        ok += re.search(rf"\b{re.escape(name)}\s*\(", body) is not None
    good &= report("Q5 call edges visible in the caller's source", ok, len(sample),
                   dict(Counter(e[4] for e in sample)).__repr__())
    st.reset()
    st.r.hdel("nodes:git", st.ws)
    return good


def context_questions(ws: str, facts_ws: str | None) -> bool:
    good = True
    if ws:
        st = Store(ws)
        dec = st.q("x", "MATCH (d:Decision) OPTIONAL MATCH (s:Session)-[:MADE]->(d) OPTIONAL MATCH (d)-[:CITES]->(u:Unit) "
                   "RETURN d.uid, s.uid, count(u)", ro=True)
        events = [e for e in (json.loads(f.get("e") or f.get(b"e")) for _, f in st.r.xrange(f"ws:{ws}:events"))
                  if e["kind"] == "decision"]
        good &= report(f"Q6 decisions with a session + cited units ({ws})", sum(bool(s) and n > 0 for _, s, n in dec), len(dec))
        good &= report(f"Q6 decision nodes == decision events ({ws})",
                       len({d for d, *_ in dec} & {e["decision"] for e in events}), max(len(dec), len(events)))
        chain = st.q("x", "MATCH p = (:Session)-[:MADE]->(:Decision)-[:CITES]->(:Unit) RETURN length(p) LIMIT 1", ro=True)
        print(f"   e.g. Session-MADE->Decision-CITES->Unit paths exist: {bool(chain)}")
    if facts_ws:
        st = Store(facts_ws)
        rows = st.q("x", "MATCH (f:Fact) OPTIONAL MATCH (f)-[:SOURCED_FROM]->(s:Source) RETURN f.text, s.ref", ro=True)
        ok = 0
        for text, ref in rows:
            if not ref:
                continue
            path, line = ref.rsplit("#L", 1)
            raw = Path(path).read_text(encoding="utf-8").splitlines()[int(line) - 1]
            words = [w for w in re.findall(r"\w{4,}", text) if "REDACTED" not in w][:8]
            ok += all(w in raw for w in words)
        good &= report(f"Q7 facts whose cited transcript line holds them ({facts_ws})", ok, len(rows))
    return good


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("repos", nargs="*")
    ap.add_argument("--context-ws", default="")
    ap.add_argument("--facts-ws")
    a = ap.parse_args()
    rng = random.Random(11)
    ok = True
    for r in a.repos:
        print(f"--- {Path(r).name}")
        ok &= code_questions(Path(r), rng)
    print("--- agent context")
    ok &= context_questions(a.context_ws, a.facts_ws)
    print("ALL ANSWERS MATCH" if ok else "SOME ANSWERS DIFFER (see above)")
