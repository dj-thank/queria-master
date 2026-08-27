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
if "%~2"=="" (
  .venv\Scripts\python.exe public_data_enricher.py prepare "%~1" --replace
) else (
  .venv\Scripts\python.exe public_data_enricher.py prepare "%~1" --sheet "%~2" --replace
)
if errorlevel 1 exit /b 1
.venv\Scripts\python.exe public_data_enricher.py make-assignment --output input\corporate-number-assignment.csv --chunk-size 10000
