@echo off
cd /d "%~dp0"
docker compose up -d --build
if errorlevel 1 (
  pause
  exit /b 1
)
start "" "http://localhost:8080/"
