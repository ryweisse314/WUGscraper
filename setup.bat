@echo off
echo Installing Python dependencies...
python -m pip install --upgrade pip
python -m pip install gradio pywin32
python -m pip install playwright
python -m playwright install
echo Done!
pause