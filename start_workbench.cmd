@echo off
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul
cd /d "%~dp0"
set "WORKBENCH_PYTHON="

if exist ".venv\Scripts\python.exe" call :try_python ".venv\Scripts\python.exe"
if defined WORKBENCH_PYTHON goto run
if exist "venv\Scripts\python.exe" call :try_python "venv\Scripts\python.exe"
if defined WORKBENCH_PYTHON goto run
if defined VIRTUAL_ENV if exist "%VIRTUAL_ENV%\Scripts\python.exe" call :try_python "%VIRTUAL_ENV%\Scripts\python.exe"
if defined WORKBENCH_PYTHON goto run
if defined CONDA_PREFIX if exist "%CONDA_PREFIX%\python.exe" call :try_python "%CONDA_PREFIX%\python.exe"
if defined WORKBENCH_PYTHON goto run
where py >nul 2>&1
if not errorlevel 1 call :try_python py -3.11
if defined WORKBENCH_PYTHON goto run
where python >nul 2>&1
if not errorlevel 1 call :try_python python
if defined WORKBENCH_PYTHON goto run
where conda >nul 2>&1
if not errorlevel 1 call :try_python conda run -n autopapercrawl --no-capture-output python
if defined WORKBENCH_PYTHON goto run

echo No usable Python found. Python 3.11 or newer, requests and pypdf are required.
echo Conda is optional. Install Python 3.11, then run these commands in this folder:
echo.
echo   py -3.11 -m venv .venv
echo   .venv\Scripts\python.exe -m pip install -r requirements.txt
echo.
echo Then double-click start_workbench.cmd again.
if "%~1"=="" pause
exit /b 1

:try_python
call %* -c "import sys; import requests, pypdf; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>&1
if errorlevel 1 exit /b 0
set "WORKBENCH_PYTHON=%*"
exit /b 0

:run
echo Project: %CD%
echo Python: %WORKBENCH_PYTHON%
echo Default URL: http://127.0.0.1:8767
call %WORKBENCH_PYTHON% -m autopapercrawl serve --port 8767 %*
set "WORKBENCH_EXIT_CODE=%ERRORLEVEL%"
if not "%WORKBENCH_EXIT_CODE%"=="0" if "%~1"=="" pause
exit /b %WORKBENCH_EXIT_CODE%
