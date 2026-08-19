@echo off
chcp 65001 > nul
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0bootstrap.ps1"
if errorlevel 1 (
  echo.
  echo セットアップに失敗しました。上のエラー内容を確認してください。
  pause
  exit /b 1
)
echo.
echo セットアップが完了しました。
pause
