"""`--profile harness`: the research harness's exact tool names, descriptions and reply texts, implemented on the
product's data model and code path (Session / Unit leases / Intent / Fact + Source). Used to reproduce the study
results on the product (REPRODUCTION-PLAN.md). Texts are copied verbatim from the frozen harness:
pilot/graph_tools.py (board mode) and coop/coop_tools.py (graph mode).

Differences from the harness are structural only: an Agent is a Session (bearer-token identity), a briefing fact is
a Source a Session is BRIEFED with, a post is an ASSERTED Fact whose SOURCED_FROM is that briefing, and a claim is a
lease that never expires (`ops.FOREVER`), exactly the harness's first-claim-wins semantics.
"""
from __future__ import annotations

import re

from . import ops

# ================================================================ HiddenBench (board mode) ====

BOARD_TOOLS = {
    "share_fact": "Post one of your own numbered facts to the shared board. It is marked source-checked.",
    "shared_facts": "List every fact posted to the board so far, with who posted it.",
    "who_has_not_shared": "Name the participants who hold facts nobody else has and have not posted them.",
}


def seed_board(store, fact_assignments: list[dict]) -> dict[str, dict]:
    """One Session per participant, each BRIEFED with its numbered facts. Returns {name: session dict}."""
    sessions = {}
    for a in fact_assignments:
        token, sid = ops.join(store, a["agent"], task="hiddenbench", client="harness")
        sessions[a["agent"]] = {"session": sid, "name": a["agent"], "token": token}
    rows = [{"sid": sessions[a["agent"]]["session"], "num": n, "uid": "briefing:" + ops.h(text), "text": text}
            for a in fact_assignments for n, text in enumerate(a["visible_facts"], start=1)]
    store.q("harness.seed_board", """
        UNWIND $rows AS r
        MATCH (s:Session {uid: r.sid})
        MERGE (src:Source {uid: r.uid}) ON CREATE SET src.kind = 'briefing', src.text = r.text
        MERGE (s)-[b:BRIEFED]->(src) SET b.num = r.num
        """, {"rows": rows})
    return sessions


def share_fact(store, sess: dict, fact_number: int) -> str:
    rows = store.q("harness.share_fact.check", "MATCH (:Session {uid: $sid})-[:BRIEFED {num: $n}]->(src:Source) "
                   "RETURN src.uid", {"sid": sess["session"], "n": fact_number}, ro=True)
    if not rows:  # the harness posts nothing for a number the agent doesn't hold
        return f"Source-checked: no. You have no Fact {fact_number}, so nothing was posted."
    ops.post_fact(store, sess, "", {"kind": "briefing", "ref": str(fact_number)})
    return f"Source-checked: yes. Your Fact {fact_number} is now on the board."


def shared_facts(store) -> str:
    rows = store.q("harness.shared_facts", "MATCH (s:Session)-[a:ASSERTED]->(f:Fact {status: 'source-checked'}) "
                   "RETURN s.name, f.text ORDER BY a.seq", ro=True)
    if not rows:
        return "The board is empty. Nobody has posted a fact yet."
    return "Facts on the board:\n" + "\n".join(f"- {who} (source-checked: this is in their briefing): {text}"
                                              for who, text in rows)


def who_has_not_shared(store) -> str:
    rows = store.q("harness.who_has_not_shared", """
        MATCH (holder:Session)-[:BRIEFED]->(src:Source)
        WITH src, collect(holder) AS holders
        WHERE size(holders) = 1
        WITH src, holders[0] AS holder
        OPTIONAL MATCH (holder)-[a:ASSERTED]->(f:Fact {source_uid: src.uid})
        WITH holder, count(a) AS posted
        WHERE posted = 0
        RETURN DISTINCT holder.name ORDER BY holder.name""", ro=True)
    if not rows:
        return "Everyone who holds facts nobody else has has posted."
    return f"These participants hold facts nobody else has and have not posted yet: {', '.join(r[0] for r in rows)}"


