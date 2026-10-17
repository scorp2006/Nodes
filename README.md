# Nodes

A shared FalkorDB graph that a team of AI agents coordinates through. Agents connect over MCP and:
- post facts to a board that **checks each fact against its source**;
- **claim** the functions they will change, with an atomic compare-and-set;
- see who owns what and where their plans meet in the call graph.

A live web view shows all of it as it happens.

This README is a draft. Every number below links to the script and output that produced it. Numbers from the
development split are labelled **dev**, and everything else is a pre-registered held-out test unless marked
otherwise.

## Results

### 1. Hidden-profile decisions (HiddenBench): the board helps. Pre-registered, held-out.
Four agents each hold private facts. The group picks the right answer only if those facts get shared.
The set-up:
- 40 held-out tasks, Claude Haiku, same seeds in every condition.
- Five conditions:
  - baseline chat;
  - a "share your facts" nudge;
  - **placebo**: same tool count, nothing shared;
  - the board without source checks;
  - **the source-checked board**.
- Primary comparison: board vs placebo, exact sign test.

| condition | group right |
|---|---|
| baseline | 6/40 (15%) |
| nudge | 6/40 (15%) |
| placebo | 5/40 (12%) |
| board without source checks | 5/40 (12%) |
| **source-checked board** | **15/40 (38%)** |

Paired comparisons:
- **Primary, board vs placebo:** better on 11 tasks, worse on 1, p = 0.0063.
- **Secondaries, Holm-adjusted:** vs baseline 0.0156, vs nudge 0.0234, vs the unchecked board 0.0190.
- **The unchecked board does nothing** (5/40). The source check is the active ingredient, not the extra tool.

Sources: pre-registration `results/preregistration_test1.md`; result `results/test1_primary.txt` (on `main`).
Independent recompute: `verify/recompute_hiddenbench.py` → `results/verify/hiddenbench_recompute.txt` (see also
`results/verify/erratum_holm.md`).

How much of the hidden-profile gap does it close? This is a reference measure, not confirmatory
(`verify/gap_closed.py`):
- With every fact given to every agent, individual accuracy is 93.8%.
- After discussion: baseline 23.5%, placebo 22.3%, **board 44.4%**.
- So the board closes **30%** of the gap; placebo closes −2%.

Checks:
- **Leakage audit** (`verify/leakage_audit.py`): 135 boards, 0 invariant violations. 2,490 transcript
  combinations, 0 leaks of a private fact that its holder never shared.
- **Case studies** (`results/verify/case_studies.md`): three wins and two losses, read from the transcripts.
  - Wins: the board stops drift caused by unverifiable claims, and it surfaces the facts that rule options out.
  - Losses: agents discount facts that aren't in their own briefing, and a credible fact can cut against the
    right answer.
- **Cost and latency** (`verify/cost_table.py` → `results/verify/cost_table_test1.txt`):
  - The board uses about 2× the output tokens of baseline (58k vs 28k per run).
  - Because it is right more often, it is the **cheapest per correct group decision**: $0.32, vs $0.56 for
    baseline and $0.80 for placebo, at Haiku list prices.
  - It is slower: median wall time is 1,035 s per task, vs 595 s for baseline.

**dev** (20 tasks, used to build the board): board 13/20 vs baseline 2/20 (`results/dev1_summary.txt`).

### 2. Two coding agents on one repo (CooperBench): pending
A pre-registered blind batch is running:
- design: `results/preregistration_cb1b.md`;
- 135 runs, Claude Sonnet, light Python repos;
- conditions: graph-coordinated vs the same procedure over chat vs plain coop.

Results will be added here after the single pre-registered analysis.

A note on comparing with published conflict rates: the published "no conflicts" figures used an older merge
policy with a union fallback. Under the current evaluator, the same published patches conflict (5/5;
`coop/merge_probe.py`).

### 3. Can the call graph predict merge trouble before it happens? No, beyond "same function". Pre-registered.
The test used the published patches from 5 held-out tasks: 84 eligible pairs, 45 of them interaction failures
(`results/detector2_report.md`). On the primary subset, where the two agents edit different functions (n = 34,
4 failures), no predictor beats chance:
- same file: AUC 0.50;
- call distance: AUC 0.42;
- call + shared-state distance: AUC 0.40.

Over all eligible pairs, call distance scores AUC 0.83. But "both edited the same function" alone scores 0.84.
**So multi-hop paths add nothing here.** Nodes therefore relies on same-function claims and a hand-off, and shows
multi-hop paths only as "possible overlap".

