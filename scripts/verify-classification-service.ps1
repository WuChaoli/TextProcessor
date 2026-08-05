[CmdletBinding()]
param(
    [string]$ComposeProjectName,
    [string[]]$ComposeFiles = @("compose.yml", "compose.docling.yml"),
    [string]$Token = $env:CLASSIFICATION_INTERNAL_SERVICE_TOKEN
)

$ErrorActionPreference = "Stop"
if (-not $Token) { throw "CLASSIFICATION_INTERNAL_SERVICE_TOKEN is required" }
$prefix = @()
if ($ComposeProjectName) { $prefix += @("-p", $ComposeProjectName) }
foreach ($file in $ComposeFiles) { $prefix += @("-f", $file) }

function Invoke-Compose([string[]]$Arguments) {
    & docker compose @prefix @Arguments
    if ($LASTEXITCODE -ne 0) { throw "docker compose failed" }
}

$id = @(Invoke-Compose @("ps", "-q", "classification") | Select-Object -First 1)[0]
$inspection = @(& docker inspect $id) | ConvertFrom-Json
if ($inspection[0].State.Health.Status -ne "healthy") { throw "classification is not healthy" }
if (@($inspection[0].HostConfig.PortBindings.PSObject.Properties).Count -gt 0) { throw "classification publishes a host port" }

$probe = @'
import json, os, urllib.error, urllib.request
base = "http://localhost:8000"
for path in ("/health/live", "/health/ready"):
    with urllib.request.urlopen(base + path, timeout=10) as response:
        assert response.status == 200
request = urllib.request.Request(
    base + "/internal/v1/classify",
    data=json.dumps({"schemaVersion":"1","requestId":"unauthorized","inputUri":"file:///forbidden"}).encode(),
    headers={"Content-Type":"application/json"}, method="POST")
try:
    urllib.request.urlopen(request, timeout=10)
except urllib.error.HTTPError as error:
    assert error.code == 401
else:
    raise AssertionError("unauthenticated request succeeded")
'@
$encoded = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($probe))
Invoke-Compose @("exec", "-T", "classification", "python", "-c", "import base64;exec(base64.b64decode('$encoded'))") | Out-Null

$logs = @(Invoke-Compose @("logs", "--no-color", "classification")) -join "`n"
foreach ($pattern in @($Token, "Traceback (most recent call last)", "C:\\", "/models/releases/")) {
    if ($logs.Contains($pattern)) { throw "sensitive log pattern detected" }
}
Write-Host "CLASSIFICATION_SERVICE_OK internalOnly=true"
