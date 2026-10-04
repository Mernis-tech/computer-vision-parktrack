@echo off
chcp 65001 >nul
cd /d "%~dp0"
docker compose restart detector
docker compose logs -f --tail=100 detector
