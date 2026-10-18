---
description: Run several tasks at once, one subagent per task, coordinated through Nodes
argument-hint: "<task 1>; <task 2>; <task 3>"
---

Split these tasks on `;` and run them in parallel: $ARGUMENTS

Dispatch exactly one subagent per task, all in a single message (one Agent tool call per task), so they run at the
same time. Tell each subagent its task in full, plus: "First call the Nodes `declare_intent` tool with the units you
will change. Every edit is gated by Nodes: on DEFER do other parts or call `status(wait_seconds=60)`; on HANDOFF merge
the named commit first; on 'possible overlap (advisory)' call `before_acting` again with `acknowledge='<why>'` if your
change can't affect theirs, otherwise coordinate. When committed, call `coordinate(op='done', text='<sha>')`."

When all of them finish, report one line per task: done or not, and the commit.
