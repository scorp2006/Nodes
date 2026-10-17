# One CooperBench pair, coop mode, local Docker, claude_code adapter.
# Asks for the CLAUDE_CODE_OAUTH_TOKEN with hidden input; the token lives only in this
# PowerShell process (never a file, never shell history, never the screen).
#   & .\coop\smoke_coop.ps1 -Name smoke-coop            # unmodified CooperBench
#   & .\coop\smoke_coop.ps1 -Name smoke-graph -Graph     # + our code-graph tools (coop/run_cb_graph.py)
#   add -Remember once to save the token encrypted for your Windows account, so later runs don't ask
param(
    [string]$Name = "smoke-coop",
    [string]$Repo = "pallets_click_task",
    [string]$Task = "2068",
    [string]$Features = "1,2",
    [string]$Model = "anthropic/claude-haiku-5-5",
    [switch]$Graph,
    [switch]$Remember,
    [string]$RepoDir = "external\click-pr-2068",
    [string]$Src = "src\click"
)
$ErrorActionPreference = "Stop"
$env:Path = "C:\Program Files\Docker\Docker\resources\bin;" + $env:Path
$root = Split-Path -Parent $PSScriptRoot

function Read-Secret([System.Security.SecureString]$s) {
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($s)
    try { [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr) } finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
}
# Optional saved token: encrypted with Windows DPAPI, so only this Windows user on this machine can read it.
# Lives outside the repo. Delete it with:  Remove-Item "$env:LOCALAPPDATA\nodes-bench\cc_token.dpapi"
$tokenFile = Join-Path $env:LOCALAPPDATA "nodes-bench\cc_token.dpapi"

if (-not $env:CLAUDE_CODE_OAUTH_TOKEN -and (Test-Path $tokenFile)) {
    $env:CLAUDE_CODE_OAUTH_TOKEN = Read-Secret (Get-Content $tokenFile | ConvertTo-SecureString)
    Write-Host "Using the saved token (encrypted for your Windows account)."
}
if (-not $env:CLAUDE_CODE_OAUTH_TOKEN) {
    $secure = Read-Host -AsSecureString "Paste the token printed by 'claude setup-token' (input is hidden)"
    # setup-token wraps the token across lines in narrow windows; tokens never contain whitespace.
    $env:CLAUDE_CODE_OAUTH_TOKEN = (Read-Secret $secure) -replace "\s", ""
    if ($Remember) {
        New-Item -ItemType Directory -Force (Split-Path $tokenFile) | Out-Null
        ConvertTo-SecureString $env:CLAUDE_CODE_OAUTH_TOKEN -AsPlainText -Force | ConvertFrom-SecureString | Set-Content $tokenFile
        Write-Host "Token saved, encrypted for your Windows account. Next runs won't ask."
    }
}
# Without a token the adapter would fall back to reading ~/.claude/.credentials.json. Refuse instead.
if (-not $env:CLAUDE_CODE_OAUTH_TOKEN -or $env:CLAUDE_CODE_OAUTH_TOKEN.Length -lt 20) { throw "No token given; stopping." }
Remove-Item Env:ANTHROPIC_API_KEY -ErrorAction SilentlyContinue

$logDir = Join-Path $root "runs\cooperbench"
New-Item -ItemType Directory -Force $logDir | Out-Null
$statsFile = Join-Path $logDir "$Name.docker_stats.txt"
Remove-Item $statsFile -ErrorAction SilentlyContinue

# Sample agent-container memory every 2 s while the run is going (peak RAM is taken from this file).
$sampler = Start-Job -ArgumentList $statsFile -ScriptBlock {
    param($out)
    $env:Path = "C:\Program Files\Docker\Docker\resources\bin;" + $env:Path
    while ($true) {
        $ids = docker ps -q --filter "name=minisweagent" 2>$null
        if ($ids) { docker stats --no-stream --format "{{.Name}}`t{{.MemUsage}}" $ids 2>$null | Out-File -Append -Encoding utf8 $out }
        Start-Sleep -Seconds 2
    }
}

$cbArgs = @("run", "-n", $Name, "-r", $Repo, "-t", $Task, "-f", $Features, "-m", $Model, "-a", "claude_code",
            "--setting", "coop", "--backend", "docker", "-c", "1", "--redis", "redis://localhost:6379/1",
            "--dataset-dir", (Join-Path $root "external\CooperBench\dataset"), "--log-dir", $logDir)
$start = Get-Date
try {
    if ($Graph) {
        $world = "cbw_" + ($Repo -replace "_task", "") + "_" + $Task + "_" + ($Features -replace ",", "_")
        & (Join-Path $root ".venv-cb\Scripts\python.exe") (Join-Path $root "coop\run_cb_graph.py") `
            --world-graph $world --repo-dir (Join-Path $root $RepoDir) --src $Src `
            --tool-log (Join-Path $logDir "$Name.tool_calls.jsonl") -- @cbArgs
    } else {
        & (Join-Path $root ".venv-cb\Scripts\cooperbench.exe") @cbArgs
    }
}
finally {
    Stop-Job $sampler; Remove-Job $sampler
    Remove-Item Env:CLAUDE_CODE_OAUTH_TOKEN -ErrorAction SilentlyContinue
}
"wall_seconds: {0:N0}" -f ((Get-Date) - $start).TotalSeconds | Tee-Object -FilePath (Join-Path $logDir "$Name.wall.txt")
