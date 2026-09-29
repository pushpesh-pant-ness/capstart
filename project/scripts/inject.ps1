<#
.SYNOPSIS
  Trigger (or manually revert) one of the 5 MVP fault-injection scripts.

.EXAMPLE
  ./scripts/inject.ps1 -Incident crashloop
  ./scripts/inject.ps1 -Incident crashloop -Revert
  ./scripts/inject.ps1 -Incident node
  ./scripts/inject.ps1 -Incident service
  ./scripts/inject.ps1 -Incident networkpolicy
  ./scripts/inject.ps1 -Incident replica

  # Node NotReady is the one incident the agent cannot fully self-heal - a
  # downed kubelet can only be restarted from outside the Kubernetes API. Use
  # -AutoRevertAfter so this one command injects the fault AND automatically
  # restarts the kubelet after N seconds, giving you time to Approve in the UI
  # first without needing a second terminal/manual revert:
  ./scripts/inject.ps1 -Incident node -AutoRevertAfter 30
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [ValidateSet("crashloop", "node", "service", "networkpolicy", "replica")]
    [string]$Incident,

    [switch]$Revert,

    # Seconds to wait after injecting before automatically running --revert.
    # Mainly useful for -Incident node, since that fault requires an
    # out-of-band fix (kubelet restart) the agent's Kubernetes API access
    # cannot perform on its own.
    [int]$AutoRevertAfter = 0
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

$target = $scriptMap[$Incident]
if ($Revert) {
    python $target --revert
    exit $LASTEXITCODE
}

python $target
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

if ($AutoRevertAfter -gt 0) {
    Write-Host "==> Auto-reverting '$Incident' in $AutoRevertAfter s (Approve the incident in the UI now: http://localhost:8000)" -ForegroundColor Yellow
    Start-Sleep -Seconds $AutoRevertAfter
    python $target --revert
}
