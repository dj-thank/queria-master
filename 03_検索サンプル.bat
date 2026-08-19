@echo off
chcp 65001 > nul
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo 先に 01_初回セットアップ.bat を実行してください。
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -m queria_master search --prefecture 東京都 --has-web --limit 50
pause
