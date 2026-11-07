@echo off
setlocal
cd /d "%~dp0"
if errorlevel 1 goto directory_error
title Disk Cleaner - Build EXE

if not exist "main.py" goto missing_files
if not exist "requirements.txt" goto missing_files
python --version >nul 2>&1
if errorlevel 1 goto missing_python

echo [1/2] Installing build dependencies...
python -m pip install -r requirements.txt pyinstaller
if errorlevel 1 goto dependency_error

echo [2/2] Building EXE...
python -m PyInstaller --noconfirm --onedir --windowed --collect-all customtkinter --name DiskCleaner main.py
if errorlevel 1 goto build_error
if not exist "dist\DiskCleaner\DiskCleaner.exe" goto build_error

echo.
echo [OK] EXE: "%CD%\dist\DiskCleaner\DiskCleaner.exe"
echo Copy the ENTIRE dist\DiskCleaner folder when moving the app.
if /i not "%~1"=="--no-pause" pause
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
:build_error
echo [ERROR] EXE build failed. See the error above.
:failed
if /i not "%~1"=="--no-pause" pause
exit /b 1
