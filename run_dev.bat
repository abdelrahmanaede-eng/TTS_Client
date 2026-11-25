@echo off
setlocal
echo Starting TimeTracker in development mode...

cd /d "%~dp0"
python -m pip install -r requirements.txt --quiet
python main.py

endlocal
