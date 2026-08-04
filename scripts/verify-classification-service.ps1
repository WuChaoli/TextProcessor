param(
    [string]$BaseUrl = "http://localhost:8000",
    [string]$Token = $env:CLASSIFICATION_INTERNAL_SERVICE_TOKEN
)

$ErrorActionPreference = "Stop"
if ([string]::IsNullOrWhiteSpace($Token)) { throw "CLASSIFICATION_INTERNAL_SERVICE_TOKEN is required" }

$live = Invoke-WebRequest -UseBasicParsing "$BaseUrl/health/live"
if ($live.StatusCode -ne 200) { throw "live check failed" }
$ready = Invoke-WebRequest -UseBasicParsing "$BaseUrl/health/ready"
if ($ready.StatusCode -ne 200) { throw "ready check failed" }

try {
    Invoke-WebRequest -UseBasicParsing -Method Post "$BaseUrl/internal/v1/classify" -ContentType "application/json" -Body '{"schemaVersion":"1","requestId":"unauthorized","text":"smoke"}'
    throw "unauthenticated request unexpectedly succeeded"
} catch {
    if ($_.Exception.Response.StatusCode.value__ -ne 401) { throw }
}

$requestId = "classification-smoke-$([guid]::NewGuid().ToString('N'))"
$headers = @{ Authorization = "Bearer $Token"; "X-Request-ID" = $requestId }
$body = @{ schemaVersion = "1"; requestId = $requestId; text = "内部分类服务验收文本" } | ConvertTo-Json -Compress
$result = Invoke-RestMethod -Method Post "$BaseUrl/internal/v1/classify" -Headers $headers -ContentType "application/json" -Body $body
if ($result.tags.Count -ne 4) { throw "classification response must contain four tags" }
if ($null -eq $result.confidence.topTriple -or $null -eq $result.confidence.endDoc) { throw "classification response must contain two confidences" }

$logs = docker compose logs --no-color classification-service
foreach ($pattern in @($Token, "internal_service_token", "Traceback (most recent call last)", "C:\\", "/models/releases/")) {
    if ($logs -match [regex]::Escape($pattern)) { throw "sensitive log pattern detected" }
}
Write-Output "classification-service verification passed"
