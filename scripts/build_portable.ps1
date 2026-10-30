# Build WinFirst as a portable PyInstaller executable.
#
#   powershell -ExecutionPolicy Bypass -File scripts\build_portable.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\build_portable.ps1 -OneDir
#   powershell -ExecutionPolicy Bypass -File scripts\build_portable.ps1 -SkipSmoke
#
# Output:
#   dist\WinFirst.exe            (one-file, default)
#   dist\WinFirst\WinFirst.exe   (-OneDir)
#
# The script regenerates the icon, performs a clean build and runs the packaged
# smoke test, which constructs all 24 pages without applying any Windows change.

[CmdletBinding()]
param(
    [switch]$OneDir,
    [switch]$SkipSmoke
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Push-Location $Root

try {
    if (-not (Test-Path "src\winfirst\resources\icons\winfirst.ico")) {
        Write-Host "Generating application icon..."
        $env:QT_QPA_PLATFORM = "offscreen"
        python scripts\make_icon.py
        if ($LASTEXITCODE -ne 0) { throw "Icon generation failed." }
        Remove-Item Env:\QT_QPA_PLATFORM -ErrorAction SilentlyContinue
    }

    if ($OneDir) {
        $env:WINFIRST_ONEFILE = "0"
    } else {
        Remove-Item Env:\WINFIRST_ONEFILE -ErrorAction SilentlyContinue
    }

    $env:PYTHONPATH = "src"
    Write-Host "Running PyInstaller (onefile=$(-not $OneDir))..."
    python -m PyInstaller --noconfirm --clean WinFirst.spec
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed with exit code $LASTEXITCODE." }

    $Exe = if ($OneDir) { "dist\WinFirst\WinFirst.exe" } else { "dist\WinFirst.exe" }
    if (-not (Test-Path $Exe)) { throw "Build did not produce $Exe." }

    $Info = Get-Item $Exe
    Write-Host ("Built {0} ({1:N1} MB)" -f $Info.FullName, ($Info.Length / 1MB))

    if (-not $SkipSmoke) {
        Write-Host "Running packaged smoke test..."
        $Process = Start-Process -FilePath $Info.FullName -ArgumentList "--smoke-test" -PassThru -Wait
        if ($Process.ExitCode -ne 0) {
            throw "Packaged smoke test failed with exit code $($Process.ExitCode). See logs under %LOCALAPPDATA%\WinFirst\logs."
        }
        Write-Host "Packaged smoke test passed (all pages constructed)."
    }
}
finally {
    Pop-Location
}
