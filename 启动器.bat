@echo off
rem 双击本文件即可启动 DeepSeek Harness 启动器（图形界面）。
rem 优先用 pythonw 以免弹出黑色命令行窗口；找不到 pythonw 时退回 python。
cd /d "%~dp0"
where pythonw >nul 2>nul
if %errorlevel%==0 (
  start "" pythonw launcher.py
) else (
  start "" python launcher.py
)
