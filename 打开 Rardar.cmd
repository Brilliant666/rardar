@echo off
cd /d "%~dp0"
python -m pipeline.runtime start --open
if errorlevel 1 pause
