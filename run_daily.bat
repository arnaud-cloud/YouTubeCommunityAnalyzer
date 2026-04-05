@echo off
cd /d "%~dp0"
python run_daily.py >> collector.log 2>&1
