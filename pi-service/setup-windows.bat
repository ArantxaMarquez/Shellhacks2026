@echo off
REM Windows setup: makes a venv and installs requirements.
REM Run from anywhere:  pi-service\setup-windows.bat  (or double-click it)

cd /d "%~dp0"

set PY=python
where python >nul 2>nul
if errorlevel 1 (
  where py >nul 2>nul
  if errorlevel 1 goto nopython
  set PY=py -3
)

echo Creating virtual environment in pi-service\venv ...
%PY% -m venv venv
if errorlevel 1 goto fail

REM Activating here only affects this script; the install below uses that venv.
call venv\Scripts\activate.bat

echo Installing requirements ...
python -m pip install --upgrade pip >nul
python -m pip install -r requirements.txt
if errorlevel 1 goto fail

echo.
echo Setup done. Start the camera service with:
echo.
echo   cd pi-service
echo   venv\Scripts\activate
echo   python camera_service.py
echo.
pause
exit /b 0

:nopython
echo Python not found. Install Python 3 from https://www.python.org/downloads/
echo and tick "Add python.exe to PATH" in the installer, then re-run this.
pause
exit /b 1

:fail
echo Setup failed. Scroll up for the error.
pause
exit /b 1
