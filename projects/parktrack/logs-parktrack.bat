@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo Для выхода из просмотра нажмите Ctrl+C.
docker compose logs -f --tail=150 api bootstrap detector admin-panel minio
