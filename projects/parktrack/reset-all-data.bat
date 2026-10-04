@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ВНИМАНИЕ: будут удалены локальная база ParkTrack и все снимки MinIO.
set /p answer=Для подтверждения напишите DELETE: 
if /I not "%answer%"=="DELETE" (
  echo Отмена.
  pause
  exit /b 0
)
docker compose down -v
echo Локальные данные удалены. Для чистого запуска используйте start-parktrack.bat.
pause
