param(
    [string]$HostAddress = "127.0.0.1",
    [int]$Port = 8091
)

$ErrorActionPreference = "Stop"
$PSNativeCommandUseErrorActionPreference = $true
$serviceRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$repositoryRoot = (Resolve-Path (Join-Path $serviceRoot "..\..")).Path

& (Join-Path $PSScriptRoot "prestart.ps1")
Push-Location $repositoryRoot
try {
    & uv run --project $serviceRoot uvicorn datajuicer_service.main:create_application --factory --host $HostAddress --port $Port
}
finally {
    Pop-Location
}
