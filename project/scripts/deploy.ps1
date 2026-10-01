<#
.SYNOPSIS
  One-command deployment: kind cluster -> demo workloads -> monitoring stack
  -> agent image build/load -> RBAC -> AWS secret (from .env) -> agent deploy.

.EXAMPLE
  cd project
  Copy-Item .env.example .env   # then fill in your AWS credentials
  ./scripts/deploy.ps1
#>
[CmdletBinding()]
param(
    [switch]$SkipClusterCreate,
    [switch]$SkipImageBuild
)

# Continue (not Stop): native tools like kind/docker write progress to stderr,
# which Windows PowerShell 5.1 would otherwise turn into a terminating error.
# Real failures are still caught by Invoke-Native's $LASTEXITCODE check below.
$ErrorActionPreference = "Continue"
Set-Location (Join-Path $PSScriptRoot "..")

function Invoke-Native {
    param(
        [Parameter(Mandatory)] [string] $Description,
        [Parameter(Mandatory)] [scriptblock] $Script
    )
    Write-Host ""
    Write-Host "==> $Description" -ForegroundColor Cyan
    & $Script
    if ($LASTEXITCODE -ne 0) {
        Write-Host "FAILED: $Description (exit code $LASTEXITCODE)" -ForegroundColor Red
        exit 1
    }
}

# --- Preflight ------------------------------------------------------------

foreach ($tool in @("kind", "kubectl", "docker", "python")) {
    if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) {
        Write-Host "Required tool '$tool' was not found in PATH." -ForegroundColor Red
        exit 1
    }
}

if (-not (Test-Path ".env")) {
    Write-Host ".env not found in project root." -ForegroundColor Red
    Write-Host "Run:  Copy-Item .env.example .env   then fill in your AWS credentials." -ForegroundColor Yellow
    exit 1
}

# --- Cluster ---------------------------------------------------------------

if (-not $SkipClusterCreate) {
    $clusters = (kind get clusters 2>$null)
    if ($clusters -contains "capstart") {
        Write-Host "==> kind cluster 'capstart' already exists, skipping creation" -ForegroundColor Yellow
    }
    else {
        Invoke-Native "Creating kind cluster 'capstart'" { kind create cluster --config cluster/kind-config.yaml }
    }
}

# Installs a systemd watchdog inside each worker node container that
# auto-restarts kubelet if it's been stopped too long, no matter why (any
# injector, or a human running `docker exec ... systemctl stop kubelet` by
# hand) - so node_not_ready always self-heals within a bounded time instead
# of depending on someone remembering to run `injector/node_not_ready.py
# --revert`. Idempotent (systemctl enable --now is safe to re-run).
Invoke-Native "Installing kubelet auto-repair watchdog on worker nodes" {
    foreach ($node in @("capstart-worker", "capstart-worker2")) {
        Get-Content "cluster/kubelet-watchdog.sh" -Raw | docker exec -i $node bash -c "cat > /usr/local/bin/kubelet-watchdog.sh"
        docker exec $node chmod +x /usr/local/bin/kubelet-watchdog.sh
        Get-Content "cluster/kubelet-watchdog.service" -Raw | docker exec -i $node bash -c "cat > /etc/systemd/system/kubelet-watchdog.service"
        docker exec $node systemctl daemon-reload
        docker exec $node systemctl enable --now kubelet-watchdog
    }
}

# --- Namespaces + demo workloads -------------------------------------------

Invoke-Native "Applying namespaces" { kubectl apply -f cluster/workloads/namespaces.yaml }
Invoke-Native "Applying demo workloads" { kubectl apply -f cluster/workloads/demo-app.yaml }


# --- Monitoring stack --------------------------------------------------

