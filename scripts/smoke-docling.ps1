[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [ValidateNotNullOrEmpty()]
    [string[]]$SamplePath,
    [Parameter(Mandatory)]
    [ValidateNotNull()]
    [hashtable]$ExpectedText,
    [string]$BaseUrl = $env:EXTRACTION_WORKER__DOCLING_BASE_URL,
    [string]$ApiKey = $env:EXTRACTION_WORKER__DOCLING_API_KEY,
    [ValidateRange(1, 3600)]
    [int]$TimeoutSeconds = 120,
    [ValidateRange(1, 60)]
    [int]$PollIntervalSeconds = 2
)

$ErrorActionPreference = "Stop"

function Get-SampleFormat {
    param([string]$Path)

    switch ([IO.Path]::GetExtension($Path).ToLowerInvariant()) {
        ".docx" { return "docx" }
        ".xlsx" { return "xlsx" }
        ".html" { return "html" }
        ".epub" { return "epub" }
        default { throw "Docling smoke accepts only docx, xlsx, html, and epub samples." }
    }
}

if ([string]::IsNullOrWhiteSpace($BaseUrl)) {
    throw "EXTRACTION_WORKER__DOCLING_BASE_URL is required."
}
if ([string]::IsNullOrWhiteSpace($ApiKey)) {
    throw "EXTRACTION_WORKER__DOCLING_API_KEY is required."
}

$sampleMap = [ordered]@{}
foreach ($path in $SamplePath) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "An authorized Docling smoke sample is not readable."
    }
    $format = Get-SampleFormat -Path $path
    if ($sampleMap.Contains($format)) {
        throw "Docling smoke requires exactly one sample for each format."
    }
    $sampleMap[$format] = [IO.Path]::GetFullPath($path)
}
$requiredFormats = @("docx", "xlsx", "html", "epub")
if (Compare-Object ($sampleMap.Keys | Sort-Object) $requiredFormats) {
    throw "Docling smoke requires docx, xlsx, html, and epub samples."
}
foreach ($format in $requiredFormats) {
    $values = @($ExpectedText[$format]) | Where-Object { -not [string]::IsNullOrWhiteSpace($_) }
    if ($values.Count -eq 0) {
        throw "Docling smoke requires expected text for every format."
    }
    $ExpectedText[$format] = $values
}

$environmentNames = @(
    "DOCLING_REAL_INTEGRATION",
    "DOCLING_REAL_SAMPLE_PATHS",
    "DOCLING_REAL_EXPECTATIONS",
    "EXTRACTION_WORKER__DOCLING_BASE_URL",
    "EXTRACTION_WORKER__DOCLING_API_KEY",
    "EXTRACTION_WORKER__PROCESSING_DEADLINE_SECONDS",
    "EXTRACTION_WORKER__POLL_INTERVAL_SECONDS"
)
$originalEnvironment = @{}
foreach ($name in $environmentNames) {
    $originalEnvironment[$name] = [Environment]::GetEnvironmentVariable($name, "Process")
}

$pushedLocation = $false
try {
    $env:DOCLING_REAL_INTEGRATION = "1"
    $env:DOCLING_REAL_SAMPLE_PATHS = $sampleMap | ConvertTo-Json -Compress
    $env:DOCLING_REAL_EXPECTATIONS = $ExpectedText | ConvertTo-Json -Compress
    $env:EXTRACTION_WORKER__DOCLING_BASE_URL = $BaseUrl
    $env:EXTRACTION_WORKER__DOCLING_API_KEY = $ApiKey
    $env:EXTRACTION_WORKER__PROCESSING_DEADLINE_SECONDS = $TimeoutSeconds.ToString()
    $env:EXTRACTION_WORKER__POLL_INTERVAL_SECONDS = $PollIntervalSeconds.ToString()

    Push-Location (Join-Path $PSScriptRoot "../backend")
    $pushedLocation = $true
    & uv run pytest -m real_integration tests/integration/structured_extraction/test_docling_real.py -q
    if ($LASTEXITCODE -ne 0) {
        throw "Docling real integration smoke failed."
    }
    Write-Host "Docling real integration smoke passed for $($requiredFormats.Count) formats."
}
finally {
    if ($pushedLocation) {
        Pop-Location
    }
    foreach ($name in $environmentNames) {
        [Environment]::SetEnvironmentVariable($name, $originalEnvironment[$name], "Process")
    }
}
