"""Redact secrets before anything is written to a trace, log or the graph (tokens never leave memory)."""
from __future__ import annotations

import re

SECRET_KEYS = re.compile(r"^(authorization|(.*[_-])?token|api[_-]?key|.*secret.*|password|cookie|x-api-key)$", re.I)
PATTERNS = [
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"nds_[A-Za-z0-9_-]{8,}"),                    # Nodes bearer tokens
    re.compile(r"sk-(?:ant-)?[A-Za-z0-9_-]{16,}"),           # Anthropic / OpenAI style keys
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),               # GitHub tokens
    re.compile(r"AKIA[0-9A-Z]{16}"),                         # AWS access key ids
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
]


def text(s: str) -> str:
    for p in PATTERNS:
        s = p.sub("[REDACTED]", s)
    return s


def value(v, key: str = ""):
    """Recursively redact a JSON-like value; any field whose name looks secret is blanked entirely."""
    if key and SECRET_KEYS.search(key) and not isinstance(v, (dict, list)):
        return "[REDACTED]" if v not in (None, "", 0) else v
    if isinstance(v, str):
        return text(v)
    if isinstance(v, dict):
        return {k: value(x, str(k)) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [value(x) for x in v]
    return v


if __name__ == "__main__":
    rec = {"headers": {"Authorization": "Bearer nds_abc123def456ghi"}, "cmd": "curl -H 'Authorization: Bearer xyz12345678'",
           "note": "key sk-ant-api03-ABCDEFGHIJKLMNOPQRST", "tokens_used": 412, "ok": "plain text"}
    out = value(rec)
    assert "nds_" not in str(out) and "sk-ant" not in str(out) and "xyz12345678" not in str(out), out
    assert out["ok"] == "plain text" and out["tokens_used"] == 412  # a budget counter is not a secret
    assert value({"token": "abc", "NODES_TOKEN": "x1", "budget_tokens": 9})["budget_tokens"] == 9
    print("redact self-check ok:", out)