# ================================================================ CooperBench (graph mode) ====

COOP_TOOLS = {
    "declare_plan": ("Record the functions you intend to change (e.g. [\"click.termui.edit\"]); replaces your previous "
                     "plan. Waits up to 60 s for your partner's plan, then reports overlaps, ownership and your next step."),
    "claim": ("Claim functions so only you change them. Atomic: the first agent to claim a function owns it.\n"
              "Returns what you now own and who owns the rest."),
    "mark_committed": ("After you commit AND push (git push team <your branch>), pass the commit hash so your partner\n"
                       "knows your branch is ready."),
    "coordination_status": ("Your role, overlaps, who owns what, and your exact next step. With wait_seconds (max 110), "
                            "waits\nserver-side until your next step changes (for example, your partner commits)."),
    "impact_of": "List the functions that call, or are called by, `function` within 2 calls.",
}

CONFLICTS = """
MATCH (me:Session {name: $me})-[:DECLARED]->(:Intent {status: 'declared', task: 'harness-plan'})-[:TOUCHES]->(f:Unit)
MATCH p = (f)-[:DEPENDS_ON*0..3]-(g:Unit)<-[:TOUCHES]-(:Intent {status: 'declared', task: 'harness-plan'})<-[:DECLARED]-(other:Session)
WHERE other <> me
RETURN other.name, f.qualname AS mine, g.qualname AS theirs, length(p) - 2 AS call_hops,
       [n IN nodes(p) | coalesce(n.qualname, n.name)] AS via
ORDER BY call_hops LIMIT 20
"""


def normalize(name: str) -> str:
    n = name.strip().rstrip("()").replace("\\", "/")
    n = re.sub(r"\.py(?=[:/.]|$)", "", n)
    n = n.replace("::", ".").replace(":", ".").replace("/", ".")
    return n.removeprefix("src.")


def resolve_names(store, names: list[str]) -> tuple[list[str], dict[str, list[str]]]:
    found, problems = [], {}
    for raw in names:
        n = normalize(raw)
        rows = store.q("harness.resolve", "MATCH (f:Unit {kind: 'function'}) WHERE f.qualname = $n OR f.qualname "
                       "ENDS WITH $s RETURN f.qualname ORDER BY size(f.qualname) LIMIT 6",
                       {"n": n, "s": "." + n}, ro=True)
        exact = [r[0] for r in rows if r[0] == n]
        if exact or len(rows) == 1:
            found.append((exact or [rows[0][0]])[0])
        else:
            problems[raw] = [r[0] for r in rows]
    return found, problems


def problems_note(problems: dict) -> str:
    if not problems:
        return ""
    return "\nNot recorded (no unique match): " + "; ".join(
        f"{k} -> {', '.join(v) if v else 'no such function'}" for k, v in problems.items())


def set_plan(store, sess: dict, found: list[str]) -> None:
    store.q("harness.set_plan", """
        MATCH (s:Session {uid: $sid})
        MERGE (i:Intent {uid: $iid}) ON CREATE SET i.task = 'harness-plan', i.created_at = $now
        SET i.status = 'declared'
        MERGE (s)-[:DECLARED]->(i)
        WITH i OPTIONAL MATCH (i)-[old:TOUCHES]->() DELETE old
        WITH DISTINCT i UNWIND $found AS q
        MATCH (u:Unit {kind: 'function', qualname: q})
        MERGE (i)-[:TOUCHES {mode: 'modify'}]->(u)
        """, {"sid": sess["session"], "iid": f"{sess['name']}-plan", "now": ops.now_ms(), "found": found})


def overlaps(store, name: str) -> list:
    return store.q("harness.conflicts", CONFLICTS, {"me": name}, ro=True)


