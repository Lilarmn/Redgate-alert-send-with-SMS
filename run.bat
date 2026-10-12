@echo off
title Redgate Alert to SMS Relay
chcp 65001 >nul
cd /d "%~dp0"

echo ======================================================================
echo   Redgate Alert to SMS Relay Service
echo ======================================================================
echo   Checking Python environment and dependencies...
echo ======================================================================

:: 1. Check Python executable
set "PY_EXE=python"
where python >nul 2>nul
if %errorlevel% equ 0 goto :PYTHON_FOUND

if exist "%LOCALAPPDATA%\Programs\Python\Python314\python.exe" (
    set "PY_EXE=%LOCALAPPDATA%\Programs\Python\Python314\python.exe"
    goto :PYTHON_FOUND
)
if exist "C:\Program Files\Python314\python.exe" (
    set "PY_EXE=C:\Program Files\Python314\python.exe"
    goto :PYTHON_FOUND
)
if exist "relay_service\.venv\Scripts\python.exe" (
    set "PY_EXE=relay_service\.venv\Scripts\python.exe"
    goto :PYTHON_FOUND
)

echo.
echo [ERROR] Python is not installed on this server!
echo ----------------------------------------------------------------------
echo Please run the installer in the "python installer" folder:
echo    python-3.14.0-amd64.exe
echo.
echo ** Important **: Check "[x] Add python.exe to PATH" during installation.
echo ----------------------------------------------------------------------
echo After installation finishes, run this file [run.bat] again.
echo.
pause
exit /b 1

:PYTHON_FOUND
echo [+] Python executable: %PY_EXE%

:: 2. Auto-check & auto-install offline packages from wheelhouse (100% Offline)
"%PY_EXE%" -c "import flask, waitress, pyodbc" >nul 2>nul
if %errorlevel% equ 0 goto :PACKAGES_OK

echo.
echo [*] Installing required Python packages from offline wheelhouse...
echo    Please wait a few seconds...
echo.
"%PY_EXE%" -m pip install --no-index --find-links="%~dp0relay_service\wheelhouse" -r "%~dp0relay_service\requirements.txt"
if %errorlevel% neq 0 (
    echo.
    echo [ERROR] Offline package installation failed!
    echo Please make sure wheel files exist in "relay_service\wheelhouse".
    pause
    exit /b 1
)
echo [+] All packages installed successfully offline!
echo.

:PACKAGES_OK
echo [+] Python dependencies verified.

:: 3. Launch browser automatically after 2 seconds
start "" cmd /c "timeout /t 2 /nobreak >nul 2>nul && start http://localhost:8000"

:: 4. Run the relay server
echo [*] Starting Relay Server on http://localhost:8000 ...
cd /d "%~dp0relay_service"
"%PY_EXE%" server.py config.json

:: 5. Handle unexpected exit / errors
if %errorlevel% equ 0 goto :DONE

echo.
echo ======================================================================
echo [ERROR] Relay service stopped with exit code: %errorlevel%
echo ======================================================================
echo Troubleshooting steps:
echo  1. Check if another application is already using port 8000.
echo  2. Verify that SQL Server service is running on the server.
echo  3. Review full error details in log file:
echo     relay_service\logs\relay.log
echo ======================================================================
echo.

:DONE
pause