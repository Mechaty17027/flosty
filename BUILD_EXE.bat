@echo off
setlocal EnableExtensions DisableDelayedExpansion
pushd "%~dp0" >nul 2>nul
if errorlevel 1 goto folder_error
if not exist ".venv\Scripts\python.exe" goto setup_error
".venv\Scripts\python.exe" -c "import PySide6.QtWidgets, psutil, cryptography"
if errorlevel 1 goto setup_error
".venv\Scripts\python.exe" -m pip check
if errorlevel 1 goto fail
".venv\Scripts\python.exe" -m pip install pyinstaller
if errorlevel 1 goto fail
".venv\Scripts\python.exe" tools\make_windows_icon.py
if errorlevel 1 goto fail
".venv\Scripts\python.exe" tools\prepare_python_runtime.py
if errorlevel 1 goto fail
".venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean --onefile --windowed --name MechaHub --icon build\MechaHub.ico --add-data "assets;assets" --add-data "build/python_runtime;python_runtime" main.py
if errorlevel 1 goto fail
if not exist "dist\MechaHub.exe" goto fail
echo.
echo DONE: dist\MechaHub.exe
popd
pause
exit /b 0
:setup_error
echo Please run INSTALL_FIRST.bat first.
goto fail
:fail
echo Failed to build .exe. Check the errors above.
popd
pause
exit /b 1
:folder_error
echo Cannot open the MechaHub project folder.
pause
exit /b 1
