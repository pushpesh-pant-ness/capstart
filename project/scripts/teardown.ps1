<#
.SYNOPSIS
  Deletes the kind cluster and everything running in it (agent, Postgres,
  monitoring, demo workloads). The Postgres PVC is backed by kind's local-path
  storage on the node's container filesystem, so it is deleted along with the
  cluster - there is no other state to clean up.

.EXAMPLE
  ./scripts/teardown.ps1
#>
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

if (-not (Get-Command kind -ErrorAction SilentlyContinue)) {
    Write-Host "kind was not found in PATH." -ForegroundColor Red
    exit 1
}

Write-Host "==> Deleting kind cluster 'capstart'" -ForegroundColor Cyan
kind delete cluster --name capstart
