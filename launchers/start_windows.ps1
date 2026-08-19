$ErrorActionPreference = "Stop"

$AppDir = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$Python = Join-Path $AppDir ".venv\Scripts\python.exe"

if (-not (Test-Path $Python)) {
    throw "Windows sanal ortami bulunamadi. Once launchers\setup_windows.ps1 dosyasini calistirin."
}

Set-Location $AppDir
Start-Process "http://127.0.0.1:5000"
& $Python app.py
