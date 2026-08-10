[CmdletBinding()]
param(
    [int]$TimeoutSeconds = 180
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$startedAt = Get-Date
$runId = ([guid]::NewGuid().ToString("N")).Substring(0, 12)
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$backendRoot = Join-Path $repoRoot "backend"
$tempRoot = Join-Path ([System.IO.Path]::GetTempPath()) "textprocessor-md-$runId"
$inputRoot = Join-Path $tempRoot "input"
$outputRoot = Join-Path $tempRoot "output"
$stagingRoot = Join-Path $tempRoot "staging"
$containerPrefix = "tp-md-stack-$runId"
$dbContainer = "$containerPrefix-db"
$redisContainer = "$containerPrefix-redis"
$ownershipManifest = Join-Path $tempRoot "ownership.json"
$processes = [System.Collections.Generic.List[System.Diagnostics.Process]]::new()
$ownedProcessRecords = [System.Collections.Generic.List[object]]::new()
$logs = [System.Collections.Generic.List[string]]::new()

function Get-FreeTcpPort {
    $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, 0)
    $listener.Start()
    try { return ([System.Net.IPEndPoint]$listener.LocalEndpoint).Port } finally { $listener.Stop() }
}

function Assert-True([bool]$Condition, [string]$Message) {
    if (-not $Condition) { throw "ASSERTION_FAILED: $Message" }
}

function Start-OwnedProcess([string]$Name, [string[]]$Arguments, [int]$ApiPort = 0) {
    $stdout = Join-Path $tempRoot "$Name.stdout.log"
    $stderr = Join-Path $tempRoot "$Name.stderr.log"
    $process = Start-Process -FilePath "uv" -ArgumentList $Arguments -WorkingDirectory $backendRoot `
        -PassThru -WindowStyle Hidden -RedirectStandardOutput $stdout -RedirectStandardError $stderr
    $processes.Add($process)
    $ownedProcessRecords.Add([pscustomobject]@{
        runId = $runId
        name = $Name
        pid = $process.Id
        apiPort = $ApiPort
    })
    ConvertTo-Json -Depth 3 -InputObject @($ownedProcessRecords) |
        Set-Content -LiteralPath $ownershipManifest -Encoding utf8
    $logs.Add($stdout)
    $logs.Add($stderr)
    return $process
}

function Stop-OwnedProcess([System.Diagnostics.Process]$Process) {
    if ($null -eq $Process -or $Process.HasExited) { return }
    & taskkill.exe /PID $Process.Id /T /F *> $null
    $Process.WaitForExit(10000) | Out-Null
}

function Wait-Http([string]$Uri) {
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    do {
        try {
            $response = Invoke-WebRequest -UseBasicParsing -Uri $Uri -TimeoutSec 2
            if ($response.StatusCode -eq 200) { return }
        } catch { Start-Sleep -Milliseconds 300 }
    } while ((Get-Date) -lt $deadline)
    throw "HTTP endpoint did not become ready: $Uri"
}

function Get-Token([string]$ApiBase) {
    $body = "username=stack-$runId%40example.com&password=Stack-$runId-Passw0rd!"
    return (Invoke-RestMethod -Method Post -Uri "$ApiBase/api/v1/login/access-token" `
        -ContentType "application/x-www-form-urlencoded" -Body $body).access_token
}

