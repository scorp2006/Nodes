# The 4 real breakages among the light-repo disjoint pairs (ILLUSTRATIONS, not evidence)

These are the four interaction failures in the detector v2 primary subset (`results/detector2_rows.json`):
eligible held-out pairs whose two patches edit different functions. Each patch passes its own tests alone; the two
together break. The changed lines come from `coop/detector_eval.changed_lines` on the published Claude k1 patches.
The paths come from the frozen v2 graph (`coop/world_graph_v2.py`) loaded for each task. Script:
`verify/breakage_cases.py`; raw output: `results/verify/breakage_cases_raw.txt`. Every number here is read from
that output.

| pair | how it broke | shared line(s) in the base file | what is at that line | CALLS path | CALLS + SHARES_STATE path |
|---|---|---|---|---|---|
| jinja 1559, features 2 + 7 | merge conflict | `ext.py` 244 | blank line just above `class InternationalizationExtension` | none | 2 hops |
| jinja 1559, features 5 + 7 | merge conflict | `ext.py` 572 | right after a method's final `return` | none | 2 hops |
| jinja 1559, features 8 + 9 | merge conflict | `ext.py` 638 and 854 | above `def extract_from_ast`, and the module's alias list (`debug = DebugExtension`) | none (no function edited) | none |
| jinja 1621, features 3 + 6 | merge conflict | `loaders.py` 23 | the import block | none (feature 3 edits no function) | none |

The 2-hop paths in the first two cases both run through `Extension.__init__` (a SHARES_STATE edge, then a CALLS
edge). In 2 + 7 the path starts at `_install_callables`; in 5 + 7 it starts at `parse`.

## What these show
1. **All four are textual merge conflicts, not semantic breakages.** In no case did the code merge and then fail
   its tests.
2. **The conflicts sit between functions, not inside them.** In each case both agents inserted new code at the
   same spot: above a class, after a method, in the import block, or in the module's alias list. A
   function-level call graph has nothing to look at there.
3. **Did the graph show it?**
   - **Call graph: no.** There was no CALLS path in any case.
   - **Shared state: two cases, but by coincidence.** It found 2-hop paths in two cases, but those paths connect
     functions the agents edited elsewhere in the file, not the line that conflicted. Showing that path would
     have been the right warning for the wrong reason.
4. **The disjoint-function subset includes pairs that edit no function at all** (1559 8+9, and one side of
   1621 3+6), so "disjoint" is sometimes trivially true. That is a limit of defining disjointness by functions,
   in v2 and in v3.
5. **What would have caught all four** is a check at the level of lines or insertion points in a shared file.
   For example, treat module-level and class-level regions (imports, alias lists, the space between
   definitions) as claimable units, alongside functions. **This is a hypothesis drawn from four examples.**
   It was not tested and is not part of any pre-registered analysis.
