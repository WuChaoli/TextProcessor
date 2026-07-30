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
        Write-Host "[FAIL] ${Name}: $($_.Exception.Message)"
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

function Invoke-ContainerPython {
    param(
        [string]$Service,
        [string]$Code,
        [string[]]$PythonArguments = @()
    )

    $encodedCode = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($Code))
    $bootstrap = "import base64;exec(compile(base64.b64decode('$encodedCode'),'task10-smoke','exec'))"
    $composeCommand = @("exec", "-T", $Service, "python", "-c", $bootstrap) + $PythonArguments
    Invoke-Compose -Arguments $composeCommand
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

Invoke-VerificationStage "Celery broker message identity envelope" {
    Invoke-Compose exec -T redis redis-cli DEL celery | Out-Null
    Invoke-Compose stop extraction-worker | Out-Null
    $dispatchCode = @'
import uuid
from app.features.structured_extraction.dispatcher import CeleryExtractionTaskDispatcher

CeleryExtractionTaskDispatcher().enqueue_submit(uuid.uuid4())
'@
    Invoke-ContainerPython -Service "extraction-beat" -Code $dispatchCode | Out-Null
    $envelopeText = @(Invoke-Compose exec -T redis redis-cli --raw LINDEX celery 0)[0]
    if ([string]::IsNullOrWhiteSpace($envelopeText)) {
        throw "No Celery message was observed in the broker."
    }
    $envelope = $envelopeText | ConvertFrom-Json
    $bodyBytes = [Convert]::FromBase64String([string]$envelope.body)
    $body = [Text.Encoding]::UTF8.GetString($bodyBytes) | ConvertFrom-Json
    $kwargs = @($body)[1]
    $actualKeys = @($kwargs.PSObject.Properties.Name | Sort-Object)
    $expectedKeys = @("schema_version", "task_id", "task_type")
    if (Compare-Object $actualKeys $expectedKeys) {
        throw "Celery broker envelope contains unexpected task kwargs."
    }
    Invoke-Compose -Arguments @("up", "-d", "extraction-worker") | Out-Null
    $workerReady = $false
    for ($attempt = 0; $attempt -lt 12; $attempt++) {
        try {
            Assert-ServiceState -Service "extraction-worker" -RequireHealthcheck $true
            $workerReady = $true
            break
        }
        catch {
            Start-Sleep -Seconds 5
        }
    }
    if (-not $workerReady) {
        throw "Worker did not consume the verified broker message."
    }
}

