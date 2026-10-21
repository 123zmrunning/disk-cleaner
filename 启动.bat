@echo off
chcp 65001 >nul
title 磁盘清理助手 v3.1

echo  正在检查依赖...
pip show customtkinter >nul 2>&1
if errorlevel 1 (
    echo  正在安装 customtkinter，首次启动需要联网，请稍候...
    pip install customtkinter -q
)

echo  正在启动...
python main.py

if errorlevel 1 (
    echo.
    echo  [错误] 程序异常退出，请检查 Python 是否正确安装。
    echo  下载地址：https://www.python.org/downloads/
    pause
)
