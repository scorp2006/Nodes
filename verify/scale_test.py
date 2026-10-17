"""Scale test, no LLM: the CPython standard library as the world graph, plus many concurrent agents.

Runs against its own FalkorDB (same image digest, port 6390), so a running batch on 6379 is untouched.
  1. Parse the stdlib with coop/world_graph.py's parser (files that don't parse as UTF-8 Python are skipped
     and counted), load it, report nodes, edges, load time and graph memory.
  2. check_conflicts: the CONFLICTS query from world_graph.py with CALLS*0..d for d = 1..4, two agents with 5
     random functions each, 100 random trials per depth: p50 / p99 latency.
  3. Concurrency: the real tool server (coop_tools.build_server, graph mode) on port 8790. N = 10/50/100 agents,
     each with its own MCP session, does 10 rounds of claim(5 random functions from a contested pool of 300)
     + coordination_status(). Reports p50/p99 per tool, throughput, and proves zero double-claims three ways:
     (a) no function is reported as won by two agents, (b) every won function's claimed_by is its winner,
     (c) no function's version went above 1 (a successful claim bumps it, so >1 means it was claimed twice).

  .venv-cb/Scripts/python verify/scale_test.py > results/verify/scale_test.txt
"""
import asyncio
import random
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import ast
import sysconfig
from pathlib import Path

from falkordb import FalkorDB

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "coop"))
import world_graph as wg  # noqa: E402
import coop_tools as ct  # noqa: E402

PORT_DB, PORT_SRV, GRAPH = 6390, 8790, "scale_stdlib"
DB = FalkorDB(host="localhost", port=PORT_DB)
wg.FalkorDB = lambda **kw: DB  # world_graph.load() hard-codes 6379; point it at the scale instance


def pct(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(p / 100 * (len(xs) - 1))))]


def ms(xs):
    return f"p50 {pct(xs, 50) * 1000:7.1f} ms   p99 {pct(xs, 99) * 1000:7.1f} ms   (n={len(xs)})"


def stdlib_copy() -> tuple[Path, int, int]:
    """Copy the parseable .py files of the stdlib (minus site-packages) into a temp tree."""
    src = Path(sysconfig.get_paths()["stdlib"])
    dst = Path(tempfile.mkdtemp()) / "Lib"
    kept = skipped = 0
    for p in src.rglob("*.py"):
        if "site-packages" in p.parts:
            continue
        try:
            ast.parse(p.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError, ValueError):
            skipped += 1
            continue
        out = dst / p.relative_to(src)
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(p, out)
        kept += 1
    return dst, kept, skipped


def build():
    root, kept, skipped = stdlib_copy()
    t0 = time.perf_counter()
    funcs, calls, stats = wg.parse_repo(root)
    t1 = time.perf_counter()
    g = wg.load(GRAPH, funcs, calls)
    t2 = time.perf_counter()
    shutil.rmtree(root.parent, ignore_errors=True)
    nodes = g.ro_query("MATCH (n) RETURN count(n)").result_set[0][0]
    edges = g.ro_query("MATCH ()-[r]->() RETURN count(r)").result_set[0][0]
    print(f"stdlib: {sysconfig.get_paths()['stdlib']}  (Python {sys.version.split()[0]})")
    print(f"files parsed {kept}, skipped (not UTF-8 Python) {skipped}")
    print(f"parser stats: {stats}")
    print(f"graph: {nodes} nodes, {edges} edges")
    print(f"parse {t1 - t0:.1f} s, load into FalkorDB {t2 - t1:.1f} s")
    try:
        mem = DB.connection.execute_command("GRAPH.MEMORY", "USAGE", GRAPH)
        print(f"GRAPH.MEMORY USAGE: {mem}")
    except Exception as e:  # noqa: BLE001
        print(f"GRAPH.MEMORY USAGE unavailable ({e}); used_memory: {DB.connection.info('memory')['used_memory_human']}")
    return g, [f["qualname"] for f in funcs]