def claim(store, sess: dict, found: list[str]) -> tuple[list[str], dict[str, str]]:
    """Per-function compare-and-set on the lease (the harness claims what's free and reports the rest)."""
    rows = store.q("harness.claim", """
        UNWIND $found AS q
        MATCH (u:Unit {kind: 'function', qualname: q})
        WITH u, u.lease_owner IS NULL AS free
        SET u.lease_owner = CASE WHEN free THEN $sid ELSE u.lease_owner END,
            u.lease_expires = CASE WHEN free THEN $forever ELSE u.lease_expires END,
            u.lease_epoch = CASE WHEN free THEN coalesce(u.lease_epoch, 0) + 1 ELSE u.lease_epoch END
        WITH u MATCH (o:Session {uid: u.lease_owner})
        RETURN u.qualname, o.name""", {"found": found, "sid": sess["session"], "forever": ops.FOREVER})
    owner = dict(rows)
    mine = [q for q in found if owner.get(q) == sess["name"]]
    others = {q: o for q, o in owner.items() if o != sess["name"]}
    return mine, others


def mark_committed(store, sess: dict, commit: str) -> int:
    rows = store.q("harness.mark_committed", """
        MATCH (s:Session {uid: $sid}) SET s.commit = $c, s.committed_at = timestamp()
        WITH s OPTIONAL MATCH (u:Unit {lease_owner: $sid}) SET u.committed = true, u.commit = $c
        RETURN count(u)""", {"sid": sess["session"], "c": commit})
    return rows[0][0] if rows else 0


def planners(store) -> list[str]:
    return [r[0] for r in store.q("harness.planners", "MATCH (s:Session)-[:DECLARED]->(:Intent {task: 'harness-plan'}) "
                                  "RETURN s.name ORDER BY s.name", ro=True)]


def role(store, name: str) -> tuple[str, str | None]:
    ov = overlaps(store, name)
    partner = ov[0][0] if ov else next((p for p in planners(store) if p != name), None)
    if not ov:
        return "independent", partner
    shared = sorted({x for _, m, t, _, _ in ov for x in (m, t)})
    owners = {r[0] for r in store.q("harness.owners", "UNWIND $s AS q MATCH (u:Unit {kind: 'function', qualname: q}) "
                                    "WHERE u.lease_owner IS NOT NULL MATCH (o:Session {uid: u.lease_owner}) "
                                    "RETURN DISTINCT o.name", {"s": shared}, ro=True)}
    if name in owners:
        return "owner", partner
    if owners:
        return "builder", partner
    return "unclaimed", partner


def commit_of(store, name: str | None) -> str | None:
    if not name:
        return None
    rows = store.q("harness.commit_of", "MATCH (s:Session {name: $n}) RETURN s.commit", {"n": name}, ro=True)
    return rows[0][0] if rows else None


def next_step(store, name: str) -> tuple[str, str]:
    r, partner = role(store, name)
    mine, theirs = commit_of(store, name), commit_of(store, partner)
    agent = name
    if r == "independent":
        return "independent", "No overlap with your partner: finish your feature and follow the git merge sequence above."
    if r == "unclaimed":
        return "unclaimed", "You overlap with your partner but nobody has claimed the shared functions yet: claim() them now."
    if r == "owner":
        if not mine:
            return "owner-work", (f"You own the shared functions. Implement your WHOLE feature now, commit, "
                                  f"`git push team {agent}`, then mark_committed(<commit hash>).")
        if not theirs:
            return "owner-wait", (f"Done on your side. {partner} is building on top of your commit. Call "
                                  f"coordination_status(wait_seconds=110) until {partner} has pushed the combined tree.")
        return "owner-final", (f"{partner} pushed the combined tree at {theirs}. FINAL: `git fetch team && git merge "
                               f"--ff-only team/{partner}` then `git diff team/main..HEAD > patch.txt`. Do not resolve "
                               f"conflicts or merge any other way; if --ff-only fails, message {partner}.")
    if not theirs:
        return "builder-wait", (f"{partner} owns the shared functions: do NOT edit them. Implement and commit your other "
                                f"parts, then call coordination_status(wait_seconds=110) until {partner} has committed.")
    if not mine:
        return "builder-work", (f"{partner} committed at {theirs}. Now: commit your work, `git fetch team && git merge "
                                f"--no-edit team/{partner}`, add your changes to the shared functions on top, commit, "
                                f"`git push team {agent}`, then mark_committed(<commit hash>).")
    return "builder-final", ("You pushed the combined tree. FINAL: `git diff team/main..HEAD > patch.txt`. "
                             f"{partner} will fast-forward to your branch.")


