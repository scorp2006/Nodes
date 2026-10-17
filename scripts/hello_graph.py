"""First contact with FalkorDB: a tiny hidden-profile graph.

Three agents each hold facts. Some facts were shared in the discussion,
some were not. The graph answers: "which facts exist that the group
has never heard?" without revealing what those facts say.
"""
from falkordb import FalkorDB

db = FalkorDB(host="localhost", port=6379)
g = db.select_graph("hello_hidden_profile")
g.delete() if "hello_hidden_profile" in db.list_graphs() else None

# Write: idempotent ingest (UNWIND + MERGE), safe to re-run.
knows = [
    {"agent": "Person 1", "fact": "f1", "topic": "bridge"},
    {"agent": "Person 2", "fact": "f2", "topic": "tunnel"},
    {"agent": "Person 3", "fact": "f3", "topic": "driveway"},
    {"agent": "Person 1", "fact": "s1", "topic": "hotels"},  # a shared fact
    {"agent": "Person 2", "fact": "s1", "topic": "hotels"},
    {"agent": "Person 3", "fact": "s1", "topic": "hotels"},
]
g.query(
    """
    UNWIND $rows AS row
    MERGE (a:Agent {name: row.agent})
    MERGE (f:Fact {id: row.fact})
      ON CREATE SET f.topic = row.topic
    MERGE (a)-[:KNOWS]->(f)
    """,
    {"rows": knows},
)

# Person 2 mentioned their fact in the discussion; nobody else did.
g.query(
    """
    MATCH (a:Agent {name: 'Person 2'}), (f:Fact {id: 'f2'})
    MERGE (a)-[:SHARED {round: 1}]->(f)
    """
)

# Read: read-only query. Facts held by exactly one agent and never shared.
res = g.ro_query(
    """
    MATCH (holder:Agent)-[:KNOWS]->(f:Fact)
    WITH f, collect(holder.name) AS holders
    WHERE size(holders) = 1
    OPTIONAL MATCH (:Agent)-[s:SHARED]->(f)
    WITH f, holders, count(s) AS times_shared
    WHERE times_shared = 0
    RETURN holders[0] AS who_holds_it, f.topic AS topic
    ORDER BY who_holds_it
    """
)
print("Unshared private facts (topic only, content stays private):")
for who, topic in res.result_set:
    print(f"  {who} holds an unshared fact about: {topic}")

counts = g.ro_query("MATCH (n) RETURN labels(n)[0] AS label, count(n) ORDER BY label").result_set
print("Graph contents:", counts)

assert sorted(r[0] for r in res.result_set) == ["Person 1", "Person 3"], res.result_set
print("check passed")
