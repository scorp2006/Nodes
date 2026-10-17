# One CooperBench pair under one condition, local Docker, claude_code adapter, --git always on.
#   & .\coop\run_pair.ps1 -Name dev-click-graph -Repo pallets_click_task -Task 2068 -Features 1,2 -Condition graph
# Conditions: coop | protocol | graph   (all go through coop/run_cb_graph.py, so the launcher is identical)
# Token: read from the encrypted saved token if present, else asked once with hidden input (-Remember saves it,
# encrypted for your Windows account). Never written in plain text, never shown, never in shell history.
param(
    [Parameter(Mandatory)][string]$Name,
    [string]$Repo = "pallets_click_task",
    [string]$Task = "2068",
    [string]$Features = "1,2",
    [ValidateSet("coop", "protocol", "graph")][string]$Condition = "coop",
    [string]$Model = "anthropic/claude-haiku-5-5",
    [switch]$Remember,
    [switch]$NoEval   # batch mode: skip CooperBench's per-pair auto-eval; evaluate everything at the end (no peeking)
)
$ErrorActionPreference = "Stop"
$env:Path = "C:\Program Files\Docker\Docker\resources\bin;" + $env:Path
$root = Split-Path -Parent $PSScriptRoot

function Read-Secret([System.Security.SecureString]$s) {
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($s)
    try { [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr) } finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
}
$tokenFile = Join-Path $env:LOCALAPPDATA "nodes-bench\cc_token.dpapi"
if (-not $env:CLAUDE_CODE_OAUTH_TOKEN -and (Test-Path $tokenFile)) {
    $env:CLAUDE_CODE_OAUTH_TOKEN = Read-Secret (Get-Content $tokenFile | ConvertTo-SecureString)
}
if (-not $env:CLAUDE_CODE_OAUTH_TOKEN) {
    $secure = Read-Host -AsSecureString "Paste the token printed by 'claude setup-token' (input is hidden)"
    $env:CLAUDE_CODE_OAUTH_TOKEN = (Read-Secret $secure) -replace "\s", ""
    if ($Remember) {
        New-Item -ItemType Directory -Force (Split-Path $tokenFile) | Out-Null
        ConvertTo-SecureString $env:CLAUDE_CODE_OAUTH_TOKEN -AsPlainText -Force | ConvertFrom-SecureString | Set-Content $tokenFile
    }
}
# Without a token the adapter would fall back to reading ~/.claude/.credentials.json. Refuse instead.
if (-not $env:CLAUDE_CODE_OAUTH_TOKEN -or $env:CLAUDE_CODE_OAUTH_TOKEN.Length -lt 20) { throw "No token given; stopping." }
Remove-Item Env:ANTHROPIC_API_KEY -ErrorAction SilentlyContinue

$logDir = Join-Path $root "runs\cooperbench"
New-Item -ItemType Directory -Force $logDir | Out-Null
$statsFile = Join-Path $logDir "$Name.docker_stats.txt"
Remove-Item $statsFile -ErrorAction SilentlyContinue
$sampler = Start-Job -ArgumentList $statsFile -ScriptBlock {
    param($out)
    $env:Path = "C:\Program Files\Docker\Docker\resources\bin;" + $env:Path
    while ($true) {
        $ids = docker ps -q --filter "name=minisweagent" 2>$null
        if ($ids) { docker stats --no-stream --format "{{.Name}}`t{{.MemUsage}}" $ids 2>$null | Out-File -Append -Encoding utf8 $out }
        Start-Sleep -Seconds 2
    }
}

# Pre-pull the task image: CooperBench's own `docker run` gives up after 120 s, which a 1-5 GB image needs more than.
$image = "akhatua/cooperbench-" + ($Repo -replace "_task", "" -replace "_", "-") + ":task$Task"
docker image inspect $image *> $null
if ($LASTEXITCODE -ne 0) { docker pull -q $image | Out-Null; if ($LASTEXITCODE -ne 0) { throw "could not pull $image" } }

$py = Join-Path $root ".venv-cb\Scripts\python.exe"
$launcher = @((Join-Path $root "coop\run_cb_graph.py"), "--condition", $Condition)
if ($Condition -ne "coop") {
    $prep = & (Join-Path $root "..\..\.venv\Scripts\python.exe") -I (Join-Path $root "coop\prepare_repo.py") $Repo $Task
    $repoDir, $src = $prep.Trim().Split("|")
    $world = "cbw_" + $Condition + "_" + ($Repo -replace "_task", "") + "_" + $Task + "_" + ($Features -replace ",", "_")
    $launcher += @("--world-graph", $world, "--repo-dir", $repoDir, "--src", $src,
                   "--tool-log", (Join-Path $logDir "$Name.tool_calls.jsonl"))
}
$cbArgs = @("run", "-n", $Name, "-r", $Repo, "-t", $Task, "-f", $Features, "-m", $Model, "-a", "claude_code",
            "--setting", "coop", "--git", "--backend", "docker", "-c", "1", "--redis", "redis://localhost:6379/1",
            "--dataset-dir", (Join-Path $root "external\CooperBench\dataset"), "--log-dir", $logDir)
if ($NoEval) { $cbArgs += "--no-auto-eval" }
$start = Get-Date
try { & $py @launcher -- @cbArgs }
finally {
    Stop-Job $sampler; Remove-Job $sampler
    Remove-Item Env:CLAUDE_CODE_OAUTH_TOKEN -ErrorAction SilentlyContinue
}
"wall_seconds: {0:N0}" -f ((Get-Date) - $start).TotalSeconds | Out-File -Encoding utf8 (Join-Path $logDir "$Name.wall.txt")
"done: $Name"
