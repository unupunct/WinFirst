# Build the WinFirst installer with Inno Setup 6.3+.
#
#   powershell -ExecutionPolicy Bypass -File scripts\build_installer.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\build_installer.ps1 -SkipBuild
#   powershell -ExecutionPolicy Bypass -File scripts\build_installer.ps1 -Iscc "C:\path\ISCC.exe"
#
# Requires dist\WinFirst.exe (one-file build) unless -SkipBuild is used.
# If Inno Setup is not installed the script exits with code 2 and explains how
# to install it; it never pretends the installer was built.

[CmdletBinding()]
param(
    [string]$Iscc = "",
    [switch]$SkipBuild
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Push-Location $Root

try {
    if (-not $SkipBuild) {
        & (Join-Path $PSScriptRoot "build_portable.ps1")
        if ($LASTEXITCODE -ne 0) { throw "Portable build failed." }
    }

    if (-not (Test-Path "dist\WinFirst.exe")) {
        throw "dist\WinFirst.exe was not found. Run the portable build first."
    }

    $Candidates = @()
    if ($Iscc) { $Candidates += $Iscc }
    $Command = Get-Command "ISCC.exe" -ErrorAction SilentlyContinue
    if ($Command) { $Candidates += $Command.Source }
    $Candidates += @(
        (Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6\ISCC.exe"),
        (Join-Path ${env:ProgramFiles(x86)} "Inno Setup 6\ISCC.exe"),
        (Join-Path $env:ProgramFiles "Inno Setup 6\ISCC.exe")
    )

    $Compiler = $null
    foreach ($Candidate in $Candidates) {
        if ($Candidate -and (Test-Path $Candidate)) { $Compiler = $Candidate; break }
    }

    if (-not $Compiler) {
        Write-Error @"
Inno Setup 6 was not found.

Install it from https://jrsoftware.org/isinfo.php or with:
    winget install --id JRSoftware.InnoSetup -e

Then rerun this script, or pass -Iscc with the full path to ISCC.exe.
The portable executable is already built; only installer compilation is missing.
"@
        exit 2
    }

    Write-Host "Compiling installer with $Compiler ..."
    & $Compiler "installer\winfirst.iss"
    if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed with exit code $LASTEXITCODE." }

    $Output = Get-ChildItem "installer\Output\WinFirst-Setup-*.exe" |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if (-not $Output) { throw "Inno Setup completed but no installer was found." }
    Write-Host ("Built {0} ({1:N1} MB)" -f $Output.FullName, ($Output.Length / 1MB))
}
finally {
    Pop-Location
}
