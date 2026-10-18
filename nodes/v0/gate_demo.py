"""Scripted action-gate demo (no LLM): three agents on pallets/click hit all four decisions.
Watch the live view at http://127.0.0.1:8781/ while it runs.

  python -m nodes up --workspace <click checkout> --package <click checkout>/src/click     (terminal 1)
  python -m nodes.gate_demo [--port 8780] [--pause 3]                                       (terminal 2)
"""
import argparse
import asyncio
import json

from .demo import call

EDIT, EDIT_FILE, GET_EDITOR = "click.termui.edit", "click._termui_impl.Editor.edit_file", "click._termui_impl.Editor.get_editor"
SRC = "src/click/termui.py"

STEPS = [
    ("alice", "before_acting", {"functions": [EDIT], "description": "return the text even when unchanged",
                                "evidence": [{"text": "If the editor is closed without changes, `None` is returned.",
                                              "source": SRC}]}),                         # proceed
    ("bob", "before_acting", {"functions": [EDIT_FILE], "description": "support editors with arguments"}),  # proceed
    ("bob", "before_acting", {"functions": [EDIT, GET_EDITOR], "description": "add an editor fallback"}),   # defer
    ("alice", "before_acting", {"functions": [EDIT_FILE], "description": "pass the env through"}),          # escalate: deadlock
    ("carol", "before_acting", {"functions": [GET_EDITOR], "description": "drop the editor lookup",
                                "evidence": [{"text": "edit() never launches an editor", "source": SRC}]}),  # escalate: provenance
    ("alice", "mark_done", {"commit": "a1b2c3d"}),
    ("bob", "before_acting", {"functions": [EDIT, GET_EDITOR], "description": "add an editor fallback"}),   # hand_off
]


async def main(a):
    for agent, tool, args in STEPS:
        text = await call(a.port, agent, tool, args, quiet=True)
        if tool == "before_acting":
            r = json.loads(text)
            print(f"\n{agent}: {args['description']}\n  -> {r['decision'].upper()}: {r['reason']}\n"
                  f"     path: {' '.join(r['explanation'])}")
        else:
            print(f"\n{agent}: {tool}({args})")
        await asyncio.sleep(a.pause)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8780)
    ap.add_argument("--pause", type=float, default=3.0)
    asyncio.run(main(ap.parse_args()))