### 4. Scale: no LLM involved (`verify/scale_test.py`)
The CPython 3.12 standard library as the code graph:
- 15,713 nodes and 24,617 edges; parse 8 s, load 1 s, 5 MB of graph memory.
- The conflict query takes p50 2–4 ms and p99 ≤ 32 ms at depths 1–4.

With 10, 50 or 100 agents in concurrent MCP sessions, each doing claim + status against a contested pool of 300
functions:
- **zero double-claims**, checked three ways at every level;
- the hardened server handles **125 calls/s at 100 agents**, against 44 for the study server
  (`results/verify/scale_test_nodes.txt`, `results/verify/hardening.md`).

## Graph model
```
(:File)-[:CONTAINS]->(:Function)-[:CALLS]->(:Function)            code, parsed with Python's ast
(:Agent)-[:PLANS]->(:Step)-[:TOUCHES]->(:Function)                 what each agent intends to change
(:Function {claimed_by, version})                                  ownership
(:Agent)-[:POSTED {seq}]->(:Fact {text, source, checked})          the board
```

The claim is an atomic compare-and-set. FalkorDB runs writes one at a time, so the first agent wins:
```cypher
UNWIND $found AS q
MATCH (f:Function {qualname: q})
WITH f, f.claimed_by IS NULL AS free
SET f.claimed_by = CASE WHEN free THEN $agent ELSE f.claimed_by END,
    f.version    = CASE WHEN free THEN f.version + 1 ELSE f.version END
RETURN f.qualname, f.claimed_by
```

Where two plans meet within 3 calls:
```cypher
MATCH (me:Agent {id: $me})-[:PLANS]->(:Step)-[:TOUCHES]->(f:Function)
MATCH p = (f)-[:CALLS*0..3]-(g:Function)<-[:TOUCHES]-(:Step)<-[:PLANS]-(other:Agent)
WHERE other <> me
RETURN other.id, f.qualname, g.qualname, length(p) - 2 AS hops
ORDER BY hops
```

The HiddenBench board (`pilot/graph_tools.py`) posts a fact only along an edge the agent already has:
`(:Agent)-[:KNOWS]->(:Fact)`. An agent can't post a fact it doesn't hold, and the reply says only
"source-checked: yes/no".

## Run it
Requirements: Docker and Python 3.12.
```bash
pip install -r requirements.txt
python -m nodes up --workspace path/to/repo --package path/to/repo/src/yourpkg
```
This starts FalkorDB in Docker (a pinned image digest) if nothing is answering on port 6379, loads the code
graph, and serves:
- MCP at `http://127.0.0.1:8780/mcp`;
- the live view at `http://127.0.0.1:8781/`;
- replays at `http://127.0.0.1:8781/replay?task=t42_d0`.

Connect an agent, for example Claude Code:
```bash
claude mcp add --transport http nodes http://127.0.0.1:8780/mcp --header "X-Agent: alice"
```
Tools:
- `declare_plan`, `claim`, `release`, `mark_done`, `status`, `impact_of`;
- `share_fact(text, source)` and `board` for the board.

To see it without an LLM, run `python -m nodes.demo` (two scripted agents on pallets/click).
Tests: `python -m pytest nodes/test_nodes.py`.

## Limitations
- **Identity is trusted.** `X-Agent` isn't authenticated, so this is for one trusted team on localhost.
- **Claims don't expire.** They last until released; there's no lease TTL.
- **The call graph is static.** About 29–37% of call sites resolve (dev). Multi-hop overlap did not predict
  failures (result 3).
- **The HiddenBench result is narrow.** One model (Haiku) and one benchmark. The board costs about 2× the
  output tokens. Two of five case studies are losses.
- **The CooperBench result is not in yet.**

## Credits and AI use
- HiddenBench (MIT) and CooperBench (MIT) are used as benchmarks, unmodified; they are cloned locally into `external/` and not redistributed here.
- FalkorDB is the graph database.
- **AI use:**
  - The code, experiments and this draft were written with Claude Code (Claude Opus) acting as a coding agent,
    under the author's direction.
  - The agents inside the experiments are Claude Haiku (HiddenBench) and Claude Sonnet (CooperBench), run
    through `claude -p`.
  - Pre-registrations were committed before each confirmatory run.
  - All numbers come from committed scripts and their outputs.
