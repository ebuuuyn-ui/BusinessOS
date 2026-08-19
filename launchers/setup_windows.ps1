$ErrorActionPreference = "Stop"

$AppDir = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $AppDir

if (-not (Get-Command py -ErrorAction SilentlyContinue)) {
    throw "Python Launcher (py) bulunamadi. Python 3.10 veya daha yeni bir surum kurun."
}

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    py -3 -m venv .venv
}

& ".venv\Scripts\python.exe" -m pip install --upgrade pip
& ".venv\Scripts\python.exe" -m pip install -r requirements.txt

Write-Host "Business OS Windows kurulumu tamamlandi."
Write-Host "Baslatmak icin launchers\start_windows.bat dosyasini acin."
