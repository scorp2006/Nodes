# The Nodes graph schema

One FalkorDB graph per repository (`ws:<name>`). This was read from a live workspace with `CALL db.labels()`,
`CALL db.relationshipTypes()` and `MATCH (a)-[e]->(b) RETURN labels(a)[0], type(e), labels(b)[0], count(*)`
(workspace `blindspot-live`, a real three-subagent run). The core is drawn in [img/data-model.svg](img/data-model.svg).

## Node labels

| label | what it is | key properties |
|---|---|---|
| `Unit` | a piece of code: `file`, `class`, `function` or `region` (a module-level block) | `uid`, `kind`, `path`, `symbol`, `start`, `end`, `lang`, `hash`, `lease_owner`, `lease_epoch`, `lease_expires`, `rationale` |
| `Principal` | a person or an agent identity | `uid`, `kind`, `name` |
| `Session` | one running agent or subagent | `uid`, `name`, `task`, `status`, `started_at`, `last_heartbeat`, `commit`, `done_at`, `token_hash` |
| `Intent` | a declared, planned change | `uid`, `task`, `summary`, `status`, `created_at` |
| `Decision` | one gate verdict, with its reason and path | `uid`, `verdict` (proceed, defer, handoff, escalate, acknowledged), `reason`, `action`, `at`, `snapshot`, `advisory`, `pairs` |
| `Fact` | a claim an agent posted | `uid`, `text`, `source_uid`, `status` (source-checked or unverified), `recorded_at` |
| `Source` | where a fact comes from: a file range, a commit, a test run | `uid`, `kind`, `ref` |
| `Request` | an open question one agent asks about a unit | `uid`, `text`, `status`, `at` |
| `Commitment` | a promise an agent made (fulfilled when it finishes) | `uid`, `text`, `status` |
| `Revision` | a git commit | `uid`, `sha`, `at`, `message`, `is_merge`, `files_changed` |
| `Branch` | a git branch | `uid`, `name`, `head` |
| `PullRequest` | a merged pull request | `uid`, `number` |

## Relationships

| pattern | meaning |
|---|---|
| `(Unit)-[:CONTAINS]->(Unit)` | file contains class, class contains method |
| `(Unit)-[:DEPENDS_ON]->(Unit)` | a call, resolved statically (`resolved_by`: ast-static, name-match, self-attr, inherited) |
| `(Unit)-[:IMPORTS]->(Unit)` | a module imports another |
| `(Unit)-[:CO_CHANGES]->(Unit)` | changed together in git history |
| `(Principal)-[:RUNS]->(Session)` | who runs this session |
| `(Session)-[:SPAWNED]->(Session)` | a subagent and its parent |
| `(Session)-[:DECLARED]->(Intent)-[:TOUCHES]->(Unit)` | a planned change and the code it touches |
| `(Session)-[:HELD {epoch, from, until}]->(Unit)` | a lease; the live owner is also on `Unit.lease_owner` with `lease_epoch` |
| `(Session)-[:WAITS_FOR {unit, since}]->(Session)` | a deferred agent waiting on the owner of `unit` |
| `(Session)-[:MADE]->(Decision)-[:FOR]->(Intent)` | a gate verdict for an intent |
| `(Decision)-[:CITES]->(Unit or Fact)` | what the verdict was about |
| `(Session)-[:ASSERTED]->(Fact)-[:SOURCED_FROM {quote}]->(Source)` | the provenance of a fact |
| `(Fact)-[:ABOUT]->(Unit)` | the code a fact is about |
| `(Session)-[:REQUESTED]->(Request)-[:ABOUT]->(Unit)` | a question about a unit |
| `(Session)-[:COMMITTED]->(Commitment)` | a promise |
| `(Principal)-[:AUTHORED]->(Revision)-[:CHANGED]->(Unit)` | git history, per unit |
| `(Branch)-[:POINTS_TO]->(Revision)`, `(Revision)-[:MERGED_PR]->(PullRequest)` | branches and merged PRs |
| `(Principal)-[:OWNS_CODE]->(Unit)` | code ownership from history |

Counts on `blindspot-live`: 277 Units, 260 CONTAINS, 83 DEPENDS_ON, 16 IMPORTS, 26 CO_CHANGES, 20 Revisions,
19 Facts each with a Source, 12 Decisions. Indexes: a range index on every `uid`, and a full-text index on
`Unit` and `Fact` text (`nodes/db.py`).
