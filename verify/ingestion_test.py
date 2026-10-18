"""Ingestion test on local repos (no LLM). Each repo is cloned to a throwaway folder (the original is never touched),
indexed into its own workspace, checked, and the workspace is deleted afterwards. Repos are reported by label only.

Per repo:
  1. coverage: every tracked source file in an indexed language is a File unit (vs `git ls-files`)
  2. function/class counts per language vs universal-ctags (independent parser)
  3. 20 random units: the line at the unit's start really defines that symbol
  4. co-change (one random pair) and ownership (one random unit) recomputed from raw `git log` / `git blame`
  5. init twice -> identical graph; one new commit -> only the changed units update
  6. index time, node/edge counts, graph memory

  python verify/ingestion_test.py repos.json > results/v1/ingestion_test.txt
  repos.json: [{"label": "A", "path": "C:/...", "note": "Go + JS"}, ...]
"""
import json
import random
import shutil
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from nodes import gitimport, indexer  # noqa: E402
from nodes.db import Store  # noqa: E402
from nodes.lang import EXT, tracked_files  # noqa: E402

CTAGS = Path.home() / "AppData" / "Local" / "nodes-tools" / "ctags" / "ctags.exe"
CT_FUNC = {"function", "method", "member", "func", "singletonMethod", "constructor", "getter", "setter", "generator"}
CT_CLASS = {"class", "struct", "interface", "enum", "trait", "module", "implementation", "type", "record", "object"}
WORK = Path("C:/dev/ingest")


def sh(*a, cwd=None):
    return subprocess.run(list(a), cwd=cwd, capture_output=True, check=True).stdout.decode("utf-8", "replace")


def run_init(st, repo):
    t0 = time.perf_counter()
    stats = indexer.index(st, repo, None, st.ws)
    units = stats.pop("_units")
    g = gitimport.import_git(st, repo, units)
    return stats, g, units, time.perf_counter() - t0


def ctags_counts(repo, files):
    lst = WORK / f"{repo.name}.files.txt"  # a file list, not argv: Windows caps command lines at 32K chars
    lst.write_text("\n".join(files), encoding="utf-8")
    out = subprocess.run([str(CTAGS), "--output-format=json", "--fields=+nKl", "-f", "-", "-L", str(lst)], cwd=repo,
                         capture_output=True).stdout.decode("utf-8", "replace")
    fn, cl = Counter(), Counter()
    for line in out.splitlines():
        try:
            t = json.loads(line)
        except json.JSONDecodeError:
            continue
        lang = (t.get("language") or "").lower()
        if t.get("kind") in CT_FUNC:
            fn[lang] += 1
        elif t.get("kind") in CT_CLASS:
            cl[lang] += 1
    return fn, cl


