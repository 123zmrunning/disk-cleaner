@echo off
setlocal
cd /d "%~dp0"
if errorlevel 1 goto directory_error
title Disk Cleaner

if not exist "main.py" goto missing_files
if not exist "requirements.txt" goto missing_files
python --version >nul 2>&1
if errorlevel 1 goto missing_python

echo Checking dependencies...
python -m pip show customtkinter >nul 2>&1
if not errorlevel 1 goto launch
python -m pip install -r requirements.txt
if errorlevel 1 goto dependency_error

:launch
echo Starting Disk Cleaner...
python main.py
if errorlevel 1 goto launch_error
exit /b 0

:directory_error
echo [ERROR] Cannot access the project directory.
goto failed
:missing_files
echo [ERROR] Keep this BAT file next to main.py and requirements.txt.
goto failed
:missing_python
echo [ERROR] Python was not found. Install Python 3.8+ and add it to PATH.
goto failed
:dependency_error
echo [ERROR] Dependency installation failed. See the error above.
goto failed
:launch_error
echo [ERROR] The app exited with an error. See the traceback above.
:failed
if /i not "%~1"=="--no-pause" pause
exit /b 1
