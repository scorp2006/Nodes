# Run several CooperBench pairs in parallel through run_pair.ps1 (same launcher, same settings).
#   & .\coop\run_many.ps1 -Plan .\coop\plans\dev3.csv -Parallel 6
# The plan CSV has columns: Name,Repo,Task,Features,Condition. Each run logs to runs\cooperbench\<Name>.out.txt.
# Uses the saved encrypted token (run run_pair.ps1 once with -Remember first).
param(
    [Parameter(Mandatory)][string]$Plan,
    [int]$Parallel = 6,
    [switch]$NoEval
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$rows = Import-Csv $Plan
$logDir = Join-Path $root "runs\cooperbench"
New-Item -ItemType Directory -Force $logDir | Out-Null
$jobs = @()
foreach ($r in $rows) {
    if (Test-Path (Join-Path $logDir "$($r.Name).wall.txt")) { "skip (already finished): $($r.Name)"; continue }
    while (@($jobs | Where-Object State -eq "Running").Count -ge $Parallel) { Start-Sleep -Seconds 5 }
    $jobs += Start-Job -Name $r.Name -ArgumentList $root, $r, $logDir, $NoEval.IsPresent -ScriptBlock {
        param($root, $r, $logDir, $noEval)
        & (Join-Path $root "coop\run_pair.ps1") -Name $r.Name -Repo $r.Repo -Task $r.Task -Features $r.Features `
            -Condition $r.Condition -NoEval:$noEval *>&1 | Out-File -Encoding utf8 (Join-Path $logDir "$($r.Name).out.txt")
    }
    "started: $($r.Name)"
}
$jobs | Wait-Job | Out-Null
foreach ($j in $jobs) { "$($j.Name): $($j.State)" ; Remove-Job $j }
"all done"