function Submit-Task([string]$ApiBase, [hashtable]$Headers, [string]$SessionId, [string]$FileId, [string]$Source, [string]$Target) {
    $payload = @{
        sessionId = $SessionId
        fileId = $FileId
        fileStoragePath = $Source
        targetPath = $Target
    } | ConvertTo-Json
    return Invoke-RestMethod -Method Post -Uri "$ApiBase/api/v1/markdown-cleaning/tasks" `
        -Headers $Headers -ContentType "application/json; charset=utf-8" -Body ([Text.Encoding]::UTF8.GetBytes($payload))
}

function Wait-Task([string]$ApiBase, [hashtable]$Headers, [string]$TaskId, [string[]]$Terminal = @("succeeded", "failed", "cancelled")) {
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    do {
        $task = Invoke-RestMethod -Uri "$ApiBase/api/v1/markdown-cleaning/tasks/$TaskId" -Headers $Headers
        if ($Terminal -contains $task.status) { return $task }
        Start-Sleep -Milliseconds 300
    } while ((Get-Date) -lt $deadline)
    throw "Task $TaskId did not reach a terminal state"
}

function Query-Db([string]$Sql) {
    $result = & docker exec $dbContainer psql -U postgres -d textprocessor -Atc $Sql
    if ($LASTEXITCODE -ne 0) { throw "PostgreSQL query failed" }
    return ($result | Out-String).Trim()
}

try {
    New-Item -ItemType Directory -Force -Path $inputRoot, $outputRoot, $stagingRoot | Out-Null
    $dbPort = Get-FreeTcpPort
    $redisPort = Get-FreeTcpPort
    $apiPort = Get-FreeTcpPort

    & docker run -d --name $dbContainer -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=textprocessor `
        -p "127.0.0.1:${dbPort}:5432" postgres:18 | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Unable to start PostgreSQL" }
    & docker run -d --name $redisContainer -p "127.0.0.1:${redisPort}:6379" redis:7-alpine `
        redis-server --save "" --appendonly no | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Unable to start Redis" }

    $deadline = (Get-Date).AddSeconds(60)
    do {
        & docker exec $dbContainer pg_isready -U postgres -d textprocessor *> $null
        $dbReady = $LASTEXITCODE -eq 0
        if (-not $dbReady) { Start-Sleep -Milliseconds 300 }
    } while (-not $dbReady -and (Get-Date) -lt $deadline)
    Assert-True $dbReady "PostgreSQL did not become healthy"

    $workerSettings = @{
        staging_root = $stagingRoot
        queue_lease_seconds = 5
        queue_recovery_interval_seconds = 1
        queue_recovery_batch_size = 20
        processing_soft_timeout_seconds = 120
        processing_hard_timeout_seconds = 150
        max_attempts = 3
        max_in_flight_tasks = 1
        allowed_stale_grace_seconds = 0
    } | ConvertTo-Json -Compress
    $env:POSTGRES_SERVER = "127.0.0.1"
    $env:POSTGRES_PORT = "$dbPort"
    $env:POSTGRES_DB = "textprocessor"
    $env:POSTGRES_USER = "postgres"
    $env:POSTGRES_PASSWORD = "postgres"
    $env:CELERY_BROKER_URL = "redis://127.0.0.1:$redisPort/0"
    $env:SECRET_KEY = "stack-$runId-secret-key-with-at-least-32-bytes"
    $env:FIRST_SUPERUSER = "stack-$runId@example.com"
    $env:FIRST_SUPERUSER_PASSWORD = "Stack-$runId-Passw0rd!"
    $env:MARKDOWN_CLEANING_WORKER = $workerSettings
    $env:ENVIRONMENT = "local"
    $env:PYTHONUTF8 = "1"

    Push-Location $backendRoot
    try {
        & uv run alembic upgrade head
        if ($LASTEXITCODE -ne 0) { throw "Alembic migration failed" }
        & uv run python app/initial_data.py
        if ($LASTEXITCODE -ne 0) { throw "Initial user creation failed" }
    } finally {
        Pop-Location
    }

    $api = Start-OwnedProcess "api" @("run", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "$apiPort") $apiPort
    $worker = Start-OwnedProcess "worker-1" @("run", "celery", "-A", "app.core.celery_app:celery_app", "worker", "-P", "solo", "-Q", "markdown_cleaning", "--loglevel=INFO", "--hostname=md-worker-$runId@%h")
    $beat = Start-OwnedProcess "beat" @("run", "celery", "-A", "app.core.celery_app:celery_app", "beat", "--loglevel=INFO", "--pidfile=", "--schedule", (Join-Path $tempRoot "beat-schedule"))
    Write-Host "MARKDOWN_CLEANING_STACK_STARTED runId=$runId apiPort=$apiPort manifest=$ownershipManifest pids=$($ownedProcessRecords.pid -join ',')"
    $apiBase = "http://127.0.0.1:$apiPort"
    Wait-Http "$apiBase/api/v1/utils/health-check/"
    Start-Sleep -Seconds 3
    $headers = @{ Authorization = "Bearer $(Get-Token $apiBase)" }

    $fixture = Join-Path $backendRoot "tests/fixtures/markdown_cleaning/v1/case-duplicate-redact"
    $source = Join-Path $inputRoot "canonical.md"
    $target = Join-Path $outputRoot "canonical.cleaned.md"
    Copy-Item (Join-Path $fixture "input.md") $source
    $accepted = Submit-Task $apiBase $headers "stack-$runId" "canonical" $source $target
    $completed = Wait-Task $apiBase $headers $accepted.taskId
    Assert-True ($completed.status -eq "succeeded") "canonical task failed: $($completed.error | ConvertTo-Json -Compress)"
    Assert-True ($completed.result.targetPath -eq $target) "API did not return the business targetPath"
    $expectedBytes = [IO.File]::ReadAllBytes((Join-Path $fixture "expected.md"))
    $actualBytes = [IO.File]::ReadAllBytes($target)
    Assert-True ([Convert]::ToHexString($actualBytes) -eq [Convert]::ToHexString($expectedBytes)) "output differs byte-for-byte from canonical expected.md"
    $expectedSummary = Get-Content -Raw (Join-Path $fixture "summary.json") | ConvertFrom-Json
    $summary = $completed.result.summary
    Assert-True ($summary.duplicateParagraphsRemoved -eq $expectedSummary.duplicate_paragraphs_removed) "duplicate statistic mismatch"
    Assert-True ($summary.redactions.phone -eq $expectedSummary.phone_redactions) "phone statistic mismatch"
    Assert-True ($summary.redactions.idCard -eq $expectedSummary.id_card_redactions) "idCard statistic mismatch"
    Assert-True ($summary.redactions.bankCard -eq $expectedSummary.bank_card_redactions) "bankCard statistic mismatch"
    Assert-True ($summary.redactions.email -eq $expectedSummary.email_redactions) "email statistic mismatch"
    Assert-True ($summary.redactions.ipv4 -eq $expectedSummary.ipv4_redactions) "ipv4 statistic mismatch"
    Assert-True ($summary.formattingChanges -eq $expectedSummary.formatting_changes) "formatting statistic mismatch"
    Assert-True ((Query-Db "select status || ':' || attempt_count from markdown_cleaning_task where id='$($accepted.taskId)'") -eq "succeeded:1") "DB status or attempt count mismatch"
    Start-Sleep -Milliseconds 500
    $canonicalFiles = @(Get-ChildItem $outputRoot -File)
    Assert-True ($canonicalFiles.Count -eq 1 -and $canonicalFiles[0].FullName -eq $target) "happy path produced unexpected files: $($canonicalFiles.Name -join ',')"

    $duplicate = Submit-Task $apiBase $headers "stack-$runId" "canonical" $source $target
    Assert-True ($duplicate.taskId -eq $accepted.taskId) "idempotent POST returned a different task"
    Push-Location $backendRoot
    try {
        & uv run python -c "from app.core.celery_app import celery_app; celery_app.send_task('markdown_cleaning.execute', kwargs={'taskId':'$($accepted.taskId)','taskType':'markdown_cleaning','schemaVersion':1}, queue='markdown_cleaning')" | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "duplicate broker delivery failed" }
    } finally {
        Pop-Location
    }
    Start-Sleep -Seconds 2
    Assert-True ((Query-Db "select attempt_count from markdown_cleaning_task where id='$($accepted.taskId)'") -eq "1") "duplicate delivery reran a completed task"

    $conflictSource = Join-Path $inputRoot "conflict.md"
    $conflictTarget = Join-Path $outputRoot "preexisting.md"
    Copy-Item (Join-Path $fixture "input.md") $conflictSource
    [IO.File]::WriteAllText($conflictTarget, "DO-NOT-OVERWRITE", [Text.UTF8Encoding]::new($false))
    $conflict = Submit-Task $apiBase $headers "stack-$runId" "conflict" $conflictSource $conflictTarget
    $conflictDone = Wait-Task $apiBase $headers $conflict.taskId
    Assert-True ($conflictDone.status -eq "failed" -and $conflictDone.error.code -eq "OUTPUT_CONFLICT") "pre-existing target was not rejected"
    Assert-True (([IO.File]::ReadAllText($conflictTarget) -eq "DO-NOT-OVERWRITE")) "pre-existing target was overwritten"
    Assert-True ($conflictDone.result -eq $null) "failed API response leaked an internal result path"

    # Create a real running lease, terminate its worker, and rely on the real beat recovery task.
    $recoverySource = Join-Path $inputRoot "recovery.md"
    $recoveryTarget = Join-Path $outputRoot "recovery.cleaned.md"
    $largeText = "# Worker 恢复验收`n`n" + ("恢复验收内容" * 50000) + "`n"
    [IO.File]::WriteAllText($recoverySource, $largeText, [Text.UTF8Encoding]::new($false))
    $recovery = Submit-Task $apiBase $headers "stack-$runId" "recovery" $recoverySource $recoveryTarget
    $runningDeadline = (Get-Date).AddSeconds(30)
    do {
        $state = Query-Db "select status from markdown_cleaning_task where id='$($recovery.taskId)'"
        if ($state -ne "running") { Start-Sleep -Milliseconds 50 }
    } while ($state -ne "running" -and (Get-Date) -lt $runningDeadline)
    Assert-True ($state -eq "running") "worker did not acquire the recovery task lease"
    Stop-OwnedProcess $worker
    Assert-True (-not (Test-Path $recoveryTarget)) "crashed worker published a final output"
    Start-Sleep -Seconds 6
    $worker2 = Start-OwnedProcess "worker-2" @("run", "celery", "-A", "app.core.celery_app:celery_app", "worker", "-P", "solo", "-Q", "markdown_cleaning", "--loglevel=INFO", "--hostname=md-worker-recovery-$runId@%h")
    Start-Sleep -Seconds 2
    $recovered = Wait-Task $apiBase $headers $recovery.taskId
    Assert-True ($recovered.status -eq "succeeded") "beat did not recover the expired running lease: $($recovered.error | ConvertTo-Json -Compress)"
    Assert-True (([int](Query-Db "select attempt_count from markdown_cleaning_task where id='$($recovery.taskId)'")) -eq 2) "recovered task was not claimed exactly twice"
    Assert-True ($recovered.result.targetPath -eq $recoveryTarget) "recovered API result did not preserve business targetPath"
    Assert-True (@(Get-ChildItem $outputRoot -File | Where-Object Name -eq "recovery.cleaned.md").Count -eq 1) "recovery produced multiple final outputs"

    $imageVersions = @(
        (& docker inspect --format '{{.Config.Image}}' $dbContainer),
        (& docker inspect --format '{{.Config.Image}}' $redisContainer)
    ) -join ", "
    $elapsed = [math]::Round(((Get-Date) - $startedAt).TotalSeconds, 2)
    Write-Host "MARKDOWN_CLEANING_STACK_OK runId=$runId tasks=3 canonical=$($accepted.taskId) recovery=$($recovery.taskId) elapsedSeconds=$elapsed images=[$imageVersions]"
} catch {
    Write-Error "MARKDOWN_CLEANING_STACK_FAILED runId=$runId error=$($_.Exception.Message) logs=$($logs -join ',')"
    exit 1
} finally {
    foreach ($process in $processes) { Stop-OwnedProcess $process }
    & docker rm -f $dbContainer $redisContainer *> $null
    if (Test-Path $tempRoot) { Remove-Item -LiteralPath $tempRoot -Recurse -Force }
}
