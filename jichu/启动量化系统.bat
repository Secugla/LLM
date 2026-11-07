@echo off
chcp 936 >nul
cd /d "C:\Users\86180\Desktop\LLM\jichu"
title 量化信息决策系统
if not exist ".venv\Scripts\python.exe" (
    echo [错误] 未找到虚拟环境 .venv
    echo 请先在项目目录执行：pip install -r requirements.txt
    pause
    exit /b 1
)
".venv\Scripts\python.exe" app.py
if errorlevel 1 (
    echo.
    echo [程序异常退出] 请查看上方报错信息
    pause
)