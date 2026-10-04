@echo off
chcp 65001 >nul
cd /d "%~dp0"
docker compose down
echo ParkTrack остановлен. Данные сохранены.
pause
