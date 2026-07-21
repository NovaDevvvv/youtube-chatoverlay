@echo off
cd /d "%~dp0"
set "PYTHONPATH=%~dp0src"
start "" "%~dp0runtime\pythonw.exe" -m youtube_chat_overlay

