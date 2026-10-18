"""The six Nodes tools as plain functions over a Store (MCP-independent, so they can be tested and replayed).

Identity is a Session resolved from a bearer token by the server; tools never take an agent name argument.
Every write is one Cypher query (FalkorDB runs each query atomically and serializes writes per graph).
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import time
import uuid
from pathlib import Path

from .indexer import units_for_lines

LEASE_TTL_MS = 15 * 60 * 1000
FOREVER = 9_000_000_000_000_000  # harness profile: claims never expire (study semantics)
SCHEMA_VERSION = "0.1"


def now_ms() -> int:
    return int(time.time() * 1000)


def h(s: str, n: int = 16) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:n]


def envelope(data=None, next_step: str = "", sources=None, decision=None, budget: int = 1500) -> dict:
    out = {"ok": True, "data": data or {}, "sources": sources or [], "decision": decision, "next_step": next_step,
           "budget": {"tokens_used": 0, "tokens_max": budget}, "schema_version": SCHEMA_VERSION}
    # bound the response: trim the longest list inside data until it fits (~4 chars per token)
    while len(json.dumps(out)) // 4 > budget:
        lists = [(k, v) for k, v in out["data"].items() if isinstance(v, list) and v]
        if not lists:
            break
        k, v = max(lists, key=lambda kv: len(json.dumps(kv[1])))
        out["data"][k] = v[: len(v) // 2]
        out["data"].setdefault("truncated", []).append(k)
    out["budget"]["tokens_used"] = len(json.dumps(out)) // 4
    return out


def error(code: str, message: str, next_step: str) -> dict:
    return {"ok": False, "error": {"code": code, "message": message, "next_step": next_step},
            "schema_version": SCHEMA_VERSION}


# ------------------------------------------------------------------ identity -------

def join(store, name: str, task: str = "", client: str = "", worktree: str = "", branch: str = "") -> tuple[str, str]:
    """Mint a bearer token for a new session of agent `name`. Returns (token, session uid)."""
    token = "nds_" + secrets.token_urlsafe(24)
    sid = f"sess:{name}:{uuid.uuid4().hex[:8]}"
    store.q("join", """
        MERGE (p:Principal {uid: $pid}) ON CREATE SET p.kind = 'agent', p.name = $name
        CREATE (p)-[:RUNS]->(s:Session {uid: $sid, principal: $pid, name: $name, task: $task, client: $client,
                worktree: $wt, branch: $br, token_hash: $th, status: 'active', started_at: $now, last_heartbeat: $now})
        """, {"pid": f"agent:{name}", "name": name, "sid": sid, "task": task, "client": client, "wt": worktree,
              "br": branch, "th": h(token, 64), "now": now_ms()})
    store.r.hset("nodes:tokens", h(token, 64), json.dumps({"ws": store.ws, "session": sid, "name": name}))
    store.event("join", name, session=sid, task=task)
    return token, sid


def session_by_token(r, token: str) -> dict | None:
    raw = r.hget("nodes:tokens", h(token, 64))
    return json.loads(raw) if raw else None


# ------------------------------------------------------------------ targets -------

def resolve_targets(store, targets: list[str]) -> tuple[list[str], list[str]]:
    """uid | qualname | path | 'path:10-20' -> unit uids. Returns (uids, unknown)."""
    uids, unknown = [], []
    for t in targets:
        t = str(t)
        m = re.match(r"^(.*\.[A-Za-z0-9]+):(\d+)(?:-(\d+))?$", t)  # path:line in any language
        if m:
            got = units_for_lines(store, m.group(1), set(range(int(m.group(2)), int(m.group(3) or m.group(2)) + 1)))
        else:
            got = [r[0] for r in store.q("resolve", "MATCH (u:Unit) WHERE u.uid = $t OR u.qualname = $t "
                                         "OR (u.kind = 'file' AND u.path = $t) RETURN u.uid LIMIT 1",
                                         {"t": t}, ro=True)]
        (uids.extend(got) if got else unknown.append(t))
    return sorted(set(uids)), unknown


# ------------------------------------------------------------------ intents -------

def declare_intent(store, sess: dict, task: str, summary: str, touches: list[dict]) -> dict:
    rows = []
    for t in touches:
        uid = t.get("uid")
        if not uid and t.get("path"):
            found = [r[0] for r in store.q("resolve.path_symbol", "MATCH (u:Unit {path: $p}) WHERE u.symbol = $s "
                                           "OR ($s = '' AND u.kind = 'file') RETURN u.uid LIMIT 1",
                                           {"p": t["path"], "s": t.get("symbol", "")}, ro=True)]
            uid = found[0] if found else f"planned:{t['path']}#{t.get('symbol', '')}"
        if uid:
            rows.append({"uid": uid, "mode": t.get("mode", "modify"), "path": t.get("path", ""),
                         "symbol": t.get("symbol", "")})
    iid = f"intent:{sess['name']}:{uuid.uuid4().hex[:8]}"
    store.q("declare_intent", """
        MATCH (s:Session {uid: $sid})
        CREATE (s)-[:DECLARED]->(i:Intent {uid: $iid, task: $task, summary: $summary, status: 'declared', created_at: $now})
        WITH i UNWIND $rows AS r
        MERGE (u:Unit {uid: r.uid}) ON CREATE SET u.kind = 'planned', u.status = 'planned', u.path = r.path,
              u.symbol = r.symbol, u.lease_epoch = 0
        MERGE (i)-[:TOUCHES {mode: r.mode}]->(u)
        """, {"sid": sess["session"], "iid": iid, "task": task, "summary": summary[:500], "rows": rows,
              "now": now_ms()})
    same = store.q("overlap.same_unit", """
        MATCH (me:Intent {uid: $iid})-[:TOUCHES]->(u:Unit)<-[t:TOUCHES]-(o:Intent)<-[:DECLARED]-(s:Session)
        WHERE o <> me AND o.status IN ['declared', 'active'] AND s.uid <> $sid
        RETURN o.uid, s.name, u.uid, t.mode, u.lease_owner, o.created_at ORDER BY o.created_at
        """, {"iid": iid, "sid": sess["session"]}, ro=True)
    near = store.q("overlap.calls_2hop", """
        MATCH (me:Intent {uid: $iid})-[:TOUCHES]->(u:Unit)-[:DEPENDS_ON*1..2]-(v:Unit)<-[:TOUCHES]-(o:Intent)<-[:DECLARED]-(s:Session)
        WHERE o <> me AND o.status IN ['declared', 'active'] AND s.uid <> $sid
        RETURN DISTINCT s.name, u.uid, v.uid LIMIT 5
        """, {"iid": iid, "sid": sess["session"]}, ro=True)
    overlaps = [{"other_intent": o, "agent": a, "unit": u, "relation": "same-unit", "their_mode": m, "owner": own}
                for o, a, u, m, own, _ in same]
    overlaps += [{"agent": a, "unit": u, "their_unit": v,
                  "relation": "possible (calls within 2; informational, not predictive in our tests)"}
                 for a, u, v in near]
    first = same[0][1] if same else None
    suggested = {"owner": first, "handoff_order": [first, sess["name"]]} if first else {}
    store.event("intent", sess["name"], intent=iid, notify=sorted({o["agent"] for o in overlaps}))
    nxt = (f"{first} declared these units first: let them own the shared units. Work on your other units, and "
           f"call before_acting before every edit." if first else "No same-unit overlap. Call before_acting before every edit.")
    return envelope({"intent_uid": iid, "touches": [r["uid"] for r in rows], "overlaps": overlaps,
                     "suggested": suggested}, nxt)


# ------------------------------------------------------------------ the gate -------

LEASE = """
UNWIND $uids AS id
MATCH (u:Unit {uid: id})
OPTIONAL MATCH (o:Session {uid: u.lease_owner})
WITH u, (u.lease_owner IS NULL OR u.lease_owner = $me OR u.lease_expires < $now OR o.status = 'done') AS avail
WITH collect(u) AS us, collect(avail) AS av
WITH us, all(x IN av WHERE x) AS ok
UNWIND us AS u
WITH u, ok, u.lease_owner AS prev
SET u.lease_epoch = CASE WHEN ok AND (prev IS NULL OR prev <> $me) THEN coalesce(u.lease_epoch, 0) + 1 ELSE u.lease_epoch END,
    u.lease_owner = CASE WHEN ok THEN $me ELSE u.lease_owner END,
    u.lease_expires = CASE WHEN ok THEN $until ELSE u.lease_expires END
