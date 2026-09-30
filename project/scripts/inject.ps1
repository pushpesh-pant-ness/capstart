<#
.SYNOPSIS
  Inject one of the 5 MVP faults and automatically drive it end-to-end:
  wait for the alert to fire and the agent to create an incident, approve
  it, wait for the executor to finish, and print a clear PASS/FAIL summary.
  No manual UI clicking, curl, or psql querying required.

.EXAMPLE
  ./scripts/inject.ps1 -Incident crashloop
  ./scripts/inject.ps1 -Incident service
  ./scripts/inject.ps1 -Incident networkpolicy
  ./scripts/inject.ps1 -Incident replica
  ./scripts/inject.ps1 -Incident node
  ./scripts/inject.ps1 -Incident node -AutoRevertAfter 30   # extra seconds to admire the cordon/poll before it self-heals

  # Manually undo a fault without going through the agent at all:
  ./scripts/inject.ps1 -Incident crashloop -Revert

  # Watch the agent diagnose it but click Approve/Reject yourself in the UI:
  ./scripts/inject.ps1 -Incident crashloop -Manual

  # Skip the "is the cluster clean?" guard (not recommended - faults stack):
  ./scripts/inject.ps1 -Incident node -Force

.NOTES
  Node NotReady is the one incident the agent cannot fully self-heal - a
  downed kubelet can only be restarted from outside the Kubernetes API. This
  script always calls `--revert` itself (after the agent has genuinely
  registered the fault and started cordon/poll, so there's no more racing
  the ~40-50s it takes kind to actually flip the node NotReady). If the
  executor's 5-minute retry budget somehow already expired before the
  revert landed, this script automatically hits /retry once for you too.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [ValidateSet("crashloop", "node", "service", "networkpolicy", "replica")]
    [string]$Incident,

    [switch]$Revert,

    # Extra seconds to wait AFTER the incident is confirmed + approved before
    # reverting the node's kubelet (purely cosmetic - lets you watch a few
    # "waiting for kubelet" retry log lines before it self-heals). 0 = revert
    # as soon as the incident is approved. Only used for -Incident node.
    [int]$AutoRevertAfter = 15,

    # Don't auto-approve - inject, wait for the incident, then leave it
    # "pending" for you to Approve/Reject by hand in the UI (still polls and
    # reports the final outcome once you do).
    [switch]$Manual,

    # Skip the pre-flight "is the cluster/audit log clean" check. Without
    # this, injecting a second fault while a previous one is still
    # unresolved is refused, since faults stacked on top of each other are
    # the #1 cause of "the agent isn't solving it" confusion.
    [switch]$Force,

    [string]$AgentUrl = "http://localhost:8000",
    [string]$PrometheusUrl = "http://localhost:9090",
    [string]$NodeName = "capstart-worker",

    [int]$IncidentTimeoutSeconds = 150,
    [int]$ExecutionTimeoutSeconds = 330
)

$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

$scriptMap = @{
    crashloop     = "injector/crashloop.py"
    node          = "injector/node_not_ready.py"
    service       = "injector/service_unreachable.py"
    networkpolicy = "injector/networkpolicy_block.py"
    replica       = "injector/replica_mismatch.py"
}
$incidentTypeMap = @{
    crashloop     = "crashloop"
    node          = "node_not_ready"
    service       = "service_unreachable"
    networkpolicy = "networkpolicy_block"
    replica       = "replica_mismatch"
}
$target = $scriptMap[$Incident]
$incidentType = $incidentTypeMap[$Incident]

if ($Revert) {
    python $target --revert
    exit $LASTEXITCODE
}

# --- helpers ---------------------------------------------------------------

function Invoke-Psql {
    param([Parameter(Mandatory)][string]$Query)
    kubectl exec -n agent deploy/postgres -- psql -U agent -d agent -t -A -F '|' -c $Query 2>$null
}

function Get-MaxIncidentId {
    $raw = (Invoke-Psql "select coalesce(max(id),0) from incidents;") -join ""
    if ([string]::IsNullOrWhiteSpace($raw)) { return 0 }
    return [int]$raw.Trim()
}