if ($ExerciseWorkerLossRecovery) {
    Invoke-VerificationStage "Running task worker-loss recovery" {
        $exerciseRoot = Join-Path ([IO.Path]::GetTempPath()) "textprocessor-task10-$([guid]::NewGuid())"
        $exerciseCompose = Join-Path $exerciseRoot "compose.task10-smoke.yml"
        $originalComposeArguments = $script:composeArguments
        try {
            $inputDirectory = Join-Path $exerciseRoot "input"
            $outputDirectory = Join-Path $exerciseRoot "output"
            $stagingDirectory = Join-Path $exerciseRoot "staging"
            New-Item -ItemType Directory -Force $inputDirectory, $outputDirectory, $stagingDirectory | Out-Null
            [IO.File]::WriteAllBytes(
                (Join-Path $inputDirectory "running.pdf"),
                [Text.Encoding]::ASCII.GetBytes(
                    "%PDF-1.4`n1 0 obj`n<< /Type /Catalog >>`nendobj`ntrailer`n<< /Root 1 0 R >>`n%%EOF`n"
                )
            )
            @'
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

status_count = 0
status_lock = threading.Lock()

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        return

    def _json(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            self._json(200, {"status": "ok"})
        elif self.path == "/tasks/task10-mineru":
            global status_count
            with status_lock:
                status_count += 1
                first_status_request = status_count == 1
            if first_status_request:
                time.sleep(30)
            self._json(200, {"status": "completed"})
        elif self.path == "/tasks/task10-mineru/result":
            self._json(200, {"backend": "task10", "version": "1", "results": {"only": {"md_content": "# recovered\\n"}}})
        else:
            self._json(404, {})

    def do_POST(self):
        if self.path != "/tasks":
            self._json(404, {})
            return
        self._json(202, {"task_id": "task10-mineru"})

ThreadingHTTPServer(("0.0.0.0", 8088), Handler).serve_forever()
'@ | Set-Content -LiteralPath (Join-Path $exerciseRoot "mineru_stub.py") -Encoding utf8
            $composePath = $exerciseRoot.Replace("\", "/")
            @"
services:
  task10-mineru:
    image: python:3.14-alpine
    command: ["python", "/work/mineru_stub.py"]
    volumes:
      - type: bind
        source: $composePath
        target: /work
        read_only: true
  extraction-worker:
    environment:
      EXTRACTION_INPUT_ROOTS: '["/task10/input"]'
      EXTRACTION_OUTPUT_ROOTS: '["/task10/output"]'
      EXTRACTION_WORKER__STAGING_ROOT: /task10/staging
      EXTRACTION_WORKER__OUTPUT_ROOTS: '["/task10/output"]'
      EXTRACTION_WORKER__PRODUCTION_FORMATS: '["pdf"]'
      EXTRACTION_WORKER__MINERU_BASE_URL: http://task10-mineru:8088
      CELERY_BROKER_VISIBILITY_TIMEOUT_SECONDS: 5
    volumes:
      - type: bind
        source: $composePath/input
        target: /task10/input
        read_only: true
      - type: bind
        source: $composePath/output
        target: /task10/output
      - type: bind
        source: $composePath/staging
        target: /task10/staging
  extraction-beat:
    volumes:
      - type: bind
        source: $composePath/input
        target: /task10/input
        read_only: true
      - type: bind
        source: $composePath/output
        target: /task10/output
"@ | Set-Content -LiteralPath $exerciseCompose -Encoding utf8
            $script:composeArguments = @($originalComposeArguments) + @("-f", $exerciseCompose)
            Invoke-Compose -Arguments @(
                "up", "-d", "--force-recreate",
                "task10-mineru", "extraction-worker", "extraction-beat"
            ) | Out-Null
            for ($attempt = 0; $attempt -lt 12; $attempt++) {
                try {
                    Invoke-Compose exec -T task10-mineru python -c "import urllib.request; urllib.request.urlopen('http://localhost:8088/health', timeout=2)" | Out-Null
                    Assert-ServiceState -Service "extraction-worker" -RequireHealthcheck $true
                    break
                }
                catch {
                    Start-Sleep -Seconds 2
                }
            }
            Assert-ServiceState -Service "extraction-worker" -RequireHealthcheck $true
            $createTaskCode = @'
from pathlib import Path
from sqlmodel import Session, select
from app.core.db import engine
from app.features.structured_extraction.dispatcher import CeleryExtractionTaskDispatcher
from app.features.structured_extraction.repository import ExtractionTaskRepository
from app.features.structured_extraction.request_policy import RequestPolicy
from app.features.structured_extraction.schemas import ExtractionTaskCreate
from app.features.structured_extraction.service import ExtractionTaskService
from app.models import User

with Session(engine) as session:
    caller = session.exec(select(User)).first()
    if caller is None:
        raise RuntimeError("No smoke caller is available")
    task = ExtractionTaskService(
        ExtractionTaskRepository(session),
        RequestPolicy(
            input_roots=(Path("/task10/input"),),
            output_roots=(Path("/task10/output"),),
            allowed_http_hosts=(),
            allowed_http_cidrs=(),
            max_input_bytes=1024 * 1024,
        ),
        CeleryExtractionTaskDispatcher(),
    ).create_task(
        caller.id,
        ExtractionTaskCreate(
            session_id="task10-running-recovery",
            file_id="task10-running.pdf",
            file_storage_path="/task10/input/running.pdf",
            target_path="/task10/output/recovered.md",
        ),
    )
    print(task.id)
'@
            $taskId = @(Invoke-ContainerPython -Service "extraction-beat" -Code $createTaskCode | Where-Object { $_ -match "^[0-9a-f-]{36}$" })[-1]
            if ([string]::IsNullOrWhiteSpace($taskId)) {
                throw "Running recovery smoke task was not created."
            }
            $statusCode = @'
import sys
from sqlmodel import Session
from app.core.db import engine
from app.features.structured_extraction.models import ExtractionTask, ExtractionTaskStatus

with Session(engine) as session:
    task = session.get(ExtractionTask, sys.argv[1])
    if task is None:
        raise SystemExit(2)
    print(f"{task.status}:{task.processing_phase}:{task.external_task_id or ''}")
'@
            $runningObserved = $false
            for ($attempt = 0; $attempt -lt 60; $attempt++) {
                $status = @(Invoke-ContainerPython -Service "extraction-beat" -Code $statusCode -PythonArguments @($taskId))[-1]
                if ($status -eq "running:polling:task10-mineru") {
                    $runningObserved = $true
                    break
                }
                Start-Sleep -Milliseconds 500
            }
            if (-not $runningObserved) {
                throw "Task did not reach RUNNING/polling with a persisted external task before worker loss."
            }
            $workerId = @(Invoke-Compose ps -q extraction-worker | Select-Object -First 1)[0]
            & docker kill --signal=KILL $workerId | Out-Null
            if ($LASTEXITCODE -ne 0) {
                throw "Worker container could not be terminated."
            }
            Invoke-Compose -Arguments @("up", "-d", "extraction-worker") | Out-Null
            $succeeded = $false
            for ($attempt = 0; $attempt -lt 90; $attempt++) {
                $status = @(Invoke-ContainerPython -Service "extraction-beat" -Code $statusCode -PythonArguments @($taskId))[-1]
                if ($status -eq "succeeded::task10-mineru") {
                    $succeeded = $true
                    break
                }
                Start-Sleep -Seconds 1
            }
            if (-not $succeeded) {
                throw "Killed RUNNING task did not recover to succeeded."
            }
            $published = @(Get-ChildItem -LiteralPath $outputDirectory -Filter "*.md" -File)
            if ($published.Count -ne 1 -or $published[0].Name -ne "recovered.md") {
                throw "Recovery smoke did not publish exactly one Markdown result."
            }
        }
        finally {
            $script:composeArguments = @($originalComposeArguments) + @("-f", $exerciseCompose)
            try {
                Invoke-Compose -Arguments @("rm", "-sf", "task10-mineru") 2>$null | Out-Null
            }
            catch {
                Write-Warning "Unable to remove temporary task10-mineru container: $($_.Exception.Message)"
            }
            $script:composeArguments = $originalComposeArguments
            Invoke-Compose -Arguments @(
                "up", "-d", "--force-recreate", "extraction-worker", "extraction-beat"
            ) | Out-Null
            if (Test-Path $exerciseRoot) {
                Remove-Item -LiteralPath $exerciseRoot -Recurse -Force
            }
        }
    }
}

if ($failures.Count -gt 0) {
    throw "Extraction stack verification failed in stages: $($failures -join ', ')."
}

Write-Host "Extraction stack verification passed."
