@echo off
rem summery launcher for Windows: double-click, or run "deploy.cmd --port 9000".
rem Same options as deploy.ps1. -ExecutionPolicy Bypass applies to this run only.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0deploy.ps1" %*
exit /b %ERRORLEVEL%