WITH u, ok, prev
MATCH (s:Session {uid: $me})
FOREACH (_ IN CASE WHEN ok AND (prev IS NULL OR prev <> $me) THEN [1] ELSE [] END |
    CREATE (s)-[:HELD {epoch: u.lease_epoch, from: $now, until: $until}]->(u))
RETURN u.uid, prev, ok, u.lease_epoch, u.lease_expires
"""


def before_acting(store, sess: dict, action: str, targets: list[str], relies_on_facts: list[str] | None = None,
                  ttl_ms: int = LEASE_TTL_MS, workspace_root: Path | None = None) -> dict:
    me, name, t = sess["session"], sess["name"], now_ms()
    uids, unknown = resolve_targets(store, targets)
    out = {"action": action, "targets": uids, "unknown_targets": unknown, "impact": [], "lease": []}
    facts = relies_on_facts or []
    bad = [r for r in store.q("gate.facts", "UNWIND $f AS id OPTIONAL MATCH (x:Fact {uid: id}) "
                              "RETURN id, x.status", {"f": facts}, ro=True) if r[1] != "source-checked"] if facts else []
    if bad:
        return _decide(store, sess, out, "escalate", f"relies on a fact that is not source-checked: {bad[0][0]}",
                       [name, "-RELIES_ON->", bad[0][0], "-STATUS->", str(bad[0][1] or "missing")], uids, facts)
    rows = store.q("gate.lease", LEASE, {"uids": uids, "me": me, "now": t, "until": t + ttl_ms}) if uids else []
    ok = all(r[2] for r in rows)
    out["impact"] = [{"owner": o, "mine": a, "theirs": b, "within": "2 calls", "note": "informational"} for o, a, b in
                     store.q("gate.impact", """
        UNWIND $uids AS id MATCH (u:Unit {uid: id})-[:DEPENDS_ON*1..2]-(v:Unit)
        WHERE v.lease_owner IS NOT NULL AND v.lease_owner <> $me AND v.lease_expires >= $now
        MATCH (o:Session {uid: v.lease_owner})
        RETURN DISTINCT o.name, u.uid, v.uid LIMIT 5""", {"uids": uids, "me": me, "now": t}, ro=True)]
    if ok:
        out["lease"] = [{"uid": u, "epoch": e, "expires": x} for u, _, _, e, x in rows]
        store.q("gate.clear_waits", "MATCH (:Session {uid: $me})-[w:WAITS_FOR]->() WHERE w.unit IN $u DELETE w",
                {"me": me, "u": uids})
        taken = {u: p for u, p, *_ in rows if p and p != me}
        done = {s: (n, c) for s, n, c in store.q(
            "gate.prev", "UNWIND $p AS id MATCH (s:Session {uid: id}) WHERE s.status = 'done' "
            "RETURN s.uid, s.name, s.commit", {"p": list(set(taken.values()))}, ro=True)} if taken else {}
        prev = {u: p for u, p in taken.items() if p in done}  # from a finished owner: a hand-off
        if prev:  # snapshot first, for dry-run / replay of the merge that follows
            u0, s0 = next(iter(prev.items()))
            pname, commit = done.get(s0, ("?", None))
            snap = store.snapshot(f"handoff-{uuid.uuid4().hex[:8]}")
            store.event("handoff", name, units=list(prev), notify=sorted({done[s][0] for s in prev.values() if s in done}))
            return _decide(store, sess, out, "handoff",
                           f"{pname} finished; you now own {', '.join(prev)}. Merge {pname}'s commit {commit} first, "
                           f"then build on top.", [pname, "-DONE_AT->", str(commit), "-HANDS->", u0, "-TO->", name],
                           uids, facts, snapshot=snap)
        expired = [u for u in taken if u not in prev]
        return _decide(store, sess, out, "proceed", "every target is yours (lease granted or renewed)"
                       + (f"; took over expired lease(s) on {', '.join(expired)}" if expired else ""),
                       [name, "-HOLDS->", ", ".join(uids) or "(no unit)"], uids, facts)
    blocked = [(u, p) for u, p, *_ in rows if p and p != me]
    owners = {s: n for s, n in store.q("gate.owners", "UNWIND $p AS id MATCH (s:Session {uid: id}) RETURN s.uid, s.name",
                                       {"p": [p for _, p in blocked]}, ro=True)}
    u0, s0 = next((u, p) for u, p in blocked if p in owners)
    cycle = store.q("gate.cycle", "MATCH p = (:Session {uid: $o})-[:WAITS_FOR*1..10]->(:Session {uid: $me}) "
                    "RETURN [n IN nodes(p) | n.name] LIMIT 1", {"o": s0, "me": me}, ro=True)
    if cycle:
        path = [name] + [x for n in cycle[0][0] for x in ("-WAITS_FOR->", n)]
        return _decide(store, sess, out, "escalate", f"deadlock: {' waits for '.join([name] + cycle[0][0])}; "
                       "a human or lead must break the cycle", path, uids, facts)
    store.q("gate.wait", "MATCH (a:Session {uid: $me}), (o:Session {uid: $o}) MERGE (a)-[w:WAITS_FOR {unit: $u}]->(o) "
            "SET w.since = $now", {"me": me, "o": s0, "u": u0, "now": t})
    store.event("defer", name, unit=u0, notify=[owners[s0]])
    return _decide(store, sess, out, "defer", f"{owners[s0]} holds {u0} and is still working; wait for their "
                   "coordinate(op='done'), then call before_acting again",
                   [name, "-NEEDS->", u0, "-LEASED_BY->", owners[s0]], uids, facts)


def _decide(store, sess, out, verdict, reason, path, uids, facts, snapshot=None) -> dict:
    did = f"dec:{uuid.uuid4().hex[:12]}"
    store.q("gate.decision", """
        MATCH (s:Session {uid: $sid})
        CREATE (s)-[:MADE]->(d:Decision {uid: $did, verdict: $v, reason: $r, path: $p, action: $a, at: $now,
                                          snapshot: $snap})
        WITH d OPTIONAL MATCH (s2:Session {uid: $sid})-[:DECLARED]->(i:Intent) WHERE i.status IN ['declared', 'active']
        WITH d, i ORDER BY i.created_at DESC LIMIT 1
        FOREACH (_ IN CASE WHEN i IS NULL THEN [] ELSE [1] END | MERGE (d)-[:FOR]->(i))
        WITH d UNWIND (CASE WHEN size($u) = 0 THEN [null] ELSE $u END) AS uid
        OPTIONAL MATCH (u:Unit {uid: uid})
        FOREACH (_ IN CASE WHEN u IS NULL THEN [] ELSE [1] END | MERGE (d)-[:CITES]->(u))
        """, {"sid": sess["session"], "did": did, "v": verdict, "r": reason, "p": " ".join(path),
              "a": out["action"], "now": now_ms(), "snap": snapshot or "", "u": uids})
    if facts:
        store.q("gate.cites", "MATCH (d:Decision {uid: $d}) UNWIND $f AS id MATCH (x:Fact {uid: id}) "
                "MERGE (d)-[:CITES]->(x)", {"d": did, "f": facts})
    store.event("decision", sess["name"], verdict=verdict, decision=did, path=" ".join(path))
    nxt = {"proceed": "Make the change now. Call before_acting again before the next one.",
           "handoff": "Merge the finished owner's commit first, then make your change on top.",
           "defer": "Work on something else, or call status(wait_seconds=60) to be told when it frees up.",
           "escalate": "Stop and ask a human (or your lead) to resolve this before acting."}[verdict]
    out |= {"decision": verdict, "reason": reason, "path": path, "decision_uid": did, "snapshot": snapshot}
    return envelope(out, nxt, decision=verdict)


# ------------------------------------------------------------------ facts -------

def check_file_source(root: Path, ref: str, quote: str) -> tuple[bool, str]:
    m = re.match(r"^(.*?)(?::(\d+)(?:-(\d+))?)?$", ref)
    p = (root / m.group(1)).resolve()
    if not (p.is_file() and p.is_relative_to(root.resolve())):
        return False, ""
    text = p.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    if m.group(2):
        lines = lines[int(m.group(2)) - 1:int(m.group(3) or m.group(2))]
    norm = lambda s: " ".join(s.split()).lower()  # noqa: E731
    return bool(quote.strip()) and norm(quote) in norm("\n".join(lines)), h(text, 12)


def post_fact(store, sess: dict, text: str, source_ref: dict, root: Path | None = None) -> dict:
    kind, ref, quote = source_ref.get("kind", ""), str(source_ref.get("ref", "")), source_ref.get("quote") or text
    if kind == "briefing":
        num = re.sub(r"\D", "", ref)
        rows = store.q("fact.briefing", "MATCH (s:Session {uid: $sid})-[b:BRIEFED {num: $n}]->(src:Source) "
                       "RETURN src.uid, src.text", {"sid": sess["session"], "n": int(num or -1)}, ro=True)
        checked, src_uid, text = (True, rows[0][0], rows[0][1]) if rows else (False, f"unverified:briefing:{ref}", text)
    elif kind == "file" and root is not None:
        checked, digest = check_file_source(root, ref, quote)
        src_uid = f"file:{ref}@{digest}" if checked else f"unverified:file:{ref}"
    else:
        checked, src_uid = False, f"unverified:{kind}:{ref}"
    status = "source-checked" if checked else "unverified"
    fid = "fact:" + h(text)
    store.q("post_fact", """
        MERGE (src:Source {uid: $src}) ON CREATE SET src.kind = $kind, src.ref = $ref
        WITH src OPTIONAL MATCH (f0:Fact {uid: $fid})
        // MANDATORY source_uid is checked when the node is created, so create it with every property at once
        FOREACH (_ IN CASE WHEN f0 IS NULL THEN [1] ELSE [] END |
            CREATE (:Fact {uid: $fid, text: $text, source_uid: $src, status: $status, recorded_at: $now}))
        WITH src MATCH (f:Fact {uid: $fid})
        MERGE (f)-[:SOURCED_FROM {quote: $quote}]->(src)
        WITH f MATCH (s:Session {uid: $sid})
        OPTIONAL MATCH ()-[prev:ASSERTED]->()
        WITH f, s, count(prev) AS seq
        MERGE (s)-[a:ASSERTED]->(f) ON CREATE SET a.seq = seq, a.status = $status
        """, {"src": src_uid, "kind": kind, "ref": ref, "fid": fid, "text": text[:2000], "status": status,
              "now": now_ms(), "quote": quote[:500], "sid": sess["session"]})
    store.event("fact", sess["name"], fact=fid, status=status)
    return envelope({"fact_uid": fid, "status": status}, "Teammates will see it in status() and context_for().",
                    sources=[{"kind": kind, "ref": ref, "quote": quote[:200], "checked": checked}])


# ------------------------------------------------------------------ coordinate -------

def coordinate(store, sess: dict, op: str, target: str = "", text: str = "") -> dict:
    me, name = sess["session"], sess["name"]
    if op == "done":
        waiters = [r[0] for r in store.q("coord.done", """
            MATCH (s:Session {uid: $me}) SET s.status = 'done', s.commit = $c, s.done_at = $now
            WITH s OPTIONAL MATCH (s)-[w:WAITS_FOR]->() DELETE w
            WITH s OPTIONAL MATCH (s)-[:DECLARED]->(i:Intent) SET i.status = 'done'
            WITH s OPTIONAL MATCH (s)-[:COMMITTED]->(c:Commitment) SET c.status = 'fulfilled'
            WITH s OPTIONAL MATCH (x:Session)-[:WAITS_FOR]->(s) RETURN DISTINCT x.name""",
            {"me": me, "c": text or "", "now": now_ms()}) if r[0]]
        store.event("done", name, commit=text, notify=waiters)
        return envelope({"commit": text, "notified": waiters},
                        "Done. Your units can now be handed off to teammates who need them.")
    if op == "release":
        uids, _ = resolve_targets(store, [target]) if target else ([], [])
        rel = [r[0] for r in store.q("coord.release", """
            MATCH (u:Unit {lease_owner: $me}) WHERE size($u) = 0 OR u.uid IN $u
            SET u.lease_owner = NULL, u.lease_expires = NULL RETURN u.uid""", {"me": me, "u": uids})]
        store.event("release", name, units=rel)
        return envelope({"released": rel}, "Released.")
    if op in ("request", "commit_to"):
        rid = f"{'req' if op == 'request' else 'commit'}:{uuid.uuid4().hex[:10]}"
        if op == "commit_to":
            store.q("coord.commit_to", "MATCH (s:Session {uid: $me}) CREATE (s)-[:COMMITTED]->(:Commitment "
                    "{uid: $id, text: $t, status: 'open', at: $now})", {"me": me, "id": rid, "t": text, "now": now_ms()})
            return envelope({"commitment_uid": rid}, "Teammates can rely on this; fulfil it with coordinate(op='done').")
        uids, _ = resolve_targets(store, [target])
        rows = store.q("coord.request", """
            MATCH (s:Session {uid: $me}), (u:Unit {uid: $u})
            OPTIONAL MATCH (o:Session {uid: u.lease_owner})
            CREATE (s)-[:REQUESTED]->(r:Request {uid: $id, text: $t, status: 'open', at: $now})-[:ABOUT]->(u)
            FOREACH (_ IN CASE WHEN o IS NULL THEN [] ELSE [1] END | CREATE (r)-[:TO]->(o))
            RETURN o.name""", {"me": me, "u": uids[0] if uids else "", "id": rid, "t": text, "now": now_ms()})
        owner = rows[0][0] if rows else None
        store.event("request", name, request=rid, notify=[owner] if owner else [])
        return envelope({"request_uid": rid, "to": owner}, f"Sent to {owner}." if owner else "Nobody owns that unit.")
    if op in ("accept", "decline"):
        store.q("coord.answer", "MATCH (r:Request {uid: $id}) SET r.status = $st",
                {"id": target, "st": "accepted" if op == "accept" else "declined"})
        return envelope({"request_uid": target, "status": op + "ed"}, "Recorded.")
    return error("INVALID_ARGS", f"unknown op {op!r}", "Use request, accept, decline, done, release or commit_to.")


# ------------------------------------------------------------------ reads -------

def digest(store, sess: dict) -> dict:
    me = sess["session"]
    owns = [r[0] for r in store.q("read.owns", "MATCH (u:Unit {lease_owner: $me}) WHERE u.lease_expires >= $now "
                                  "RETURN u.uid LIMIT 50", {"me": me, "now": now_ms()}, ro=True)]
    waits = store.q("read.waits", "MATCH (:Session {uid: $me})-[w:WAITS_FOR]->(o:Session) RETURN o.name, w.unit, "
                    "o.status, o.commit", {"me": me}, ro=True)
    reqs = store.q("read.requests", "MATCH (x:Session)-[:REQUESTED]->(r:Request {status: 'open'})-[:TO]->(:Session "
                   "{uid: $me}) RETURN r.uid, x.name, r.text", {"me": me}, ro=True)
    facts = store.q("read.facts", "MATCH (s:Session)-[a:ASSERTED]->(f:Fact) RETURN s.name, f.text, f.status, "
                    "f.source_uid ORDER BY a.seq DESC LIMIT 20", ro=True)
    return {"you_own": owns, "waiting_for": [{"agent": a, "unit": u, "status": s, "commit": c} for a, u, s, c in waits],
            "requests_to_you": [{"uid": u, "from": f, "text": t} for u, f, t in reqs],
            "facts": [{"by": a, "text": t, "status": s, "source": src} for a, t, s, src in facts]}


def context_for(store, sess: dict, query: str, budget: int = 1500) -> dict:
    units = store.q("read.context_units", """
        MATCH (u:Unit) WHERE u.kind <> 'file' AND (u.uid CONTAINS $q OR u.path CONTAINS $q OR u.symbol CONTAINS $q)
        OPTIONAL MATCH (o:Session {uid: u.lease_owner})
        RETURN u.uid, u.kind, o.name, u.lease_expires LIMIT 25""", {"q": query}, ro=True)
    data = digest(store, sess) | {"units": [{"uid": u, "kind": k, "owner": o if (x or 0) >= now_ms() else None}
                                            for u, k, o, x in units],
                                  "recent": store.events(15)}
    return envelope(data, "Declare what you will change with declare_intent, then call before_acting before editing.",
                    sources=[{"kind": "fact", "ref": f["source"], "status": f["status"]} for f in data["facts"]],
                    budget=budget)