def conflicts(g, names):
    rng = random.Random(1)
    print("\ncheck_conflicts (2 agents x 5 random functions, 100 trials per depth):")
    for d in (1, 2, 3, 4):
        q = wg.CONFLICTS.replace("CALLS*0..3", f"CALLS*0..{d}")
        times, hits = [], 0
        for _ in range(100):
            g.query("MATCH (s:Step) DETACH DELETE s")
            ct.set_plan(g, "a1", rng.sample(names, 5))
            ct.set_plan(g, "a2", rng.sample(names, 5))
            t0 = time.perf_counter()
            rows = g.ro_query(q, {"me": "a1"}).result_set
            times.append(time.perf_counter() - t0)
            hits += bool(rows)
        print(f"  depth {d}: {ms(times)}   trials with an overlap: {hits}/100")
    g.query("MATCH (s:Step) DETACH DELETE s")


SERVER = f"""
import sys; sys.path.insert(0, r"{HERE.parent / 'coop'}")
from falkordb import FalkorDB
import coop_tools as ct
ct.build_server(FalkorDB(host="localhost", port={PORT_DB}), "graph").run("streamable-http", host="127.0.0.1", port={PORT_SRV})
"""


async def agent(i, pool, rounds, lat, won):
    from mcp import ClientSession
    from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

    rng = random.Random(1000 + i)
    me = f"agent{i}"
    async with create_mcp_http_client(headers={"X-World-Graph": GRAPH, "X-Agent": me}) as http:
        async with streamable_http_client(f"http://127.0.0.1:{PORT_SRV}/mcp", http_client=http) as (r, w, *_):
            async with ClientSession(r, w) as s:
                await s.initialize()
                for _ in range(rounds):
                    t0 = time.perf_counter()
                    res = await s.call_tool("claim", {"functions": rng.sample(pool, 5)})
                    lat["claim"].append(time.perf_counter() - t0)
                    first = res.content[0].text.split("\n")[0].removeprefix("You own: ").removesuffix(".")
                    if first != "nothing new":
                        won.setdefault(me, set()).update(first.split(", "))
                    t0 = time.perf_counter()
                    await s.call_tool("coordination_status", {})
                    lat["coordination_status"].append(time.perf_counter() - t0)


def concurrency(g, names):
    rng = random.Random(2)
    pool = rng.sample(names, 300)
    proc = subprocess.Popen([sys.executable, "-c", SERVER], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        time.sleep(5)
        print("\nconcurrency (real tool server, one MCP session per agent, 10 rounds of claim + coordination_status):")
        for n in (10, 50, 100):
            g.query("MATCH (f:Function) SET f.claimed_by = NULL, f.version = 0")
            g.query("MATCH (a:Agent) DETACH DELETE a")
            lat, won = {"claim": [], "coordination_status": []}, {}

            async def run():
                await asyncio.gather(*(agent(i, pool, 10, lat, won) for i in range(n)))

            t0 = time.perf_counter()
            asyncio.run(run())
            wall = time.perf_counter() - t0
            calls = len(lat["claim"]) + len(lat["coordination_status"])
            # (a) disjoint winners, (b) DB agrees, (c) nothing claimed twice
            seen, dup = {}, 0
            for a, fs in won.items():
                for f in fs:
                    dup += f in seen and seen[f] != a
                    seen[f] = a
            owner = dict(g.ro_query("UNWIND $p AS q MATCH (f:Function {qualname: q}) RETURN q, f.claimed_by",
                                    {"p": pool}).result_set)
            mismatch = sum(owner.get(f) != a for f, a in seen.items())
            twice = g.ro_query("MATCH (f:Function) WHERE f.version > 1 RETURN count(f)").result_set[0][0]
            claimed = sum(v is not None for v in owner.values())
            print(f"  N={n:3}: {calls} calls in {wall:.1f} s = {calls / wall:.0f} calls/s")
            for tool, xs in lat.items():
                print(f"         {tool:20} {ms(xs)}")
            print(f"         pool functions claimed {claimed}/300; reported won by 2 agents: {dup}; "
                  f"DB owner != reported winner: {mismatch}; version > 1: {twice}")
            assert dup == 0 and mismatch == 0 and twice == 0
    finally:
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True)


if __name__ == "__main__":
    g, names = build()
    conflicts(g, names)
    concurrency(g, names)
