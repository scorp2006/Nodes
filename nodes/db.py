"""Storage for Nodes: one FalkorDB graph per workspace, with the database enforcing what it can.

- UNIQUE constraint on `uid` for every core label (duplicate nodes are rejected by the database)
- MANDATORY constraints: a Fact must carry `source_uid` (provenance), a Decision must carry `reason` and `verdict`,
  a Session must carry `principal`. A write without them is rejected by FalkorDB, not by our code.
- every query is timed; latencies go to an in-memory window (for FALKORDB-USAGE.md) and the trace file
- Redis Stream `ws:{ws}:events` (append-only log) and pub/sub `ws:{ws}:notify:{agent}` on the same server
- GRAPH.COPY snapshots `ws:{ws}:snap:{id}`
"""
from __future__ import annotations

import json
import statistics
import threading
import time
from collections import defaultdict, deque

from falkordb import FalkorDB
from redis.exceptions import ResponseError

LABELS = ["Principal", "Session", "Unit", "Intent", "Decision", "Fact", "Source", "Request", "Commitment"]
MANDATORY = [("Fact", "source_uid"), ("Decision", "reason"), ("Decision", "verdict"), ("Session", "principal")]
EXTRA_INDEXES = [("Unit", "lease_owner"), ("Unit", "path"), ("Unit", "qualname"), ("Intent", "status"),
                 ("Session", "token_hash"), ("Session", "name")]


class Store:
    def __init__(self, workspace: str, host: str = "localhost", port: int = 6380):
        self.ws = workspace
        self.db = FalkorDB(host=host, port=port)
        self.r = self.db.connection
        self.g = self.db.select_graph(f"ws:{workspace}")
        self.lat = defaultdict(lambda: deque(maxlen=5000))  # query name -> recent latencies (ms)
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ setup -------
    def setup(self) -> None:
        """Indexes and constraints. Idempotent. Constraints build asynchronously; we wait until OPERATIONAL."""
        for label in LABELS:
            self._try(f"CREATE INDEX FOR (n:{label}) ON (n.uid)")
            self._try_cmd("GRAPH.CONSTRAINT", "CREATE", self.g.name, "UNIQUE", "NODE", label, "PROPERTIES", 1, "uid")
        for label, prop in EXTRA_INDEXES:
            self._try(f"CREATE INDEX FOR (n:{label}) ON (n.{prop})")
        for label, prop in MANDATORY:
            self._try_cmd("GRAPH.CONSTRAINT", "CREATE", self.g.name, "MANDATORY", "NODE", label, "PROPERTIES", 1, prop)
        for _ in range(100):
            states = [r[4] for r in self.g.ro_query("CALL db.constraints()").result_set]
            if states and all(s == "OPERATIONAL" for s in states):
                return
            time.sleep(0.05)
        raise RuntimeError(f"constraints not operational: {states}")

    def _try(self, q):
        try:
            self.g.query(q)
        except ResponseError:  # already exists
            pass

    def _try_cmd(self, *args):
        try:
            self.r.execute_command(*args)
        except ResponseError:  # already exists
            pass

    def reset(self) -> None:
        for key in (self.g.name,):
            try:
                self.db.select_graph(key).delete()
            except ResponseError:
                pass
        self.r.delete(f"ws:{self.ws}:events")

    # ------------------------------------------------------------------ queries -------
    def q(self, name: str, cypher: str, params: dict | None = None, ro: bool = False):
        t0 = time.perf_counter()
        res = (self.g.ro_query if ro else self.g.query)(cypher, params or {})
        self.lat[name].append((time.perf_counter() - t0) * 1000)
        return res.result_set

    def latency_report(self) -> dict:
        return {k: {"n": len(v), "p50_ms": round(statistics.median(v), 2),
                    "p99_ms": round(sorted(v)[min(len(v) - 1, -(-len(v) * 99 // 100) - 1)], 2)}
                for k, v in sorted(self.lat.items()) if v}

    # ------------------------------------------------------------------ events -------
    def event(self, kind: str, agent: str | None, **data) -> None:
        """Append to the workspace event log and notify the agents it concerns."""
        payload = {"kind": kind, "agent": agent or "", "t": time.time(), **data}
        t0 = time.perf_counter()
        self.r.xadd(f"ws:{self.ws}:events", {"e": json.dumps(payload)}, maxlen=100000, approximate=True)
        for who in data.get("notify", []):
            self.r.publish(f"ws:{self.ws}:notify:{who}", json.dumps(payload))
        self.lat["event+notify"].append((time.perf_counter() - t0) * 1000)

    def events(self, count: int = 50) -> list[dict]:
        return [json.loads(f.get("e") or f.get(b"e")) for _, f in self.r.xrevrange(f"ws:{self.ws}:events", count=count)]

    def snapshot(self, snap_id: str) -> str:
        name = f"ws:{self.ws}:snap:{snap_id}"
        t0 = time.perf_counter()
        self.r.execute_command("GRAPH.COPY", self.g.name, name)
        self.lat["graph.copy"].append((time.perf_counter() - t0) * 1000)
        return name
