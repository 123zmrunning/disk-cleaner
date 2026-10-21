@echo off
chcp 65001 >nul
title 磁盘清理助手 v3.1 - 打包为 EXE

echo.
echo  ╔══════════════════════════════════════════╗
echo  ║     磁盘清理助手 v3.1  打包工具          ║
echo  ║     将程序打包为独立 .EXE，无需Python    ║
echo  ╚══════════════════════════════════════════╝
echo.

REM ── 检查 Python ─────────────────────────────────────────────────
python --version >nul 2>&1
if errorlevel 1 (
    echo  [错误] 未检测到 Python！
    echo.
    echo  打包过程本身需要 Python，但打包完成后生成的 EXE 不需要 Python。
    echo  请先安装 Python 3.8+：https://www.python.org/downloads/
    echo  安装时务必勾选 "Add Python to PATH"
    echo.
    pause
    exit /b 1
)

echo  [1/3] 正在安装 PyInstaller...
pip install pyinstaller customtkinter -q
if errorlevel 1 (
    echo  [错误] 安装失败，请检查网络连接或 pip 权限。
    pause
    exit /b 1
)
echo        安装完成。
echo.

echo  [2/3] 正在打包（约需 1~3 分钟，请耐心等待）...
echo.
pyinstaller ^
  --noconfirm ^
  --onedir ^
  --windowed ^
  --collect-all customtkinter ^
  --name "磁盘清理助手_v3.1" ^
  main.py

if errorlevel 1 (
    echo.
    echo  [错误] 打包失败！请查看上方的错误信息。
    echo.
    echo  常见原因：
    echo    1. 杀毒软件拦截了 PyInstaller（临时关闭后重试）
    echo    2. 磁盘空间不足
    echo    3. 代码文件有语法错误
    echo.
    pause
    exit /b 1
)

echo.
echo  [3/3] 打包成功！
echo.
echo  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
echo   输出目录：dist\磁盘清理助手_v3.1\
echo.
echo   如何在另一台电脑上使用：
echo   将整个 dist\磁盘清理助手_v3.1\ 文件夹复制过去，
echo   双击其中的 磁盘清理助手_v3.1.exe 即可运行。
echo.
echo   注意：必须复制整个文件夹，不能只复制 .exe 文件！
echo  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
echo.

if exist "dist\磁盘清理助手_v3.1" (
    explorer "dist\磁盘清理助手_v3.1"
)
pause
