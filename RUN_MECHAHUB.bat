@echo off
setlocal EnableExtensions DisableDelayedExpansion
pushd "%~dp0" >nul 2>nul
if errorlevel 1 goto folder_error
if not exist ".venv\Scripts\python.exe" goto setup_error
if not exist ".venv\Scripts\pythonw.exe" goto setup_error
".venv\Scripts\python.exe" -c "import PySide6.QtWidgets, psutil" >nul 2>nul
if errorlevel 1 goto setup_error
start "" ".venv\Scripts\pythonw.exe" "main.py"
if errorlevel 1 goto launch_error
popd
exit /b 0
:setup_error
    echo Please run INSTALL_FIRST.bat before running MechaHub.
goto fail
:launch_error
echo MechaHub could not start. Run RUN_DEBUG.bat to see the error.
goto fail
:folder_error
echo Cannot open the MechaHub project folder.
pause
exit /b 1
:fail
popd
pause
exit /b 1
