[CmdletBinding()]
param(
    [string]$BaseUrl,
    [string]$ApiKey = $env:DOCLING_SERVE_API_KEY,
    [string]$ComposeProjectName,
    [string[]]$ComposeFiles = @("compose.yml", "compose.docling.yml"),
    [switch]$AllowPublishedPort
)

$ErrorActionPreference = "Stop"

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

if ($ComposeFiles.Count -eq 0) {
    throw "At least one Compose file is required."
}

$script:composeArguments = Get-ComposeArguments
$expectedServices = @("redis", "docling-api")
$removedServices = @("docling-redis", "docling-worker")
$runningServices = @(Invoke-Compose ps --services --status running)
foreach ($service in $expectedServices) {
    if ($runningServices -notcontains $service) {
        throw "Docling service is not running: $service"
    }
}
$configuredServices = @(Invoke-Compose config --services)
foreach ($service in $removedServices) {
    if ($configuredServices -contains $service) {
        throw "Removed Docling service is still configured: $service"
    }
}

$doclingContainerId = @(Invoke-Compose ps -q docling-api | Select-Object -First 1)[0]
$doclingInspection = @(& docker inspect $doclingContainerId) | ConvertFrom-Json
if ($LASTEXITCODE -ne 0 -or $doclingInspection.Count -ne 1) {
    throw "Unable to inspect the Docling API container."
}
$publishedPort = $doclingInspection[0].NetworkSettings.Ports.'5001/tcp'
if (-not $AllowPublishedPort -and $null -ne $publishedPort) {
    throw "Docling API must not publish host port 5001 outside an explicit local override."
}

$containerProbe = @'
import json
import os
import urllib.error
import urllib.request
from pathlib import Path

state = json.loads(
    Path("/run/textprocessor-docling/processes.json").read_text(encoding="utf-8")
)
if set(state) != {"api", "worker"}:
    raise SystemExit(1)
for pid in state.values():
    os.kill(int(pid), 0)

base_url = "http://localhost:5001"
try:
    unauthorized_request = urllib.request.Request(
        base_url + "/v1/convert/file/async",
        method="POST",
    )
    urllib.request.urlopen(unauthorized_request, timeout=5)
except urllib.error.HTTPError as error:
    if error.code not in (401, 403):
        raise SystemExit(1)
else:
    raise SystemExit(1)

request = urllib.request.Request(
    base_url + "/openapi.json",
    headers={"X-API-Key": __import__("os").environ["DOCLING_SERVE_API_KEY"]},
)
with urllib.request.urlopen(request, timeout=10) as response:
    document = response.read().decode("utf-8")
for path in (
    "/v1/convert/file/async",
    "/v1/status/poll/{task_id}",
    "/v1/result/{task_id}",
):
    if ('"' + path + '"') not in document:
        raise SystemExit(1)
'@

if ([string]::IsNullOrWhiteSpace($BaseUrl)) {
    Invoke-Compose exec -T docling-api python -c $containerProbe | Out-Null
}
else {
    if ([string]::IsNullOrWhiteSpace($ApiKey)) {
        throw "DOCLING_SERVE_API_KEY is required when BaseUrl is set."
    }
    $unauthorizedStatus = 0
    try {
        Invoke-WebRequest -Uri "$BaseUrl/v1/convert/file/async" -Method Post | Out-Null
    }
    catch {
        $unauthorizedStatus = [int]$_.Exception.Response.StatusCode
    }
    if ($unauthorizedStatus -notin @(401, 403)) {
        throw "Docling API key enforcement failed."
    }

    $headers = @{"X-API-Key" = $ApiKey}
    $openApi = Invoke-RestMethod -Uri "$BaseUrl/openapi.json" -Headers $headers
    foreach ($path in @(
        "/v1/convert/file/async",
        "/v1/status/poll/{task_id}",
        "/v1/result/{task_id}"
    )) {
        if ($null -eq $openApi.paths.$path) {
            throw "Docling OpenAPI path is missing: $path"
        }
    }
}

$redisPing = @(Invoke-Compose exec -T redis redis-cli -n 1 PING)[-1]
if ($redisPing -ne "PONG") {
    throw "Docling Redis logical database 1 did not respond to PING."
}

Write-Host "Docling deployment verification passed."
