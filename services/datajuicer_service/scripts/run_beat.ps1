param(
    [string]$LogLevel = "INFO"
)

$ErrorActionPreference = "Stop"
$PSNativeCommandUseErrorActionPreference = $true
$serviceRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$repositoryRoot = (Resolve-Path (Join-Path $serviceRoot "..\..")).Path

& (Join-Path $PSScriptRoot "prestart.ps1")
Push-Location $repositoryRoot
try {
    & uv run --project $serviceRoot celery -A datajuicer_service.worker_app:app beat --loglevel $LogLevel
}
finally {
    Pop-Location
}
