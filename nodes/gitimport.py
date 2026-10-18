"""Git import (local .git, no GitHub login): commits, authors, branches, merge commits + PR numbers, file co-change,
and blame-based ownership + rationale per unit. Incremental: only commits after the last imported one are read.

Graph:
  (:Principal {kind:'human'})-[:AUTHORED]->(:Revision {sha, at, message, pr, is_merge})-[:CHANGED]->(:Unit {kind:'file'})
  (:Branch {name})-[:POINTS_TO]->(:Revision)        (:Revision)-[:MERGED_PR]->(:PullRequest {number})
  (:Unit file)-[:CO_CHANGES {support, confidence, resolved_by:'git-history'}]-(:Unit file)
  (:Principal)-[:OWNS_CODE {lines, share, resolved_by:'git-blame'}]->(:Unit)
  Unit.last_rev / Unit.rationale: the last commit touching the unit's lines, or "no recorded rationale"
"""
from __future__ import annotations

import re
import subprocess
from collections import Counter, defaultdict
from itertools import combinations
from pathlib import Path

PR = re.compile(r"(?:Merge pull request #(\d+))|(?:\(#(\d+)\)\s*$)|(?:Merged PR (\d+))", re.M)
TRIVIAL = re.compile(r"^\s*(wip|update[sd]?|fix(es|ed)?|changes?|misc|minor|tmp|temp|cleanup|\.|-)\s*\.?\s*$", re.I)
MAX_FILES_PER_COMMIT, MIN_SUPPORT, MAX_COMMITS, MAX_BLAME_FILES = 30, 2, 5000, 3000


def git(root: Path, *args) -> str:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, check=True).stdout.decode("utf-8", "replace")


def rationale(message: str, pr: int | None) -> str:
    """The commit's own explanation, or the explicit absence of one."""
    first = message.strip().splitlines()[0] if message.strip() else ""
    if not first or TRIVIAL.match(first) or len(first.split()) < 3:
        return "no recorded rationale" + (f" (PR #{pr})" if pr else "")
    return first[:200] + (f" (PR #{pr})" if pr else "")


def commits(root: Path, since: str | None) -> list[dict]:
    rng = [f"{since}..HEAD"] if since else ["HEAD"]
    out = git(root, "log", f"-n{MAX_COMMITS}", "--name-only", "--format=%x1e%H%x1f%an%x1f%ae%x1f%at%x1f%P%x1f%B%x1f",
              *rng)
    res = []
    for block in out.split("\x1e")[1:]:
        sha, name, email, at, parents, body, files = block.split("\x1f", 6)
        m = PR.search(body)
        pr = int(next(g for g in m.groups() if g)) if m else None
        res.append({"sha": sha, "author": name, "email": email.lower(), "at": int(at), "parents": parents.split(),
                    "message": body.strip()[:2000], "pr": pr, "files": [f for f in files.strip().splitlines() if f]})
    return res


def blame_units(root: Path, units: list[dict], msgs: dict[str, dict]) -> tuple[list[dict], list[dict]]:
    """Per unit: line counts by author email, and the last commit touching its lines (from `git blame`)."""
    by_path = defaultdict(list)
    for u in units:
        if u.get("start") and u["kind"] != "file":
            by_path[u["path"]].append(u)
    owners, last = [], []
    for path in sorted(by_path)[:MAX_BLAME_FILES]:
        try:
            out = git(root, "blame", "--line-porcelain", "--", path)
        except subprocess.CalledProcessError:
            continue
        line_info, cur, info = [], None, {}
        for ln in out.splitlines():
            if ln and re.match(r"^[0-9a-f]{40} ", ln):
                cur = ln.split()
                info.setdefault(cur[0], {})
            elif ln.startswith("author-mail "):
                info[cur[0]]["email"] = ln[12:].strip("<>").lower()
            elif ln.startswith("committer-time "):
                info[cur[0]]["at"] = int(ln.split()[1])
            elif ln.startswith("\t"):
                line_info.append(cur[0])
        for u in by_path[path]:
            shas = line_info[u["start"] - 1:u["end"]]
            if not shas:
                continue
            cnt = Counter(info[s].get("email", "") for s in shas)
            owners += [{"uid": u["uid"], "email": e, "lines": n, "share": round(n / len(shas), 3)} for e, n in cnt.items()]
            newest = max(set(shas), key=lambda s: info[s].get("at", 0))
            m = msgs.get(newest, {})
            last.append({"uid": u["uid"], "sha": newest, "rationale": rationale(m.get("message", ""), m.get("pr"))
                         if m else "no recorded rationale (commit outside the imported history)"})
    return owners, last


