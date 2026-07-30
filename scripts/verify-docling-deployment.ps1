[CmdletBinding()]
param(
    [string]$BaseUrl = "http://localhost:5001",
    [string]$ApiKey = $env:DOCLING_SERVE_API_KEY
)

$ErrorActionPreference = "Stop"

if ([string]::IsNullOrWhiteSpace($ApiKey)) {
    throw "DOCLING_SERVE_API_KEY is required."
}

$expectedServices = @("docling-redis", "docling-api", "docling-worker")
$runningServices = docker compose -f compose.yml -f compose.docling.yml ps --services --status running
if ($LASTEXITCODE -ne 0) {
    throw "Unable to inspect the Docling Compose services."
}

foreach ($service in $expectedServices) {
    if ($runningServices -notcontains $service) {
        throw "Docling service is not running: $service"
    }
}

$unauthorizedStatus = 0
try {
    Invoke-WebRequest -Uri "$BaseUrl/v1/convert/file/async" -Method Post | Out-Null
}
catch {
    $unauthorizedStatus = [int]$_.Exception.Response.StatusCode
}
if ($unauthorizedStatus -notin @(401, 403)) {
    throw "Docling API key enforcement failed; received HTTP $unauthorizedStatus."
}

$headers = @{"X-API-Key" = $ApiKey}
$openApi = Invoke-RestMethod -Uri "$BaseUrl/openapi.json" -Headers $headers
$requiredPaths = @(
    "/v1/convert/file/async",
    "/v1/status/poll/{task_id}",
    "/v1/result/{task_id}"
)
foreach ($path in $requiredPaths) {
    if ($null -eq $openApi.paths.$path) {
        throw "Docling OpenAPI path is missing: $path"
    }
}

$apiImage = docker compose -f compose.yml -f compose.docling.yml images --format json docling-api |
    ConvertFrom-Json
$workerImage = docker compose -f compose.yml -f compose.docling.yml images --format json docling-worker |
    ConvertFrom-Json
if ($apiImage.ID -ne $workerImage.ID) {
    throw "Docling API and worker do not use the same image."
}

Write-Host "Docling deployment verification passed."
