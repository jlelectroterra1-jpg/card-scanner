@echo off
cd /d "%~dp0"
set EPOCHS=25
set PYTHONIOENCODING=utf-8
echo Training the card recognition model - about 1.5-2 hours. You can leave this window open.
python train_model.py >> data\vis\train.log 2>&1
echo Finished. See data\vis\train.log
