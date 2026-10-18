"""Opt-in: `nodes import-sessions --workspace W`. Reads past Claude Code transcripts (~/.claude/projects/*/*.jsonl)
whose cwd is inside the workspace's repository (worktrees included), extracts decision-rationale sentences from the
assistant's messages (no LLM: sentences that say why), redacts secrets, and stores each as a Fact at trust
'extracted' whose Source is the exact transcript line. Local only; nothing leaves the machine.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from . import identity, ops, redact

WHY = re.compile(r"\b(because|so that|decided|chose|instead of|rather than|trade-?off|the reason|in order to|"
                 r"we can't|we cannot|to avoid|which means)\b", re.I)
SENT = re.compile(r"(?<=[.!?])\s+")


def transcripts_for(common_dir: str, home: Path | None = None) -> list[Path]:
    base = (home or Path.home()) / ".claude" / "projects"
    out = []
    for f in sorted(base.glob("*/*.jsonl")) if base.exists() else []:
        try:
            with f.open(encoding="utf-8") as fh:
                for line in fh:
                    cwd = json.loads(line).get("cwd")
                    if cwd:
                        if identity.git_common_dir(cwd) == common_dir:
                            out.append(f)
                        break
        except (OSError, json.JSONDecodeError):
            continue
    return out


def rationale_sentences(path: Path, limit: int = 200):
    """(line number, sentence) for assistant text that explains a choice."""
    with path.open(encoding="utf-8") as fh:
        for n, line in enumerate(fh, 1):
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("type") != "assistant":
                continue
            for block in (rec.get("message") or {}).get("content") or []:
                if isinstance(block, dict) and block.get("type") == "text":
                    for s in SENT.split(block.get("text", "")):
                        s = " ".join(s.split())
                        if 30 <= len(s) <= 400 and WHY.search(s):
                            yield n, redact.text(s)
                            limit -= 1
                            if limit <= 0:
                                return


def import_sessions(store, common_dir: str, home: Path | None = None) -> dict:
    files = transcripts_for(identity._norm(common_dir), home)
    store.q("import.session", """
        MERGE (p:Principal {uid: 'nodes:importer'}) ON CREATE SET p.kind = 'service', p.name = 'session importer'
        WITH p OPTIONAL MATCH (s0:Session {uid: 'import:sessions'})
        FOREACH (_ IN CASE WHEN s0 IS NULL THEN [1] ELSE [] END |
            CREATE (p)-[:RUNS]->(:Session {uid: 'import:sessions', principal: 'nodes:importer', name: 'importer',
                    status: 'done', started_at: $now, task: 'import-sessions', client: 'nodes', token_hash: ''}))""",
            {"now": ops.now_ms()})
    rows = []
    for f in files:
        for n, s in rationale_sentences(f):
            rows.append({"fid": "fact:" + hashlib.sha256(s.encode()).hexdigest()[:16], "text": s,
                         "src": f"transcript:{f.name}#L{n}", "ref": f"{f}#L{n}"})
    for i in range(0, len(rows), 200):
        store.q("import.facts", """
            UNWIND $rows AS r
            MERGE (src:Source {uid: r.src}) ON CREATE SET src.kind = 'transcript', src.ref = r.ref
            WITH r, src OPTIONAL MATCH (f0:Fact {uid: r.fid})
            FOREACH (_ IN CASE WHEN f0 IS NULL THEN [1] ELSE [] END |
                CREATE (:Fact {uid: r.fid, text: r.text, source_uid: r.src, status: 'extracted', recorded_at: $now}))
            WITH r, src MATCH (f:Fact {uid: r.fid}) MERGE (f)-[:SOURCED_FROM]->(src)
            WITH f MATCH (s:Session {uid: 'import:sessions'})
            OPTIONAL MATCH ()-[prev:ASSERTED]->()
            WITH f, s, count(prev) AS seq
            MERGE (s)-[a:ASSERTED]->(f) ON CREATE SET a.seq = seq, a.status = 'extracted'""",
                {"rows": rows[i:i + 200], "now": ops.now_ms()})
    return {"transcripts": len(files), "rationale_facts": len(rows)}
