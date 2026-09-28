@echo off
title OutreachAgent Dashboard
echo Starting OutreachAgent Dashboard...
cd /d "%~dp0"
call .venv\Scripts\activate.bat
start http://127.0.0.1:8000
python -m uvicorn server:app --host 127.0.0.1 --port 8000
pause