function Get-ActiveIncidentCount {
    $raw = (Invoke-Psql "select count(*) from incidents where status in ('pending','approved','in_progress','escalated');") -join ""
    if ([string]::IsNullOrWhiteSpace($raw)) { return 0 }
    return [int]$raw.Trim()
}

function Get-IncidentRow {
    param([Parameter(Mandatory)][int]$Id)
    $raw = (Invoke-Psql "select status, resource_name, coalesce(execution_result,'') from incidents where id=$Id;") -join "`n"
    $parts = $raw -split '\|', 3
    [pscustomobject]@{
        Status        = $parts[0].Trim()
        Resource      = $parts[1].Trim()
        ExecResult    = if ($parts.Count -ge 3) { $parts[2].Trim() } else { "" }
    }
}

function Test-NodesHealthy {
    $nodes = (kubectl get nodes -o json | ConvertFrom-Json).items
    $bad = $nodes | Where-Object {
        $ready = ($_.status.conditions | Where-Object { $_.type -eq "Ready" }).status
        $ready -ne "True" -or $_.spec.unschedulable
    }
    return , $bad
}

function Wait-ForNodeReady {
    param([string]$Node, [int]$TimeoutSeconds = 90)
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        $n = kubectl get node $Node -o json | ConvertFrom-Json
        $ready = ($n.status.conditions | Where-Object { $_.type -eq "Ready" }).status
        if ($ready -eq "True") { return $true }
        Start-Sleep -Seconds 5
    }
    return $false
}

function Wait-ForNewIncident {
    # Waits not just for the incident ROW (created immediately on webhook
    # receipt, before diagnosis runs) but for its remediation_plan to
    # actually be populated (or for it to be escalated) - the whole
    # investigate -> Bedrock -> plan -> guardrail pipeline runs synchronously
    # in the webhook handler and takes several seconds, so approving as soon
    # as the row exists races an empty plan and silently no-ops.
    param([string]$IncidentType, [int]$AfterId, [int]$TimeoutSeconds)
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    Write-Host "==> Waiting for the '$IncidentType' alert to fire and the agent to finish diagnosis" -ForegroundColor Cyan -NoNewline
    while ((Get-Date) -lt $deadline) {
        $raw = (Invoke-Psql "select id, status, coalesce(remediation_plan,'') from incidents where incident_type='$IncidentType' and id > $AfterId order by id asc limit 1;") -join "`n"
        if (-not [string]::IsNullOrWhiteSpace($raw)) {
            $parts = $raw -split '\|', 3
            $id = [int]$parts[0].Trim()
            $status = $parts[1].Trim()
            $plan = if ($parts.Count -ge 3) { $parts[2].Trim() } else { "" }
            if ($status -eq "escalated" -or $plan -ne "") {
                Write-Host ""
                return [pscustomobject]@{ Id = $id; Escalated = ($status -eq "escalated") }
            }
        }
        Write-Host "." -NoNewline
        Start-Sleep -Seconds 5
    }
    Write-Host ""
    return $null
}

function Invoke-Approve {
    param([int]$Id)
    Invoke-RestMethod -Uri "$AgentUrl/incident/$Id/approve" -Method POST | Out-Null
}

function Invoke-Retry {
    param([int]$Id)
    Invoke-RestMethod -Uri "$AgentUrl/incident/$Id/retry" -Method POST | Out-Null
}

function Wait-ForTerminalStatus {
    param([int]$Id, [int]$TimeoutSeconds)
    $terminal = @("executed", "resolved", "execution_failed", "rejected")
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    Write-Host "==> Waiting for the executor to finish (incident #$Id)" -ForegroundColor Cyan -NoNewline
    while ((Get-Date) -lt $deadline) {
        $row = Get-IncidentRow -Id $Id
        if ($terminal -contains $row.Status) {
            Write-Host ""
            return $row
        }
        Write-Host "." -NoNewline
        Start-Sleep -Seconds 5
    }
    Write-Host ""
    return Get-IncidentRow -Id $Id
}

