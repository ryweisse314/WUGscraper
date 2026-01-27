@echo off
setlocal

REM Always run from the folder this .bat lives in
cd /d "%~dp0"

REM Optional: if you use a venv in this folder, auto-activate it
if exist ".venv\Scripts\activate.bat" (
  call ".venv\Scripts\activate.bat"
) else if exist "venv\Scripts\activate.bat" (
  call "venv\Scripts\activate.bat"
)

REM Run the script
python -u app.py

REM Keep the window open if there was an error
if errorlevel 1 (
  echo.
  echo Script exited with errorlevel %errorlevel%.
  pause
)

endlocal