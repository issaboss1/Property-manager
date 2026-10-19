@echo off
cd /d "%~dp0"
if not exist .env copy .env.example .env >nul
python -m pip install -q -r requirements.txt
start "" http://localhost:5000
python app.py
pause
