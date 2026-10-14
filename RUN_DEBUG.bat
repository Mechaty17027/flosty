@echo off
setlocal EnableExtensions DisableDelayedExpansion
pushd "%~dp0" >nul 2>nul
if errorlevel 1 goto folder_error
if not exist ".venv\Scripts\python.exe" goto setup_error
".venv\Scripts\python.exe" "main.py"
set "MECHAHUB_EXIT=%errorlevel%"
popd
if not "%MECHAHUB_EXIT%"=="0" (
    echo.
    echo Error occurred. Copy the error above for debugging.
    pause
)
exit /b %MECHAHUB_EXIT%
:setup_error
echo Please run INSTALL_FIRST.bat first.
popd
pause
exit /b 1
:folder_error
echo Cannot open the MechaHub project folder.
pause
exit /b 1
