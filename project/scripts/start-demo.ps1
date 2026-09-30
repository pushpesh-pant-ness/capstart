<#
.SYNOPSIS
  One command to go from nothing to "ready to inject a fault": deploys the
  whole stack (if not already deployed), opens the agent-log and demo-pod
  watch windows you'd otherwise start by hand, and opens the UI/Prometheus
  tabs in your browser.

.EXAMPLE
  ./scripts/start-demo.ps1
  ./scripts/start-demo.ps1 -SkipClusterCreate -SkipImageBuild   # fast re-run after first deploy
#>
[CmdletBinding()]
param(
    [switch]$SkipClusterCreate,
    [switch]$SkipImageBuild
)

$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")
$root = (Get-Location).Path

& (Join-Path $PSScriptRoot "deploy.ps1") -SkipClusterCreate:$SkipClusterCreate -SkipImageBuild:$SkipImageBuild
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host ""
Write-Host "==> Opening agent-log and demo-pod watch windows" -ForegroundColor Cyan
Start-Process powershell -ArgumentList @(
    "-NoExit", "-Command",
    "Set-Location '$root'; kubectl logs -n agent deploy/agent -f"
)
Start-Process powershell -ArgumentList @(
    "-NoExit", "-Command",
    "Set-Location '$root'; kubectl get pods -n demo -o wide -w"
)

Write-Host "==> Opening browser tabs (UI, Prometheus alerts)" -ForegroundColor Cyan
Start-Process "http://localhost:8000"
Start-Process "http://localhost:9090/alerts"

Write-Host ""
Write-Host "Ready. Inject a fault with one command - it auto-approves, waits for the" -ForegroundColor Green
Write-Host "executor, and prints PASS/FAIL, e.g.:"
Write-Host "  ./scripts/inject.ps1 -Incident crashloop"
Write-Host "  ./scripts/inject.ps1 -Incident node   # handles the kubelet revert/retry dance itself"
