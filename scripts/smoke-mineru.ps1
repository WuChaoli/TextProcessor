[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [ValidateNotNullOrEmpty()]
    [string[]]$SamplePath,
    [Parameter(Mandatory)]
    [ValidateNotNull()]
    [hashtable]$ExpectedText,
    [string]$BaseUrl = $env:EXTRACTION_WORKER__MINERU_BASE_URL,
    [string]$ApiKey = $env:EXTRACTION_WORKER__MINERU_API_KEY,
    [ValidateRange(1, 3600)]
    [int]$TimeoutSeconds = 120,
    [ValidateRange(1, 60)]
    [int]$PollIntervalSeconds = 2
)

$ErrorActionPreference = "Stop"

function Get-SampleFormat {
    param([string]$Path)

    switch ([IO.Path]::GetExtension($Path).ToLowerInvariant()) {
        ".pdf" { return "pdf" }
        ".png" { return "png" }
        ".jpg" { return "jpg" }
        ".jpeg" { return "jpg" }
        ".doc" { throw "Legacy .doc is unsupported in the first release; use .docx." }
        ".ppt" { throw "Legacy .ppt is unsupported in the first release; use .pptx." }
        ".pptx" { return "pptx" }
        default { throw "MinerU smoke accepts only the required routed formats." }
    }
}

if ([string]::IsNullOrWhiteSpace($BaseUrl)) {
    throw "EXTRACTION_WORKER__MINERU_BASE_URL is required."
}

$sampleMap = [ordered]@{}
foreach ($path in $SamplePath) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "An authorized MinerU smoke sample is not readable."
    }
    $format = Get-SampleFormat -Path $path
    if ($sampleMap.Contains($format)) {
        throw "MinerU smoke requires exactly one sample for each format."
    }
    $sampleMap[$format] = [IO.Path]::GetFullPath($path)
}
$requiredFormats = @("pdf", "png", "jpg", "pptx")
if (Compare-Object ($sampleMap.Keys | Sort-Object) $requiredFormats) {
    throw "MinerU smoke requires pdf, png, jpg, and pptx samples."
}
foreach ($format in $requiredFormats) {
    $values = @($ExpectedText[$format]) | Where-Object { -not [string]::IsNullOrWhiteSpace($_) }
    if ($values.Count -eq 0) {
        throw "MinerU smoke requires expected text for every format."
    }
    $ExpectedText[$format] = $values
}

$environmentNames = @(
    "MINERU_REAL_INTEGRATION",
    "MINERU_REAL_SAMPLE_PATHS",
    "MINERU_REAL_EXPECTATIONS",
    "EXTRACTION_WORKER__MINERU_BASE_URL",
    "EXTRACTION_WORKER__MINERU_API_KEY",
    "EXTRACTION_WORKER__PROCESSING_DEADLINE_SECONDS",
    "EXTRACTION_WORKER__POLL_INTERVAL_SECONDS"
)
$originalEnvironment = @{}
foreach ($name in $environmentNames) {
    $originalEnvironment[$name] = [Environment]::GetEnvironmentVariable($name, "Process")
}

$pushedLocation = $false
try {
    $env:MINERU_REAL_INTEGRATION = "1"
    $env:MINERU_REAL_SAMPLE_PATHS = $sampleMap | ConvertTo-Json -Compress
    $env:MINERU_REAL_EXPECTATIONS = $ExpectedText | ConvertTo-Json -Compress
    $env:EXTRACTION_WORKER__MINERU_BASE_URL = $BaseUrl
    if ([string]::IsNullOrWhiteSpace($ApiKey)) {
        Remove-Item Env:EXTRACTION_WORKER__MINERU_API_KEY -ErrorAction SilentlyContinue
    }
    else {
        $env:EXTRACTION_WORKER__MINERU_API_KEY = $ApiKey
    }
    $env:EXTRACTION_WORKER__PROCESSING_DEADLINE_SECONDS = $TimeoutSeconds.ToString()
    $env:EXTRACTION_WORKER__POLL_INTERVAL_SECONDS = $PollIntervalSeconds.ToString()

    Push-Location (Join-Path $PSScriptRoot "../backend")
    $pushedLocation = $true
    & uv run pytest -m real_integration tests/integration/structured_extraction/test_mineru_real.py -q
    if ($LASTEXITCODE -ne 0) {
        throw "MinerU real integration smoke failed."
    }
    Write-Host "MinerU real integration smoke passed for $($requiredFormats.Count) formats."
}
finally {
    if ($pushedLocation) {
        Pop-Location
    }
    foreach ($name in $environmentNames) {
        [Environment]::SetEnvironmentVariable($name, $originalEnvironment[$name], "Process")
    }
}
