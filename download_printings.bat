@echo off
cd /d "%~dp0"
echo Downloading a small picture of every card printing (once, ~15 min)...
python build_visual_index.py --all-printings > data\vis\printings_download.log 2>&1
echo Finished.
