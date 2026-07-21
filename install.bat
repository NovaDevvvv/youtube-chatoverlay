@echo off
setlocal EnableExtensions
title YouTube Live Chat Overlay - Installer
cd /d "%~dp0"

echo.
echo  ============================================================
echo             YouTube Live Chat Overlay Installer
echo  ============================================================
echo.

if not exist "src\youtube_chat_overlay\__main__.py" (
    echo [ERROR] Application files are missing.
    echo Extract the complete download before running this installer.
    goto :failed
)

set "BASE_PY="
set "BASE_ARGS="
where py.exe >nul 2>nul
if not errorlevel 1 (
    set "BASE_PY=py"
    set "BASE_ARGS=-3"
)

if not defined BASE_PY (
    where python.exe >nul 2>nul
    if not errorlevel 1 set "BASE_PY=python"
)

if not defined BASE_PY (
    echo [ERROR] Python 3.10 or newer is required.
    echo Install Python from https://www.python.org/downloads/windows/
    echo Enable "Add Python to PATH", then run install.bat again.
    goto :failed
)

echo [1/4] Checking Python...
%BASE_PY% %BASE_ARGS% -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)"
if errorlevel 1 (
    echo [ERROR] Python 3.10 or newer is required.
    goto :failed
)

if not exist ".venv\Scripts\python.exe" (
    echo [2/4] Creating the private application environment...
    %BASE_PY% %BASE_ARGS% -m venv ".venv"
    if errorlevel 1 goto :venv_failed
) else (
    echo [2/4] Private application environment already exists.
)

set "APP_PY=%CD%\.venv\Scripts\python.exe"
echo [3/4] Installing application components...
"%APP_PY%" -m pip install --disable-pip-version-check --upgrade pip
if errorlevel 1 goto :dependency_failed
"%APP_PY%" -m pip install --disable-pip-version-check -r requirements.txt
if errorlevel 1 goto :dependency_failed

echo [4/4] Verifying the installation...
set "PYTHONPATH=%CD%\src"
"%APP_PY%" -c "import youtube_chat_overlay; from youtube_chat_overlay.ui.application import MainWindow"
if errorlevel 1 goto :verify_failed

echo.
echo  Installation completed successfully.
echo  No Git client or repository checkout is required.
echo  Use run.bat to start the application.
echo.
pause
exit /b 0

:venv_failed
echo [ERROR] Could not create the private Python environment.
goto :failed

:dependency_failed
echo [ERROR] Could not install the application dependencies.
echo Check your internet connection and run install.bat again.
goto :failed

:verify_failed
echo [ERROR] Installation verification failed.
goto :failed

:failed
echo.
echo Installation was not completed.
pause
exit /b 1
