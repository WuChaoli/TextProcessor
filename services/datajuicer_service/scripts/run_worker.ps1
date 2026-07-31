param(
    [string]$LogLevel = "INFO"
)

$ErrorActionPreference = "Stop"
$PSNativeCommandUseErrorActionPreference = $true
$serviceRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$repositoryRoot = (Resolve-Path (Join-Path $serviceRoot "..\..")).Path
$queue = if ($env:DATAJUICER_CELERY_QUEUE) {
    $env:DATAJUICER_CELERY_QUEUE
}
else {
    "datajuicer.jobs"
}

& (Join-Path $PSScriptRoot "prestart.ps1")
Push-Location $repositoryRoot
try {
    & uv run --project $serviceRoot celery -A datajuicer_service.worker_app:app worker --loglevel $LogLevel --queues $queue --pool solo
}
finally {
    Pop-Location
}
