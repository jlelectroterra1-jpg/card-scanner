@echo off
cd /d "%~dp0"
if not exist data\cards.db python update_db.py
if not exist data\visual_index.npz (
  echo First run: downloading card pictures for picture recognition - about 15-30 minutes, once.
  python build_visual_index.py
)
python scanner.py %*
if errorlevel 1 pause
