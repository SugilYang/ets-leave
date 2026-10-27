@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist logs mkdir logs
python leave_sync.py >> "logs\%date:~0,4%-%date:~5,2%.log" 2>&1
