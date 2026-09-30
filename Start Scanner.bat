@echo off
cd /d "%~dp0"
if not exist data\cards.db python update_db.py
python scanner.py %*
if errorlevel 1 pause
