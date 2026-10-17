# Evaluate every run in a plan with CooperBench's own evaluator, once, after the whole batch has finished.
#   & .\coop\eval_all.ps1 -Plan .\coop\plans\test1b.csv -Parallel 4
param(
    [Parameter(Mandatory)][string]$Plan,
    [int]$Parallel = 4,
    [switch]$Force
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$logDir = Join-Path $root "runs\cooperbench"
$jobs = @()
foreach ($r in (Import-Csv $Plan)) {
    while (@($jobs | Where-Object State -eq "Running").Count -ge $Parallel) { Start-Sleep -Seconds 2 }
    $jobs += Start-Job -Name $r.Name -ArgumentList $root, $logDir, $r.Name, $Force.IsPresent -ScriptBlock {
        param($root, $logDir, $name, $force)
        $env:Path = "C:\Program Files\Docker\Docker\resources\bin;" + $env:Path
        $evalArgs = @("eval", "-n", $name, "--backend", "docker", "-c", "1",
                      "--dataset-dir", (Join-Path $root "external\CooperBench\dataset"), "--log-dir", $logDir)
        if ($force) { $evalArgs += "--force" }
        & (Join-Path $root ".venv-cb\Scripts\cooperbench.exe") @evalArgs *>&1 |
            Out-File -Encoding utf8 (Join-Path $logDir "$name.eval.txt")
    }
}
$jobs | Wait-Job | Out-Null
"evaluated: $(@($jobs | Where-Object State -eq 'Completed').Count) of $($jobs.Count) jobs completed"
$jobs | Remove-Job