function Write-Result {
    param([pscustomobject]$Row, [int]$Id)
    switch ($Row.Status) {
        { $_ -in @("executed", "resolved") } {
            Write-Host "PASS - incident #$Id ($($Row.Resource)) -> status=$($Row.Status)" -ForegroundColor Green
        }
        default {
            Write-Host "FAIL - incident #$Id ($($Row.Resource)) -> status=$($Row.Status)" -ForegroundColor Red
        }
    }
    Write-Host $Row.ExecResult
    Write-Host "Detail: $AgentUrl/incident/$Id"
}

# --- pre-flight --------------------------------------------------------

if (-not $Force) {
    Write-Host "==> Pre-flight: checking cluster is in a clean baseline state" -ForegroundColor Cyan
    $badNodes = Test-NodesHealthy
    $activeCount = Get-ActiveIncidentCount
    if ($badNodes -or $activeCount -gt 0) {
        Write-Host "Refusing to inject - cluster is not clean:" -ForegroundColor Red
        foreach ($n in $badNodes) {
            $ready = ($n.status.conditions | Where-Object { $_.type -eq "Ready" }).status
            Write-Host "  node $($n.metadata.name): Ready=$ready unschedulable=$([bool]$n.spec.unschedulable)"
        }
        if ($activeCount -gt 0) {
            Write-Host "  $activeCount incident(s) still pending/in_progress/escalated - run ./scripts/status.ps1 or check $AgentUrl"
        }
        Write-Host "Fix the above first (e.g. 'python injector/node_not_ready.py --revert' + 'kubectl uncordon <node>'), or pass -Force to skip this check." -ForegroundColor Yellow
        exit 1
    }
}

# --- inject --------------------------------------------------------------

$beforeId = Get-MaxIncidentId
python $target
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

$found = Wait-ForNewIncident -IncidentType $incidentType -AfterId $beforeId -TimeoutSeconds $IncidentTimeoutSeconds
if (-not $found) {
    Write-Host "FAIL - no '$incidentType' incident appeared within ${IncidentTimeoutSeconds}s." -ForegroundColor Red
    Write-Host "Check $PrometheusUrl/alerts (did the alert ever fire?) and 'kubectl logs -n agent deploy/agent'."
    exit 1
}
$incidentId = $found.Id
if ($found.Escalated) {
    Write-Host "Incident #$incidentId ($incidentType) was ESCALATED by the diagnosis graph - no plan to approve." -ForegroundColor Red
    Write-Host "Check the escalation_reason at $AgentUrl/incident/$incidentId (e.g. Bedrock error, low-confidence diagnosis) - you can retry diagnosis with the Reprocess button."
    exit 1
}
Write-Host "Incident #$incidentId diagnosed and plan ready ($incidentType)." -ForegroundColor Green

if ($Manual) {
    Write-Host "-Manual: approve or reject it yourself at $AgentUrl/incident/$incidentId" -ForegroundColor Yellow
    $row = Wait-ForTerminalStatus -Id $incidentId -TimeoutSeconds $ExecutionTimeoutSeconds
    Write-Result -Row $row -Id $incidentId
    exit 0
}

Invoke-Approve -Id $incidentId
Write-Host "Approved incident #$incidentId." -ForegroundColor Green

if ($Incident -eq "node") {
    if ($AutoRevertAfter -gt 0) {
        Write-Host "==> Waiting ${AutoRevertAfter}s (cosmetic) before reverting the kubelet out-of-band" -ForegroundColor Cyan
        Start-Sleep -Seconds $AutoRevertAfter
    }
    python $target --revert
}

$row = Wait-ForTerminalStatus -Id $incidentId -TimeoutSeconds $ExecutionTimeoutSeconds

if ($Incident -eq "node" -and $row.Status -eq "execution_failed") {
    Write-Host "Retry budget was exhausted before the revert landed - confirming node is Ready and retrying once" -ForegroundColor Yellow
    if (Wait-ForNodeReady -Node $NodeName -TimeoutSeconds 90) {
        Invoke-Retry -Id $incidentId
        $row = Wait-ForTerminalStatus -Id $incidentId -TimeoutSeconds 120
    }
    else {
        Write-Host "Node still not Ready - is docker/kind healthy? ('docker exec $NodeName systemctl status kubelet')" -ForegroundColor Red
    }
}

Write-Result -Row $row -Id $incidentId
if ($row.Status -notin @("executed", "resolved")) { exit 1 }