def import_git(store, root: Path, units: list[dict], batch: int = 500) -> dict:
    meta = store.r.hget("nodes:git", store.ws)
    since = meta.decode() if isinstance(meta, bytes) else meta
    try:
        git(root, "rev-parse", "HEAD")
    except subprocess.CalledProcessError:
        return {"git": "not a git repository"}
    try:
        if since:
            git(root, "cat-file", "-e", f"{since}^{{commit}}")
    except subprocess.CalledProcessError:
        since = None  # history was rewritten: re-read
    cs = commits(root, since)
    for i in range(0, len(cs), batch):
        store.q("git.commits", """
            UNWIND $rows AS c
            MERGE (p:Principal {uid: 'person:' + c.email}) ON CREATE SET p.kind = 'human', p.name = c.author
            MERGE (r:Revision {uid: 'rev:' + c.sha})
            ON CREATE SET r.sha = c.sha, r.at = c.at, r.message = c.message, r.pr = c.pr, r.is_merge = size(c.parents) > 1
            MERGE (p)-[:AUTHORED]->(r)
            WITH r, c UNWIND c.files AS f
            MATCH (u:Unit {kind: 'file', path: f})
            MERGE (r)-[:CHANGED]->(u)""", {"rows": cs[i:i + batch]})
    prs = [{"sha": c["sha"], "pr": c["pr"]} for c in cs if c["pr"]]
    if prs:
        store.q("git.prs", "UNWIND $rows AS c MATCH (r:Revision {uid: 'rev:' + c.sha}) "
                "MERGE (p:PullRequest {uid: 'pr:' + toString(c.pr)}) ON CREATE SET p.number = c.pr "
                "MERGE (r)-[:MERGED_PR]->(p)", {"rows": prs})
    # file co-change over ALL imported history (recomputed from the graph, so incremental imports stay exact)
    hist = store.q("git.history", "MATCH (r:Revision)-[:CHANGED]->(u:Unit {kind: 'file'}) "
                   "WITH r, collect(u.path) AS fs WHERE size(fs) <= $max RETURN fs", {"max": MAX_FILES_PER_COMMIT}, ro=True)
    single, pairs = Counter(), Counter()
    for (fs,) in hist:
        single.update(set(fs))
        pairs.update(combinations(sorted(set(fs)), 2))
    edges = [{"a": a, "b": b, "s": n, "c": round(n / min(single[a], single[b]), 4)}
             for (a, b), n in pairs.items() if n >= MIN_SUPPORT]
    store.q("git.cochange.clear", "MATCH (:Unit {kind: 'file'})-[e:CO_CHANGES]->(:Unit {kind: 'file'}) DELETE e")
    for i in range(0, len(edges), batch):
        store.q("git.cochange", "UNWIND $rows AS e MATCH (a:Unit {kind: 'file', path: e.a}), (b:Unit {kind: 'file', path: e.b}) "
                "MERGE (a)-[x:CO_CHANGES]->(b) SET x.support = e.s, x.confidence = e.c, x.resolved_by = 'git-history'",
                {"rows": edges[i:i + batch]})
    refs = [line.split() for line in git(root, "for-each-ref", "--format=%(refname:short) %(objectname)",
                                         "refs/heads", "refs/remotes").splitlines() if line.strip()]
    store.q("git.branches", "UNWIND $rows AS b MERGE (x:Branch {uid: 'branch:' + b[0]}) SET x.name = b[0], x.head = b[1] "
            "WITH x, b OPTIONAL MATCH (x)-[old:POINTS_TO]->() DELETE old "
            "WITH x, b MATCH (r:Revision {uid: 'rev:' + b[1]}) MERGE (x)-[:POINTS_TO]->(r)", {"rows": refs})
    msgs = {c["sha"]: c for c in cs}
    if since:  # blame may cite older commits: fetch their messages from the graph
        msgs |= {s: {"message": m, "pr": p} for s, m, p in store.q(
            "git.msgs", "MATCH (r:Revision) RETURN r.sha, r.message, r.pr", ro=True)}
    owners, last = blame_units(root, units, msgs)
    store.q("git.owners.clear", "MATCH (:Principal)-[o:OWNS_CODE]->(u:Unit) WHERE u.uid IN $u DELETE o",
            {"u": [u["uid"] for u in units]})
    for i in range(0, len(owners), batch):
        store.q("git.owners", "UNWIND $rows AS o MATCH (u:Unit {uid: o.uid}) "
                "MERGE (p:Principal {uid: 'person:' + o.email}) ON CREATE SET p.kind = 'human' "
                "MERGE (p)-[x:OWNS_CODE]->(u) SET x.lines = o.lines, x.share = o.share, x.resolved_by = 'git-blame'",
                {"rows": owners[i:i + batch]})
    for i in range(0, len(last), batch):
        store.q("git.rationale", "UNWIND $rows AS l MATCH (u:Unit {uid: l.uid}) SET u.last_rev = l.sha, u.rationale = l.rationale",
                {"rows": last[i:i + batch]})
    head = git(root, "rev-parse", "HEAD").strip()
    store.r.hset("nodes:git", store.ws, head)
    return {"new_commits": len(cs), "prs": len(prs), "merges": sum(len(c["parents"]) > 1 for c in cs),
            "cochange_edges": len(edges), "branches": len(refs), "owned_units": len({o["uid"] for o in owners}),
            "no_rationale_units": sum(lr["rationale"].startswith("no recorded") for lr in last), "head": head[:10]}
