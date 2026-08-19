@echo off
chcp 65001 > nul
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0refresh.ps1"
if errorlevel 1 (
  echo.
  echo 更新に失敗しました。上のエラー内容を確認してください。
  pause
  exit /b 1
)
echo.
echo 更新が完了しました。
pause
