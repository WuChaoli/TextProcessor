param(
    [string]$LogLevel = "INFO",
    [string]$SchedulePath = ""
)

$ErrorActionPreference = "Stop"
$PSNativeCommandUseErrorActionPreference = $true
$serviceRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$repositoryRoot = (Resolve-Path (Join-Path $serviceRoot "..\..")).Path
$schedule = if ($SchedulePath) {
    $SchedulePath
}
else {
    Join-Path ([System.IO.Path]::GetTempPath()) "datajuicer-service/celerybeat-schedule"
}
$scheduleDirectory = Split-Path -Parent $schedule
New-Item -ItemType Directory -Path $scheduleDirectory -Force | Out-Null

& (Join-Path $PSScriptRoot "prestart.ps1")
Push-Location $repositoryRoot
try {
    & uv run --project $serviceRoot celery -A datajuicer_service.worker_app:app beat --loglevel $LogLevel --schedule $schedule
}
finally {
    Pop-Location
}
