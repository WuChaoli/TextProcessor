[CmdletBinding()]
param(
    [string]$ComposeProjectName,
    [string[]]$ComposeFiles = @(
        "compose.yml",
        "compose.docling.yml",
        "compose.override.yml"
    ),
    [string]$BackendBaseUrl = "http://localhost:8000",
    [string]$MinerUBaseUrl = $env:EXTRACTION_WORKER__MINERU_BASE_URL,
    [string]$DoclingBaseUrl,
    [string]$DoclingApiKey = $env:DOCLING_SERVE_API_KEY,
    [switch]$ExerciseWorkerLossRecovery
)

$ErrorActionPreference = "Stop"
$failures = [System.Collections.Generic.List[string]]::new()

function Get-ComposeArguments {
    $arguments = @()
    if (-not [string]::IsNullOrWhiteSpace($ComposeProjectName)) {
        $arguments += @("-p", $ComposeProjectName)
    }
    foreach ($composeFile in $ComposeFiles) {
        $arguments += @("-f", $composeFile)
    }
    return $arguments
}

function Invoke-Compose {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Arguments)

    & docker compose @script:composeArguments @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Docker Compose command failed."
    }
}

function Invoke-VerificationStage {
    param(
        [string]$Name,
        [scriptblock]$Action
    )

    try {
        & $Action
        Write-Host "[PASS] $Name"
    }
    catch {
        $failures.Add($Name)
        Write-Host "[FAIL] $Name"
    }
}

function Assert-ServiceState {
    param(
        [string]$Service,
        [bool]$RequireHealthcheck
    )

    $containerId = @(Invoke-Compose ps -q $Service | Select-Object -First 1)[0]
    if ([string]::IsNullOrWhiteSpace($containerId)) {
        throw "Service is not created."
    }
    $state = (& docker inspect --format "{{.State.Status}}" $containerId).Trim()
    if ($LASTEXITCODE -ne 0 -or $state -ne "running") {
        throw "Service is not running."
    }
    if ($RequireHealthcheck) {
        $health = (& docker inspect --format "{{if .State.Health}}{{.State.Health.Status}}{{end}}" $containerId).Trim()
        if ($LASTEXITCODE -ne 0 -or $health -ne "healthy") {
            throw "Service healthcheck is not healthy."
        }
    }
}

if ($ComposeFiles.Count -eq 0) {
    throw "At least one Compose file is required."
}
$script:composeArguments = Get-ComposeArguments

Invoke-VerificationStage "Compose configuration" {
    Invoke-Compose config --quiet | Out-Null
}

$healthcheckedServices = @(
    "db",
    "redis",
    "backend",
    "extraction-worker",
    "extraction-beat",
    "docling-redis",
    "docling-api"
)
foreach ($service in $healthcheckedServices) {
    Invoke-VerificationStage "Service health: $service" {
        Assert-ServiceState -Service $service -RequireHealthcheck $true
    }
}
Invoke-VerificationStage "Service state: docling-worker" {
    Assert-ServiceState -Service "docling-worker" -RequireHealthcheck $false
}

Invoke-VerificationStage "Backend health endpoint" {
    $response = Invoke-WebRequest -Uri "$BackendBaseUrl/api/v1/utils/health-check/" -TimeoutSec 10
    if ($response.StatusCode -ne 200) {
        throw "Backend health endpoint did not return 200."
    }
}

Invoke-VerificationStage "MinerU health endpoint" {
    if ([string]::IsNullOrWhiteSpace($MinerUBaseUrl)) {
        throw "MinerU base URL is not configured."
    }
    $response = Invoke-WebRequest -Uri "$($MinerUBaseUrl.TrimEnd('/'))/health" -TimeoutSec 10
    if ($response.StatusCode -ne 200) {
        throw "MinerU health endpoint did not return 200."
    }
}

Invoke-VerificationStage "Docling API, RQ worker, and Redis" {
    $doclingParameters = @{
        ComposeFiles = $ComposeFiles
        ComposeProjectName = $ComposeProjectName
    }
    if (-not [string]::IsNullOrWhiteSpace($DoclingBaseUrl)) {
        $doclingParameters.BaseUrl = $DoclingBaseUrl
        $doclingParameters.ApiKey = $DoclingApiKey
    }
    if ($ComposeFiles | Where-Object { [IO.Path]::GetFileName($_) -eq "compose.override.yml" }) {
        $doclingParameters.AllowPublishedPort = $true
    }
    & "$PSScriptRoot/verify-docling-deployment.ps1" @doclingParameters
    if ($LASTEXITCODE -ne 0) {
        throw "Docling deployment verifier failed."
    }
}

Invoke-VerificationStage "Celery message and lost-worker recovery contract" {
    $verificationCode = @'
import uuid
from app.core.celery_app import celery_app
from app.features.structured_extraction.celery_tasks import _message

payload = _message(uuid.uuid4())
assert set(payload) == {"task_id", "task_type", "schema_version"}
assert all(not isinstance(value, (dict, list, tuple)) for value in payload.values())
assert celery_app.conf.task_acks_late is True
assert celery_app.conf.task_reject_on_worker_lost is True
assert "recover-structured-extraction-tasks" in celery_app.conf.beat_schedule
'@
    Invoke-Compose exec -T extraction-worker python -c $verificationCode | Out-Null
}

if ($ExerciseWorkerLossRecovery) {
    Invoke-VerificationStage "Worker kill and restart recovery" {
        $workerId = @(Invoke-Compose ps -q extraction-worker | Select-Object -First 1)[0]
        if ([string]::IsNullOrWhiteSpace($workerId)) {
            throw "Worker container is not created."
        }
        & docker kill --signal=KILL $workerId | Out-Null
        if ($LASTEXITCODE -ne 0) {
            throw "Worker container could not be terminated."
        }
        Invoke-Compose up -d extraction-worker | Out-Null
        for ($attempt = 0; $attempt -lt 12; $attempt++) {
            try {
                Assert-ServiceState -Service "extraction-worker" -RequireHealthcheck $true
                return
            }
            catch {
                Start-Sleep -Seconds 5
            }
        }
        throw "Worker did not become healthy after restart."
    }
}

if ($failures.Count -gt 0) {
    throw "Extraction stack verification failed in stages: $($failures -join ', ')."
}

Write-Host "Extraction stack verification passed."
