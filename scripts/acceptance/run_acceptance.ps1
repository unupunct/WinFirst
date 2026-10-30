# Live acceptance runner for WinFirst.
#
#   powershell -ExecutionPolicy Bypass -File scripts\acceptance\run_acceptance.ps1 -ConfirmDisposableVM
#
# Changes real Windows settings. Run it only on a disposable VM.
# The script never hides a failure: the exit code and summary.json reflect the
# real pytest result, and skipped scenarios stay skipped.

[CmdletBinding()]
param(
    [ValidateSet("all", "settings", "power", "service", "task", "cleanup", "restore")]
    [string]$Scenario = "all",
    [switch]$ConfirmDisposableVM,
    [string]$HyperVVm = "",
    [string]$OutputDir = "acceptance-results",
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"

if (-not $ConfirmDisposableVM) {
    Write-Host "Refusing to run: these tests change Windows settings." -ForegroundColor Yellow
    Write-Host "Run this script only on a disposable VM and pass -ConfirmDisposableVM." -ForegroundColor Yellow
    exit 3
}

$Root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
Push-Location $Root
$ExitCode = 0
try {
    $Stamp = Get-Date -Format "yyyyMMdd-HHmmss"
    $RunDir = Join-Path $OutputDir $Stamp
    New-Item -ItemType Directory -Path $RunDir -Force | Out-Null
    $Junit = Join-Path $RunDir "results.xml"
    $EvidenceFile = Join-Path $RunDir "evidence.jsonl"
    $Transcript = Join-Path $RunDir "transcript.log"
    $SummaryFile = Join-Path $RunDir "summary.json"

    $IsAdmin = ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator
    )
    $Os = Get-CimInstance Win32_OperatingSystem
    $CheckpointName = ""
    if ($HyperVVm) {
        if (-not (Get-Command Checkpoint-VM -ErrorAction SilentlyContinue)) {
            throw "The Hyper-V PowerShell module is not available, so the VM checkpoint cannot be created."
        }
        $CheckpointName = "WinFirst-$Stamp"
        Write-Host "Creating Hyper-V checkpoint '$CheckpointName' for $HyperVVm ..."
        Checkpoint-VM -Name $HyperVVm -SnapshotName $CheckpointName
    }

    Write-Host ""
    Write-Host "WinFirst live acceptance tests - this machine will change." -ForegroundColor Yellow
    Write-Host "Computer: $env:COMPUTERNAME   OS: $($Os.Caption) $($Os.Version)" -ForegroundColor Yellow
    Write-Host "Administrator: $IsAdmin   Scenario: $Scenario" -ForegroundColor Yellow
    Write-Host "Evidence: $RunDir" -ForegroundColor Yellow
    Write-Host ""

    $env:WINFIRST_LIVE = "1"
    $env:WINFIRST_ACCEPTANCE_EVIDENCE = $EvidenceFile

    $Arguments = @(
        "-m", "pytest", "tests/live", "-m", "live",
        "-o", "addopts=", "-v",
        "--junitxml=$Junit"
    )
    if ($Scenario -ne "all") {
        $Arguments += @("-k", $Scenario)
    }

    $Start = Get-Date
    & $Python @Arguments 2>&1 | Tee-Object -FilePath $Transcript
    $ExitCode = $LASTEXITCODE
    $DurationSeconds = [math]::Round(((Get-Date) - $Start).TotalSeconds, 1)

    $Cases = @()
    if (Test-Path $Junit) {
        [xml]$Xml = Get-Content $Junit
        foreach ($Case in $Xml.testsuites.testcase) {
            $Status = "passed"
            if ($Case.failure -or $Case.error) { $Status = "failed" }
            elseif ($Case.skipped) { $Status = "skipped" }
            $Seconds = 0
            if ($Case.time) { $Seconds = [double]$Case.time }
            $Cases += [pscustomobject]@{
                name      = [string]$Case.name
                classname = [string]$Case.classname
                status    = $Status
                seconds   = $Seconds
            }
        }
    }

    $Evidence = @()
    if (Test-Path $EvidenceFile) {
        $Evidence = @(
            Get-Content $EvidenceFile |
                Where-Object { $_.Trim() } |
                ForEach-Object { $_ | ConvertFrom-Json }
        )
    }

    $Summary = [pscustomobject]@{
        tool             = "WinFirst"
        kind             = "live-acceptance"
        started_at       = $Start.ToString("o")
        duration_seconds = $DurationSeconds
        computer         = $env:COMPUTERNAME
        windows          = "$($Os.Caption) $($Os.Version) build $($Os.BuildNumber)"
        administrator    = $IsAdmin
        scenario         = $Scenario
        hyperv_checkpoint = $CheckpointName
        exit_code        = $ExitCode
        totals           = [pscustomobject]@{
            total   = $Cases.Count
            passed  = @($Cases | Where-Object { $_.status -eq "passed" }).Count
            failed  = @($Cases | Where-Object { $_.status -eq "failed" }).Count
            skipped = @($Cases | Where-Object { $_.status -eq "skipped" }).Count
        }
        cases            = $Cases
        evidence         = $Evidence
    }
    $Json = $Summary | ConvertTo-Json -Depth 8
    [System.IO.File]::WriteAllText(
        (Join-Path (Get-Location) $SummaryFile),
        $Json,
        (New-Object System.Text.UTF8Encoding($false))
    )

    Write-Host ""
    Write-Host ("Result: {0} passed, {1} failed, {2} skipped (exit {3})" -f `
        $Summary.totals.passed, $Summary.totals.failed, $Summary.totals.skipped, $ExitCode)
    Write-Host "Summary: $SummaryFile"
    if ($CheckpointName) {
        Write-Host "A Hyper-V checkpoint was created. Restore it with:" -ForegroundColor Yellow
        Write-Host "  Restore-VMCheckpoint -VMName `"$HyperVVm`" -Name `"$CheckpointName`"" -ForegroundColor Yellow
    }
}
catch {
    Write-Host "Acceptance run failed: $($_.Exception.Message)" -ForegroundColor Red
    $ExitCode = 1
}
finally {
    Pop-Location
}

exit $ExitCode
