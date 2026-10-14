@echo off
setlocal EnableExtensions DisableDelayedExpansion
pushd "%~dp0" >nul 2>nul
if errorlevel 1 goto folder_error
title MechaHub - First time setup

echo ===========================================
echo   MechaHub: first-time installation
echo ===========================================
echo.
if exist ".venv\Scripts\python.exe" goto install
where py >nul 2>nul
if errorlevel 1 goto try_python
py -3 -c "import sys; raise SystemExit(sys.version_info < (3, 10))" >nul 2>nul
if errorlevel 1 goto try_python
py -3 -m venv .venv
if errorlevel 1 goto fail
goto install
:try_python
python -c "import sys; raise SystemExit(sys.version_info < (3, 10))" >nul 2>nul
if errorlevel 1 goto fail
python -m venv .venv
if errorlevel 1 goto fail
:install
".venv\Scripts\python.exe" -c "import sys; raise SystemExit(sys.version_info < (3, 10))" >nul 2>nul
if errorlevel 1 goto fail
".venv\Scripts\python.exe" -m pip --version >nul 2>nul
if not errorlevel 1 goto dependencies
".venv\Scripts\python.exe" -m ensurepip --upgrade
if errorlevel 1 goto fail
:dependencies
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto fail
".venv\Scripts\python.exe" -c "import PySide6.QtWidgets, psutil"
if errorlevel 1 goto fail
echo.
echo SETUP COMPLETE. Run RUN_MECHAHUB.bat next.
popd
pause
exit /b 0
:fail
echo.
echo Setup failed. Python 3.10+ and an Internet connection are required.
echo Check the errors above. Install Python with "Add Python to PATH" enabled if needed.
popd
pause
exit /b 1
:folder_error
echo Cannot open the MechaHub project folder.
pause
exit /b 1
