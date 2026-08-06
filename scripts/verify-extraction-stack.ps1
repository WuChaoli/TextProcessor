[CmdletBinding()]
param(
    [string]$ComposeProjectName,
    [string[]]$ComposeFiles = @("compose.yml", "compose.docling.yml"),
    [int]$TimeoutSeconds = 180,
    [switch]$Ephemeral,
    [switch]$SkipFaultInjection
)

$ErrorActionPreference = "Stop"
& "$PSScriptRoot/verify-single-node-stack.ps1" `
    -ComposeProjectName $ComposeProjectName `
    -ComposeFiles $ComposeFiles `
    -TimeoutSeconds $TimeoutSeconds `
    -Ephemeral:$Ephemeral `
    -SkipFaultInjection:$SkipFaultInjection
if ($LASTEXITCODE -ne 0) { throw "single-node extraction verification failed" }
