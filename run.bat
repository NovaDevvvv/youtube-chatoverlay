@echo off
cd /d "%~dp0"
set "PYTHONPATH=%~dp0src"
py -m youtube_chat_overlay
if errorlevel 1 pause
