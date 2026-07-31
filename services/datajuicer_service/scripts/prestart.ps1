param(
    [switch]$SkipMigration
)

$ErrorActionPreference = "Stop"
$PSNativeCommandUseErrorActionPreference = $true

$serviceRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$repositoryRoot = (Resolve-Path (Join-Path $serviceRoot "..\..")).Path
$vendorRoot = (Resolve-Path (Join-Path $serviceRoot "vendor\data-juicer")).Path
$pathSeparator = [IO.Path]::PathSeparator
$env:PYTHONPATH = "$vendorRoot$pathSeparator$serviceRoot"

$pythonVersion = & uv run --project $serviceRoot python -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
if ($pythonVersion.Trim() -ne "3.11") {
    throw "Data-Juicer Service requires Python 3.11, got $pythonVersion"
}

$expectedCommit = "7061da6ad06287aa0305eda162429b34361a56a3"
$actualCommit = (& git -C $vendorRoot rev-parse HEAD).Trim()
if ($actualCommit -ne $expectedCommit) {
    throw "Data-Juicer source commit mismatch: $actualCommit"
}

& uv lock --project $serviceRoot --check
& uv run --project $serviceRoot python -c "from datajuicer_service.profiles.compatibility import verify_datajuicer_runtime; verify_datajuicer_runtime()"

if (-not $SkipMigration) {
    if ([string]::IsNullOrWhiteSpace($env:DATAJUICER_DATABASE_URL)) {
        throw "DATAJUICER_DATABASE_URL is required for migration"
    }
    Push-Location $repositoryRoot
    try {
        & uv run --project $serviceRoot alembic -c (Join-Path $serviceRoot "alembic.ini") upgrade head
    }
    finally {
        Pop-Location
    }
}

Write-Host "Data-Juicer source prestart checks passed."