def status_text(store, name: str) -> str:
    lines = []
    plan = store.q("harness.plan", "MATCH (:Session {name: $n})-[:DECLARED]->(:Intent {task: 'harness-plan'})"
                   "-[:TOUCHES]->(u:Unit) RETURN u.qualname ORDER BY u.qualname", {"n": name}, ro=True)
    lines.append("Your plan: " + (", ".join(r[0] for r in plan) or "not declared yet"))
    owned = store.q("harness.ownership", "MATCH (u:Unit {kind: 'function'}) WHERE u.lease_owner IS NOT NULL "
                    "MATCH (o:Session {uid: u.lease_owner}) RETURN o.name, u.qualname ORDER BY o.name, u.qualname",
                    ro=True)
    if owned:
        lines.append("Ownership: " + "; ".join(f"{o} owns {q}" for o, q in owned))
    ov = overlaps(store, name)
    if ov:
        lines.append("Overlaps with your partner's plan: " + "; ".join(
            f"your {m} vs {other}'s {t} ({hops} call(s) apart)" for other, m, t, hops, _ in ov))
    else:
        lines.append("No overlap with your partner's plan within 3 calls.")
    r, _ = role(store, name)
    lines.append(f"Your role: {r}. Next step: {next_step(store, name)[1]}")
    return "\n".join(lines)


def declare_plan_text(store, sess: dict, functions: list[str], team: int = 2) -> tuple[str, list[str]]:
    found, problems = resolve_names(store, functions)
    set_plan(store, sess, found)
    text = status_text(store, sess["name"])
    if len(planners(store)) < team:
        text = "Your partner has not declared a plan yet; call coordination_status again before editing.\n" + text
    return f"Plan recorded: {', '.join(found) or 'nothing'}." + problems_note(problems) + "\n" + text, found


def claim_text(store, sess: dict, functions: list[str]) -> tuple[str, list[str], dict]:
    found, problems = resolve_names(store, functions)
    mine, others = claim(store, sess, found)
    text = ("You own: " + (", ".join(mine) or "nothing new") + "."
            + "".join(f"\n{q} is owned by {o}: do not edit it; merge their branch once they commit."
                      for q, o in others.items()) + problems_note(problems))
    return text, mine, others


def mark_committed_text(store, sess: dict, commit: str) -> str:
    n = mark_committed(store, sess, commit)
    return f"Recorded your commit {commit} ({n} owned function(s)).\n" + status_text(store, sess["name"])


def impact_text(store, function: str) -> str:
    found, problems = resolve_names(store, [function])
    if not found:
        cands = problems.get(function) or []
        return f"No unique function named {function}. " + (f"Candidates: {', '.join(cands)}" if cands else "Nothing matches.")
    file, line, callers, callees = store.q("harness.impact", """
        MATCH (f:Unit {kind: 'function', qualname: $q})
        OPTIONAL MATCH (caller:Unit)-[:DEPENDS_ON*1..2]->(f)
        WITH f, collect(DISTINCT caller.qualname) AS callers
        OPTIONAL MATCH (f)-[:DEPENDS_ON*1..2]->(callee:Unit)
        RETURN f.file, f.line, callers, collect(DISTINCT callee.qualname)""", {"q": found[0]}, ro=True)[0]
    return (f"{found[0]} ({file}:{line})\n  called by (<=2 calls): {', '.join(callers[:25]) or 'nothing found'}\n"
            f"  calls (<=2 calls): {', '.join(callees[:25]) or 'nothing found'}\n"
            "Note: calls made through attributes, parameters or inheritance are not tracked.")
