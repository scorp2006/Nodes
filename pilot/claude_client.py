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
from pathlib import Path

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
    def __init__(self, model: str, mcp_config: str | None = None, allowed_tools: str = "",
                 timeout: int = 300, retries: int = 2):
        self.model_name = model
        self.mcp_config = mcp_config
        self.allowed_tools = allowed_tools
        self.timeout = timeout
        self.retries = retries
        self.calls: list[dict] = []
        self._lock = threading.Lock()
        SANDBOX.mkdir(parents=True, exist_ok=True)

    def _run(self, messages, schema=None) -> dict:
        system, prompt = flatten(messages)
        cmd = ["claude", "-p", "--setting-sources", "", "--disable-slash-commands",
               "--system-prompt", system, "--tools", "", "--strict-mcp-config",
               "--no-session-persistence", "--output-format", "json", "--model", self.model_name]
        if self.mcp_config:
            cmd += ["--mcp-config", self.mcp_config, "--allowedTools", self.allowed_tools]
        if schema:
            cmd += ["--json-schema", json.dumps(schema)]
        last_err = ""
        for _ in range(self.retries + 1):
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
                })
            return out
        raise RuntimeError(f"claude -p failed after {self.retries + 1} attempts: {last_err}")

    def chat(self, messages):
        return self._run(messages)["result"]

    def chat_json(self, messages, schema):
        out = self._run(messages, schema)
        return out.get("structured_output") or json.loads(out["result"])
