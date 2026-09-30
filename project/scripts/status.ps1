<#
.SYNOPSIS
  Quick health check across all namespaces + the agent's audit log, useful
  after deploy.ps1 or after approving/rejecting an incident in the UI.

.EXAMPLE
  ./scripts/status.ps1
#>
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

Write-Host "== Nodes ==" -ForegroundColor Cyan
kubectl get nodes

Write-Host "`n== demo namespace ==" -ForegroundColor Cyan
kubectl get pods -n demo -o wide

Write-Host "`n== monitoring namespace ==" -ForegroundColor Cyan
kubectl get pods -n monitoring -o wide

Write-Host "`n== agent namespace ==" -ForegroundColor Cyan
kubectl get pods -n agent -o wide

Write-Host "`n== agent /healthz ==" -ForegroundColor Cyan
try {
    Invoke-RestMethod -Uri "http://localhost:8000/healthz" -TimeoutSec 5 | ConvertTo-Json -Compress
}
catch {
    Write-Host "Could not reach http://localhost:8000/healthz - is the agent Deployment Ready?" -ForegroundColor Yellow
}

Write-Host "`n== audit log (incidents table) ==" -ForegroundColor Cyan
kubectl exec -n agent deploy/postgres -- psql -U agent -d agent -c `
    "select id,incident_type,status,decision_by,received_at from incidents order by id"

Write-Host ""
Write-Host "UI:            http://localhost:8000"
Write-Host "Prometheus:    http://localhost:9090"
Write-Host "Alertmanager:  http://localhost:9093"
