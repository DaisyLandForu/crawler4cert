@echo off
chcp 65001 >nul
cd /d "%~dp0"
call conda run -n autopapercrawl --no-capture-output python -m autopapercrawl serve
if errorlevel 1 pause
