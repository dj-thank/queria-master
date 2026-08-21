@echo off
setlocal
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
  echo Run setup_windows.cmd first.
  exit /b 1
)
.venv\Scripts\python.exe public_data_enricher.py status
