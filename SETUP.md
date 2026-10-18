# Setup

Tested on Windows 11 from a fresh clone in a new folder and a new venv (Python 3.11.9, Docker Desktop, Claude Code CLI).
Python 3.11 or 3.12 both work.

## Once per machine

```bash
git clone <this repo> nodes && cd nodes
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt     # macOS/Linux: .venv/bin/python
.venv/Scripts/python -m nodes up
```

`nodes up` starts FalkorDB in Docker (pinned image digest, container `nodes-falkordb`, port 6380) if nothing answers,
then serves MCP, the Claude Code hooks and the dashboard on `http://127.0.0.1:8780`. Leave it running.

## Once per repo

In a second terminal:

```bash
.venv/Scripts/python -m nodes init --repo C:/path/to/your/repo
```

This indexes the code (14 languages) and the git history, writes `.mcp.json` and `.claude/settings.json` (MCP server
and hooks) into the repo, and mints agent tokens into `.nodes/tokens.json` for clients without hooks. These three
paths are added to `.git/info/exclude`, so nothing is committed. Run it again any time: it is incremental.

Then open Claude Code in the repo **interactively once** and:

1. accept the folder trust dialog (until then Claude Code ignores the repo's `permissions.allow`), and
2. approve the `nodes` MCP server when asked (project MCP servers need a one-time approval; until then the
   Nodes tools are not loaded, though the hooks still gate edits).

After that, every Claude Code session and subagent in that repo is tracked with no login or token step.
Open `http://127.0.0.1:8780` to watch.

Options: `--scope user` writes the hooks to `~/.claude/settings.json` and registers the server with
`claude mcp add --scope user` instead (repos that were never `init`-ed are ignored). `--fresh` wipes the
workspace graph first. `--no-git` skips the history import.

## Replay a recorded run (no agents, no LLM)

```bash
.venv/Scripts/python -m nodes replay --workspace NAME --speed 3
```

Open `http://127.0.0.1:8780/?ws=NAME-replay`.

## Past session rationale (opt-in, local only)

```bash
.venv/Scripts/python -m nodes import-sessions --workspace NAME
```

## LAN mode

```bash
NODES_SERVER_TOKEN=<24+ random chars> .venv/Scripts/python -m nodes up --host 0.0.0.0
.venv/Scripts/python -m nodes init --repo ... --lan
```

Every endpoint, including the dashboard, then requires the token (`Authorization: Bearer ...`, or open the
dashboard once with `?token=...` to set a cookie). Clients read it from the `NODES_SERVER_TOKEN` environment variable.

## Headless runs

`claude -p` in an untrusted folder does not load the repo's `.mcp.json`. Pass it explicitly, with the prompt on
stdin (`--mcp-config` takes several values and would swallow a prompt argument):

```bash
echo "your prompt" | claude -p --mcp-config .mcp.json --allowedTools "Read,Edit,Write,mcp__nodes__*"
```
