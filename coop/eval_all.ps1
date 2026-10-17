# Evaluate every run in a plan with CooperBench's own evaluator, once, after the whole batch has finished.
#   & .\coop\eval_all.ps1 -Plan .\coop\plans\test1.csv
param(
    [Parameter(Mandatory)][string]$Plan,
    [switch]$Force
)
$ErrorActionPreference = "Stop"
$env:Path = "C:\Program Files\Docker\Docker\resources\bin;" + $env:Path
$root = Split-Path -Parent $PSScriptRoot
$logDir = Join-Path $root "runs\cooperbench"
$cb = Join-Path $root ".venv-cb\Scripts\cooperbench.exe"
foreach ($r in (Import-Csv $Plan)) {
    $evalArgs = @("eval", "-n", $r.Name, "--backend", "docker", "-c", "1",
                  "--dataset-dir", (Join-Path $root "external\CooperBench\dataset"), "--log-dir", $logDir)
    if ($Force) { $evalArgs += "--force" }
    & $cb @evalArgs *>&1 | Out-File -Encoding utf8 (Join-Path $logDir "$($r.Name).eval.txt")
    "evaluated: $($r.Name)"
}
"all evaluated"
