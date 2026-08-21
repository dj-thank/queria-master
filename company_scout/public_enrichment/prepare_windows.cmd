@echo off
setlocal
cd /d "%~dp0"
if "%~1"=="" (
  echo Usage: prepare_windows.cmd ^<companies.csv-or-xlsx^> [sheet-name]
  exit /b 2
)
if not exist .venv\Scripts\python.exe (
  echo Run setup_windows.cmd first.
  exit /b 1
)
set "SHEET=%~2"
if "%SHEET%"=="" set "SHEET=企業DB"
.venv\Scripts\python.exe public_data_enricher.py prepare "%~1" --sheet "%SHEET%" --replace
if errorlevel 1 exit /b 1
.venv\Scripts\python.exe public_data_enricher.py make-assignment --output input\corporate-number-assignment.csv --chunk-size 10000