Invoke-Native "Applying Prometheus config" { kubectl apply -f monitoring/prometheus/prometheus-config.yaml }
Invoke-Native "Applying Prometheus deployment" { kubectl apply -f monitoring/prometheus/prometheus-deployment.yaml }
Invoke-Native "Applying Alertmanager config" { kubectl apply -f monitoring/prometheus/alertmanager-config.yaml }
Invoke-Native "Applying Alertmanager deployment" { kubectl apply -f monitoring/prometheus/alertmanager-deployment.yaml }
Invoke-Native "Applying kube-state-metrics" { kubectl apply -f monitoring/kube-state-metrics/kube-state-metrics.yaml }
Invoke-Native "Applying node-exporter" { kubectl apply -f monitoring/node-exporter/node-exporter.yaml }
Invoke-Native "Applying blackbox-exporter" { kubectl apply -f monitoring/blackbox/blackbox-exporter.yaml }
Invoke-Native "Applying Loki config" { kubectl apply -f monitoring/loki/loki-config.yaml }
Invoke-Native "Applying Loki deployment" { kubectl apply -f monitoring/loki/loki-deployment.yaml }
Invoke-Native "Applying Promtail config" { kubectl apply -f monitoring/loki/promtail-config.yaml }
Invoke-Native "Applying Promtail daemonset" { kubectl apply -f monitoring/loki/promtail-daemonset.yaml }

Invoke-Native "Waiting for demo workloads" {
    kubectl rollout status deployment/demo-web -n demo --timeout=120s
    kubectl rollout status deployment/demo-api -n demo --timeout=120s
}
Invoke-Native "Waiting for monitoring stack to become Ready" {
    kubectl rollout status deployment/prometheus -n monitoring --timeout=180s
    kubectl rollout status deployment/alertmanager -n monitoring --timeout=180s
    kubectl rollout status deployment/loki -n monitoring --timeout=180s
    kubectl rollout status deployment/kube-state-metrics -n monitoring --timeout=120s
    kubectl rollout status deployment/blackbox-exporter -n monitoring --timeout=120s
    kubectl rollout status daemonset/node-exporter -n monitoring --timeout=120s
    kubectl rollout status daemonset/promtail -n monitoring --timeout=120s
}

# --- Agent -------------------------------------------------------------

if (-not $SkipImageBuild) {
    Invoke-Native "Building agent Docker image" { docker build -t capstart/remediation-agent:local agent/ }
}
Invoke-Native "Loading agent image into kind" { kind load docker-image capstart/remediation-agent:local --name capstart }
Invoke-Native "Applying agent RBAC" { kubectl apply -f agent/k8s/rbac.yaml }
Invoke-Native "Deploying Postgres (audit/incident store)" { kubectl apply -f agent/k8s/postgres.yaml }
Invoke-Native "Waiting for Postgres to become Ready" { kubectl rollout status deployment/postgres -n agent --timeout=120s }
Invoke-Native "Creating aws-bedrock-credentials secret from .env" {
    kubectl create secret generic aws-bedrock-credentials -n agent --from-env-file=.env --dry-run=client -o yaml |
        kubectl apply -f -
}
Invoke-Native "Deploying agent" { kubectl apply -f agent/k8s/deployment.yaml }
if (-not $SkipImageBuild) {
    # deployment.yaml pins a static ":local" tag, so `kubectl apply` sees no spec diff
    # after a rebuild and would otherwise leave the old code running in the existing pod.
    Invoke-Native "Restarting agent to pick up rebuilt image" { kubectl rollout restart deployment/agent -n agent }
}
Invoke-Native "Waiting for agent to become Ready" { kubectl rollout status deployment/agent -n agent --timeout=120s }

Write-Host ""
Write-Host "Everything deployed." -ForegroundColor Green
Write-Host "  Approval UI:   http://localhost:8000"
Write-Host "  Prometheus:    http://localhost:9090"
Write-Host "  Alertmanager:  http://localhost:9093"
Write-Host ""
Write-Host "Next: run .\scripts\inject.ps1 -Incident crashloop, then watch the UI/agent logs (see instructions.md)."
