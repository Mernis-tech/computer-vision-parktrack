@echo off
chcp 65001 >nul
cd /d "%~dp0"

docker info >nul 2>&1
if errorlevel 1 (
  echo Docker Desktop не запущен. Запустите Docker Desktop и повторите попытку.
  pause
  exit /b 1
)

echo Сборка и запуск ParkTrack. Первый запуск может занять 10-30 минут.
docker compose up --build -d
if errorlevel 1 (
  echo Не удалось запустить ParkTrack. Откройте logs-parktrack.bat для просмотра ошибок.
  pause
  exit /b 1
)

echo Ожидание API...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$limit=(Get-Date).AddMinutes(5); do { try { $r=Invoke-WebRequest -UseBasicParsing -TimeoutSec 3 http://127.0.0.1:8000/api/v1/health; if ($r.StatusCode -eq 200) { exit 0 } } catch {}; Start-Sleep -Seconds 3 } while ((Get-Date) -lt $limit); exit 1"

if errorlevel 1 (
  echo API ещё не готов. Посмотрите состояние через status-parktrack.bat.
) else (
  echo ParkTrack запущен.
  echo Панель: http://127.0.0.1:5173
  echo API:     http://127.0.0.1:8000/docs
  start "" http://127.0.0.1:5173
  start "" http://127.0.0.1:8000/docs
)

pause