def check_repo(spec):
    label, src = spec["label"], Path(spec["path"])
    clone = WORK / label
    if clone.exists():
        shutil.rmtree(clone, onexc=lambda f, p, _: (__import__("os").chmod(p, 0o666), f(p)))
    sh("git", "-c", "core.longpaths=true", "clone", "-q", "--no-hardlinks", str(src), str(clone))
    st = Store(f"ingest-{label}")
    st.r.hdel("nodes:git", st.ws)  # a crashed earlier run may have left its import marker
    st.reset()
    st.setup()
    r = {"label": label, "note": spec.get("note", "")}
    stats, g, units, secs = run_init(st, clone)
    r["index_seconds"] = round(secs, 2)
    r["units"] = {k: stats[k] for k in ("units", "functions", "classes", "regions", "calls", "approximate_calls")}
    r["files_by_lang"] = stats["files_by_lang"]
    r["git"] = {k: g.get(k) for k in ("new_commits", "prs", "merges", "cochange_edges", "branches", "owned_units",
                                     "no_rationale_units")}
    r["nodes"] = st.q("x", "MATCH (n) RETURN count(n)", ro=True)[0][0]
    r["edges"] = st.q("x", "MATCH ()-[e]->() RETURN count(e)", ro=True)[0][0]
    mem = st.r.execute_command("GRAPH.MEMORY", "USAGE", st.g.name)
    r["graph_mb"] = mem[1] if isinstance(mem, list) else mem
    # 1. coverage
    files = tracked_files(clone)
    file_units = {u for (u,) in st.q("x", "MATCH (u:Unit {kind: 'file', status: 'active'}) RETURN u.path", ro=True)}
    missing = sorted(set(files) - file_units)
    r["coverage"] = {"tracked_source_files": len(files), "file_units": len(file_units & set(files)),
                     "missing": len(missing), "missing_examples_ext": sorted({Path(m).suffix for m in missing})[:5]}
    # 2. vs ctags (same file list)
    by_lang_fn = Counter(u["lang"] for u in units if u["kind"] == "function")
    by_lang_cl = Counter(u["lang"] for u in units if u["kind"] == "class")
    cfn, ccl = ctags_counts(clone, files) if files else (Counter(), Counter())
    r["vs_ctags"] = {lang: {"functions": [by_lang_fn[lang], cfn.get(_ctl(lang), 0)],
                            "classes": [by_lang_cl[lang], ccl.get(_ctl(lang), 0)]}
                     for lang in sorted(set(by_lang_fn) | set(by_lang_cl))}
    # 3. spans: the start line names the symbol
    rng = random.Random(7)
    sample = rng.sample([u for u in units if u["kind"] in ("function", "class")], min(20, sum(
        u["kind"] in ("function", "class") for u in units)))
    ok = 0
    for u in sample:
        lines = (clone / u["path"]).read_text(encoding="utf-8", errors="replace").splitlines()
        name = u["symbol"].rsplit(".", 1)[-1]
        window = " ".join(lines[u["start"] - 1:min(u["end"], u["start"] + 3)])  # decorators/attributes may precede
        ok += name in window or name.startswith("anon@")
    r["span_check"] = f"{ok}/{len(sample)}"
    # 4. co-change and ownership vs raw git
    pair = st.q("x", "MATCH (a:Unit {kind: 'file'})-[c:CO_CHANGES]->(b:Unit {kind: 'file'}) RETURN a.path, b.path, "
                "c.support LIMIT 50", ro=True)
    if pair:
        a, b, sup = rng.choice(pair)
        log = sh("git", "log", "-n5000", "--name-only", "--format=%x1e", cwd=clone)
        raw = sum(1 for blk in log.split("\x1e") if a in (fs := [x for x in blk.split("\n") if x])
                  and b in fs and len(fs) <= gitimport.MAX_FILES_PER_COMMIT)
        r["cochange_check"] = {"graph_support": sup, "raw_git_log": raw, "match": sup == raw}
    owned = st.q("x", "MATCH (p:Principal)-[o:OWNS_CODE]->(u:Unit) RETURN u.uid, u.path, u.start, u.end, "
                 "collect([p.uid, o.lines]) LIMIT 200", ro=True)
    if owned:
        uid, path, s, e, pl = rng.choice(owned)
        blame = sh("git", "blame", "--line-porcelain", "-L", f"{s},{e}", "--", path, cwd=clone)
        raw = Counter("person:" + ln[12:].strip("<>").lower() for ln in blame.splitlines() if ln.startswith("author-mail "))
        r["ownership_check"] = {"graph": dict(sorted((k, v) for k, v in pl)), "raw_git_blame": dict(sorted(raw.items())),
                                "match": dict(pl) == dict(raw)}
    # 5. idempotence and incremental
    snap = lambda: sorted(st.q("x", "MATCH (u:Unit) RETURN u.uid, u.hash, u.updated_at, u.status", ro=True))  # noqa
    before = snap()
    s2, g2, _, secs2 = run_init(st, clone)
    r["reinit"] = {"identical": snap() == before, "changed_units": s2["changed_units"], "new_commits": g2["new_commits"],
                   "seconds": round(secs2, 2)}
    target = next((u for u in units if u["kind"] == "function" and u["end"] > u["start"]), None)
    if target:
        p = clone / target["path"]
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
        lines.insert(target["end"] - 1, lines[target["end"] - 1] if False else _comment(target["lang"]) + "\n")
        p.write_text("".join(lines), encoding="utf-8")
        sh("git", "-c", "user.name=t", "-c", "user.email=t@x", "commit", "-qam", "Ingestion test edit", cwd=clone)
        before = dict((u, h) for u, h, *_ in snap())
        s3, g3, _, _ = run_init(st, clone)
        after = dict((u, h) for u, h, *_ in snap())
        changed = sorted(u for u in after if after[u] != before.get(u))
        r["one_commit"] = {"edited": target["uid"].split("#")[-1], "new_commits": g3["new_commits"],
                           "changed_units": len(changed), "changed_kinds": dict(Counter(c.split(":")[0] for c in changed))}
    st.reset()
    st.r.hdel("nodes:git", st.ws)
    return r


def _ctl(lang):
    return {"csharp": "c#", "cpp": "c++", "tsx": "typescript"}.get(lang, lang)


def _comment(lang):
    return {"python": "    # ingestion test", "ruby": "  # ingestion test"}.get(lang, "    // ingestion test")


if __name__ == "__main__":
    WORK.mkdir(parents=True, exist_ok=True)
    for spec in json.loads(Path(sys.argv[1]).read_text(encoding="utf-8")):
        try:
            print(json.dumps(check_repo(spec)), flush=True)
        except Exception as e:  # noqa: BLE001  report and continue with the next repo
            print(json.dumps({"label": spec["label"], "error": repr(e)[:300]}), flush=True)
