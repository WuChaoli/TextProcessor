[CmdletBinding()]
param(
    [string]$ComposeProjectName,
    [string[]]$ComposeFiles = @("compose.yml", "compose.docling.yml"),
    [int]$TimeoutSeconds = 120,
    [switch]$SkipFaultInjection
)

$ErrorActionPreference = "Stop"

function Get-ComposePrefix {
    $result = @()
    if ($ComposeProjectName) { $result += @("-p", $ComposeProjectName) }
    foreach ($file in $ComposeFiles) { $result += @("-f", $file) }
    return $result
}

$script:composePrefix = Get-ComposePrefix
function Invoke-Compose([string[]]$Arguments) {
    & docker compose @script:composePrefix @Arguments
    if ($LASTEXITCODE -ne 0) { throw "docker compose failed: $($Arguments -join ' ')" }
}

function Get-RunnerId {
    $id = @(Invoke-Compose @("ps", "-q", "task-runner") | Select-Object -First 1)[0]
    if (-not $id) { throw "task-runner container is missing" }
    return $id
}

function Wait-Healthy([string]$ContainerId, [int]$MinimumRestarts) {
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    do {
        $state = & docker inspect --format "{{.State.Status}}|{{if .State.Health}}{{.State.Health.Status}}{{end}}|{{.RestartCount}}" $ContainerId
        if ($LASTEXITCODE -eq 0) {
            $parts = $state.Trim().Split("|")
            if ($parts[0] -eq "running" -and $parts[1] -eq "healthy" -and [int]$parts[2] -ge $MinimumRestarts) { return }
        }
        Start-Sleep -Seconds 2
    } while ((Get-Date) -lt $deadline)
    throw "task-runner did not become healthy with RestartCount >= $MinimumRestarts"
}

function Get-ChildState {
    $json = @(Invoke-Compose @("exec", "-T", "task-runner", "cat", "/var/run/textprocessor/task-runner.json")) -join ""
    $state = $json | ConvertFrom-Json
    $keys = @($state.PSObject.Properties.Name | Sort-Object)
    if (Compare-Object $keys @("beat", "worker")) { throw "task-runner state keys must be exactly worker and beat" }
    foreach ($name in @("worker", "beat")) {
        if ([int]$state.$name -le 0) { throw "invalid $name PID" }
        Invoke-Compose @("exec", "-T", "task-runner", "kill", "-0", "$($state.$name)") | Out-Null
    }
    return $state
}

function Invoke-ChildFault([string]$Name) {
    $containerId = Get-RunnerId
    $before = [int](& docker inspect --format "{{.RestartCount}}" $containerId)
    $state = Get-ChildState
    # Kill the child, not PID 1; Docker's restart policy then restarts the failed supervisor container.
    Invoke-Compose @("exec", "-T", "task-runner", "kill", "-KILL", "$($state.$Name)") | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "docker kill equivalent could not kill $Name child" }
    Wait-Healthy $containerId ($before + 1)
    Get-ChildState | Out-Null
}

$runnerId = Get-RunnerId
Wait-Healthy $runnerId 0
Get-ChildState | Out-Null
Invoke-Compose @("exec", "-T", "redis", "redis-cli", "-n", "0", "PING") | Select-String -SimpleMatch "PONG" | Out-Null
Invoke-Compose @("exec", "-T", "task-runner", "test", "-f", "/var/lib/celery/beat-schedule") | Out-Null
Invoke-Compose @("exec", "-T", "task-runner", "celery", "-A", "app.core.celery_app:celery_app", "inspect", "ping", "--timeout=10") | Select-String -SimpleMatch "pong" | Out-Null

if (-not $SkipFaultInjection) {
    Invoke-ChildFault "worker"
    Invoke-ChildFault "beat"
}

Write-Host "TASK_RUNNER_OK children=worker,beat brokerDb=0 faults=$(-not $SkipFaultInjection)"
