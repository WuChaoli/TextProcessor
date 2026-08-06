[CmdletBinding()]
param(
    [string]$ComposeProjectName,
    [string[]]$ComposeFiles = @("compose.yml", "compose.docling.yml"),
    [int]$TimeoutSeconds = 180,
    [switch]$Ephemeral,
    [switch]$SkipFaultInjection
)

$ErrorActionPreference = "Stop"
$expectedServices = @("frontend", "backend-api", "task-runner", "docling", "classification", "datajuicer", "redis", "db")
$removedServices = @("backend", "docling-api", "classification-service", "prestart", "extraction-worker", "extraction-beat")
$internalServices = @("task-runner", "docling", "classification", "datajuicer", "redis", "db")

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

function Get-ContainerId([string]$Service) {
    $id = @(Invoke-Compose @("ps", "-q", $Service) | Select-Object -First 1)[0]
    if (-not $id) { throw "$Service container is missing" }
    return $id
}

function Wait-ServiceHealthy([string]$Service) {
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    do {
        $id = Get-ContainerId $Service
        $state = (& docker inspect --format "{{.State.Status}}|{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}" $id).Trim()
        if ($state -eq "running|healthy" -or $state -eq "running|none") { return }
        Start-Sleep -Seconds 2
    } while ((Get-Date) -lt $deadline)
    throw "$Service did not become healthy"
}

function Invoke-BackendPython([string]$Code, [string[]]$Arguments = @()) {
    $encoded = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($Code))
    Invoke-Compose (@("exec", "-T", "backend-api", "python", "-c", "import base64;exec(base64.b64decode('$encoded'))") + $Arguments)
}

function Invoke-RunnerPython([string]$Code, [string[]]$Arguments = @()) {
    $encoded = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($Code))
    Invoke-Compose (@("exec", "-T", "task-runner", "python", "-c", "import base64;exec(base64.b64decode('$encoded'))") + $Arguments)
}

function Get-TaskStatus([string]$TaskId, [string]$Runtime = "backend-api") {
    $code = @'
import sys
from sqlmodel import Session
from app.core.db import engine
from app.features.structured_extraction.models import ExtractionTask
with Session(engine) as session:
    task = session.get(ExtractionTask, sys.argv[1])
    print(task.status.value if task else "missing")
'@
    if ($Runtime -eq "task-runner") { return (@(Invoke-RunnerPython $code @($TaskId))[-1]).Trim() }
    return (@(Invoke-BackendPython $code @($TaskId))[-1]).Trim()
}

function New-SmokeTask([string]$FileId) {
    $code = @'
import sys
from pathlib import Path
from sqlmodel import Session, select
from app.core.db import engine
from app.models import User
from app.features.structured_extraction.dispatcher import CeleryExtractionTaskDispatcher
from app.features.structured_extraction.repository import ExtractionTaskRepository
from app.features.structured_extraction.request_policy import RequestPolicy
from app.features.structured_extraction.schemas import ExtractionTaskCreate
from app.features.structured_extraction.service import ExtractionTaskService
file_id = sys.argv[1]
source = Path(f"/data/textprocessor/input/{file_id}.txt")
target = Path(f"/data/textprocessor/output/{file_id}.md")
source.write_text("single-node failure verification\n", encoding="utf-8")
with Session(engine) as session:
    caller = session.exec(select(User)).first()
    if caller is None:
        raise RuntimeError("no caller exists; run migrations and initial_data first")
    task = ExtractionTaskService(
        ExtractionTaskRepository(session),
        RequestPolicy(input_roots=(source.parent,), output_roots=(target.parent,), allowed_http_hosts=(), allowed_http_cidrs=(), max_input_bytes=1048576),
        CeleryExtractionTaskDispatcher(),
    ).create_task(caller.id, ExtractionTaskCreate(session_id="single-node-verifier", file_id=file_id, file_storage_path=str(source), target_path=str(target)))
    print(task.id)
'@
    return (@(Invoke-BackendPython $code @($FileId) | Where-Object { $_ -match "^[0-9a-f-]{36}$" })[-1]).Trim()
}

function Wait-TaskSucceeded([string]$TaskId, [string]$Runtime = "backend-api") {
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    do {
        $status = Get-TaskStatus $TaskId $Runtime
        if ($status -eq "succeeded") { return }
        if ($status -in @("failed", "cancelled", "missing")) { throw "task $TaskId ended as $status" }
        Start-Sleep -Seconds 1
    } while ((Get-Date) -lt $deadline)
    throw "task $TaskId did not succeed"
}

try {
    $configured = @(Invoke-Compose @("config", "--services") | Where-Object { $_ })
    if (Compare-Object ($configured | Sort-Object) ($expectedServices | Sort-Object)) { throw "default topology is not the exact eight-service production set" }
    foreach ($removed in $removedServices) {
        if ($configured -contains $removed) { throw "removed service is still configured: $removed" }
    }
    foreach ($service in $expectedServices) { Wait-ServiceHealthy $service }

    foreach ($service in $internalServices) {
        $inspect = & docker inspect (Get-ContainerId $service) | ConvertFrom-Json
        $bindings = $inspect[0].HostConfig.PortBindings
        if ($null -ne $bindings -and @($bindings.PSObject.Properties).Count -gt 0) { throw "$service publishes a host port" }
    }

    & "$PSScriptRoot/verify-task-runner.ps1" -ComposeProjectName $ComposeProjectName -ComposeFiles $ComposeFiles -TimeoutSeconds $TimeoutSeconds -SkipFaultInjection:$SkipFaultInjection
    if ($LASTEXITCODE -ne 0) { throw "task-runner verification failed" }

    if (-not $SkipFaultInjection) {
        # API-independent task completion: queue with the runner stopped, then stop API while it executes.
        Invoke-Compose @("stop", "task-runner") | Out-Null
        $taskA = New-SmokeTask "api-independent-$([guid]::NewGuid().ToString('N'))"
        Invoke-Compose @("start", "task-runner") | Out-Null
        Wait-ServiceHealthy "task-runner"
        Invoke-Compose @("stop", "backend-api") | Out-Null
        Wait-TaskSucceeded $taskA "task-runner"
        Invoke-Compose @("start", "backend-api") | Out-Null
        Wait-ServiceHealthy "backend-api"

        # Task Runner-independent API and recovery: API creates and queries queued work while runner is absent.
        Invoke-Compose @("stop", "task-runner") | Out-Null
        $taskB = New-SmokeTask "runner-independent-$([guid]::NewGuid().ToString('N'))"
        if ((Get-TaskStatus $taskB) -ne "queued") { throw "API could not create/query a queued task while task-runner was stopped" }
        Invoke-Compose @("start", "task-runner") | Out-Null
        Wait-ServiceHealthy "task-runner"
        Wait-TaskSucceeded $taskB

        Invoke-Compose @("restart", "backend-api") | Out-Null
        Wait-ServiceHealthy "backend-api"
        foreach ($service in @("docling", "classification", "datajuicer")) { Wait-ServiceHealthy $service }
    }

    Write-Host "SINGLE_NODE_STACK_OK services=8 internalPorts=none"
}
finally {
    if ($Ephemeral) {
        Invoke-Compose @("down", "--volumes", "--remove-orphans")
    }
}
