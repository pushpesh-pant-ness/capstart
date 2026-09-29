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
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [ValidateSet("crashloop", "node", "service", "networkpolicy", "replica")]
    [string]$Incident,

    [switch]$Revert
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
}
else {
    python $target
}
