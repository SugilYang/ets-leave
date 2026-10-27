@echo off
chcp 65001 >nul
cd /d "%~dp0"
title ETS 연차 자동반영
:again
python leave_sync.py
echo 프로그램이 멈췄습니다. 10초 후 다시 시작합니다.
timeout /t 10 >nul
goto again
