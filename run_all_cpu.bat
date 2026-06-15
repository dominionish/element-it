@echo off
setlocal
cd /d "%~dp0"
echo Starting n8n and Whisper Transcriber API with CPU...
where docker >nul 2>nul
if errorlevel 1 (
  echo ERROR: Docker not found. Install Docker Desktop and start it.
  pause
  exit /b 1
)
docker compose version >nul 2>nul
if errorlevel 1 (
  echo ERROR: Docker Compose plugin not available.
  echo Try updating Docker Desktop.
  pause
  exit /b 1
)
docker compose -f docker-compose.cpu.yml up
pause
