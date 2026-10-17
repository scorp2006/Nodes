"""HiddenBench ModelClient that runs each turn through a fresh `claude -p` call.

Every call is stateless: the agent's whole history is sent each time, the same
way HiddenBench's own API clients work. Calls run from an empty folder with all
user settings, hooks, skills and MCP servers switched off, so the only thing an
agent sees is what HiddenBench gives it (plus our tools, when we pass them).
"""
from __future__ import annotations

import json
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable

SANDBOX = Path(__file__).resolve().parent.parent / "runs" / "agent_cwd"


def flatten(messages: list[dict[str, str]]) -> tuple[str, str]:
    """Split HiddenBench messages into (system prompt, single prompt text)."""
    system = messages[0]["content"] if messages and messages[0]["role"] == "system" else ""
    turns = [m for m in messages if m["role"] != "system"]
    if len(turns) == 1:
        return system, turns[0]["content"]
    earlier = "\n\n".join(
        f"{'You' if m['role'] == 'assistant' else 'Moderator'}: {m['content']}" for m in turns[:-1]
    )
    return system, f"[Conversation so far]\n{earlier}\n\n[Now]\n{turns[-1]['content']}"


class ClaudeCLIClient:
    def __init__(self, model: str, allowed_tools: str = "", timeout: int = 300, retries: int = 3):
        self.model_name = model
        self.allowed_tools = allowed_tools
        # HiddenBench only hands us messages, so an agent is identified by its
        # system prompt (unique per agent: it holds that agent's facts).
        self.mcp_for_system: dict[str, str] = {}
        # Optional pushed digest per agent: called on every turn, appended to the prompt,
        # so agents see the board without spending a tool round trip to read it.
        self.digest_for_system: dict[str, Callable[[], str]] = {}
        self.timeout = timeout
        self.retries = retries
        self.calls: list[dict] = []
        self._lock = threading.Lock()
        SANDBOX.mkdir(parents=True, exist_ok=True)

    def _run(self, messages, schema=None) -> dict:
        system, prompt = flatten(messages)
        if system in self.digest_for_system:
            prompt += "\n\n[Board right now]\n" + self.digest_for_system[system]()
        cmd = ["claude", "-p", "--setting-sources", "", "--disable-slash-commands",
               "--system-prompt", system, "--tools", "", "--strict-mcp-config",
               "--no-session-persistence", "--output-format", "json", "--model", self.model_name]
        mcp_config = self.mcp_for_system.get(system)
        if mcp_config:
            cmd += ["--mcp-config", mcp_config, "--allowedTools", self.allowed_tools]
        if schema:
            cmd += ["--json-schema", json.dumps(schema)]
        last_err = ""
        for attempt in range(self.retries + 1):
            if attempt:
                time.sleep(30 * attempt)  # back off: failures here are usually rate limits
            try:
                # prompt goes over stdin: long transcripts would overflow the Windows command line
                proc = subprocess.run(cmd, input=prompt, cwd=SANDBOX, capture_output=True, text=True,
                                      encoding="utf-8", timeout=self.timeout)
                out = json.loads(proc.stdout)
            except (subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
                last_err = repr(exc)
                continue
            if out.get("is_error") or out.get("subtype") != "success":
                last_err = str(out.get("result") or out.get("subtype"))
                continue
            with self._lock:
                self.calls.append({
                    "usage": out.get("usage", {}),
                    "cost_usd": out.get("total_cost_usd", 0.0),
                    "duration_ms": out.get("duration_ms"),
                    "num_turns": out.get("num_turns"),
                    "tools_attached": mcp_config is not None,
                })
            return out
        raise RuntimeError(f"claude -p failed after {self.retries + 1} attempts: {last_err}")

    def chat(self, messages):
        return self._run(messages)["result"]

    def chat_json(self, messages, schema):
        out = self._run(messages, schema)
        return out.get("structured_output") or json.loads(out["result"])
