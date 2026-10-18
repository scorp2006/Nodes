"""Measure each FalkorDB feature Nodes relies on (numbers for FALKORDB-USAGE.md). No model calls.
Runs on the product's FalkorDB (:6380), in throwaway workspaces.

  python verify/falkordb_usage_bench.py > results/repro/falkordb_usage_bench.txt
"""
import statistics
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "coop")]
from nodes import indexer, ops  # noqa: E402
from nodes.db import Store  # noqa: E402
from prepare_repo import prepare  # noqa: E402


def pct(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, -(-len(xs) * p // 100) - 1)]


def ms(xs):
    return f"p50 {statistics.median(xs):.2f} ms, p99 {pct(xs, 99):.2f} ms (n={len(xs)})"


def main():
    repo_dir, pkg = prepare("pallets_click_task", "2068")
    st = Store("bench-usage")
    st.reset()
    st.setup()
    t0 = time.perf_counter()
    stats = indexer.index(st, repo_dir, pkg, "click")
    print(f"workspace graph ws:bench-usage = click 2068: {stats} indexed in {time.perf_counter() - t0:.2f} s")

    # 1. UNIQUE on uid: duplicates rejected by the database
    rejected = 0
    for uid in ("fn:click:src/click/termui.py#edit", "region:click:src/click/termui.py#imports"):
        try:
            st.q("x", "CREATE (:Unit {uid: $u})", {"u": uid})
        except Exception:  # noqa: BLE001
            rejected += 1
    print(f"UNIQUE uid: {rejected}/2 duplicate creates rejected by FalkorDB")
    lat = []
    for i in range(500):
        t = time.perf_counter()
        st.q("x", "MATCH (u:Unit {uid: $u}) RETURN u.kind", {"u": "fn:click:src/click/termui.py#edit"}, ro=True)
        lat.append((time.perf_counter() - t) * 1000)
    print(f"indexed uid lookup: {ms(lat)}")

    # 2. MANDATORY provenance: a Fact without source_uid, a Decision without reason -> rejected
    rejected = 0
    for q in ("CREATE (:Fact {uid: 'f-x', text: 'x'})", "CREATE (:Decision {uid: 'd-x', verdict: 'proceed'})"):
        try:
            st.q("x", q)
        except Exception:  # noqa: BLE001
            rejected += 1
    print(f"MANDATORY provenance: {rejected}/2 writes without provenance rejected by FalkorDB")

    # 3. single-query atomic lease (CAS + epoch + HELD history): latency, and a 50-thread race
    a = {"session": ops.join(st, "alice")[1], "name": "alice"}
    lat = []
    targets = ["src/click/termui.py:646-700", "click.core.Context.invoke"]
    for _ in range(300):
        t = time.perf_counter()
        ops.before_acting(st, a, "edit", targets)
        lat.append((time.perf_counter() - t) * 1000)
    print(f"before_acting (resolve + lease CAS + impact + Decision write), 2 targets: {ms(lat)}")
    print(f"  of which the lease CAS query alone: {ms(list(st.lat['gate.lease']))}")
    sessions = [{"session": ops.join(st, f"r{i}")[1], "name": f"r{i}"} for i in range(50)]
    won = []

    def race(s):
        if ops.before_acting(Store(st.ws), s, "edit", ["click.core.Option.__init__"])["decision"] == "proceed":
            won.append(s["name"])
    threads = [threading.Thread(target=race, args=(s,)) for s in sessions]
    [t.start() for t in threads]
    [t.join() for t in threads]
    epoch = st.q("x", "MATCH (u:Unit {qualname: 'click.core.Option.__init__'}) RETURN u.lease_epoch", ro=True)[0][0]
    print(f"50 concurrent agents claim one unit: winners = {len(won)}, lease epoch = {epoch}")

    # 4. pub/sub notification latency vs. the harness's 2 s polling loop
    p = st.r.pubsub()
    p.subscribe("ws:bench-usage:notify:bob")
    p.get_message(timeout=1)
    lat = []
    for i in range(200):
        t = time.perf_counter()
        st.r.publish("ws:bench-usage:notify:bob", str(i))
        m = None
        while m is None:
            m = p.get_message(ignore_subscribe_messages=True, timeout=1)
        lat.append((time.perf_counter() - t) * 1000)
    print(f"pub/sub publish -> subscriber receives: {ms(lat)}  (harness polled every 2000 ms: mean wait ~1000 ms)")

    # 5. GRAPH.COPY snapshot before a hand-off merge
    lat = []
    for i in range(10):
        lat.append(_timed(lambda: st.snapshot(f"bench{i}")))
        st.db.select_graph(f"ws:bench-usage:snap:bench{i}").delete()
    n = st.q("x", "MATCH (n) RETURN count(n)", ro=True)[0][0]
    print(f"GRAPH.COPY of the workspace ({n} nodes): {ms(lat)}")

    # 6. graph per workspace: isolation
    other = Store("bench-usage-2")
    other.reset()
    other.setup()
    seen = other.q("x", "MATCH (u:Unit) RETURN count(u)", ro=True)[0][0]
    print(f"graph per workspace: a second workspace sees {seen} of the first workspace's units")
    st.reset()
    other.reset()


def _timed(fn):
    t = time.perf_counter()
    fn()
    return (time.perf_counter() - t) * 1000


if __name__ == "__main__":
    main()
