@echo off
setlocal

set "APP_DIR=C:\BusinessOS"
set "PORT=5000"

if not exist "%APP_DIR%\app.py" (
  echo Business OS uygulama klasoru bulunamadi: %APP_DIR%
  pause
  exit /b 1
)

if not exist "%APP_DIR%\.venv\Scripts\python.exe" (
  echo Python sanal ortami bulunamadi: %APP_DIR%\.venv
  pause
  exit /b 1
)

cd /d "%APP_DIR%"
start "" cmd /c "timeout /t 2 /nobreak >nul & start http://127.0.0.1:%PORT%"

echo Business OS baslatiliyor: http://127.0.0.1:%PORT%
echo Programi durdurmak icin bu pencerede Ctrl+C tuslarina basin.

"%APP_DIR%\.venv\Scripts\python.exe" app.py

endlocal
