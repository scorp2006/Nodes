# Open two Claude Code sessions (alice, bob), each in its own worktree (the hooks identify them, no token), plus the dashboard
# and a live event log. Run demo/setup_demo.py first.
#   powershell -File demo/start_agents.ps1 [-Dir $HOME\nodes-demo] [-Port 8780]
param([string]$Dir = "$HOME\nodes-demo", [int]$Port = 8780)
$ErrorActionPreference = "Stop"
$agents = Get-Content (Join-Path $Dir "agents.json") -Raw | ConvertFrom-Json
$nodesRepo = Split-Path -Parent $PSScriptRoot
foreach ($name in "alice", "bob") {
    $a = $agents.$name
    $script = Join-Path $Dir "start_$name.ps1"
    # The prompt goes through a file so quoting can't break it.
    $promptFile = Join-Path $Dir "prompt_$name.txt"
    Set-Content -Path $promptFile -Value $a.prompt -Encoding utf8
    @"
Set-Location '$($a.worktree)'
`$Host.UI.RawUI.WindowTitle = 'Nodes demo: $name'
claude (Get-Content '$promptFile' -Raw)
"@ | Set-Content -Path $script -Encoding utf8
    Start-Process powershell -ArgumentList "-NoExit", "-ExecutionPolicy", "Bypass", "-File", $script
}
$py = $agents._nodes_python
Start-Process powershell -ArgumentList "-NoExit", "-Command", "Set-Location '$nodesRepo'; & '$py' -m nodes watch --workspace shop"
Start-Process "http://127.0.0.1:$Port/?ws=shop"
"Started alice and bob. Approve the Nodes MCP server and the edits when Claude Code asks."
