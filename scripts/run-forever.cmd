@echo off
rem Keeps `rhca run` going: starts it, and restarts it a minute after it stops
rem for any reason. It places only orders you accept on the dashboard. A
rem shortcut to this file in the Windows Startup folder starts it at sign-in
rem (docs/runbook.md).
rem Close this window to stop it for good.
title rhca run
cd /d "%~dp0.."
:loop
.venv\Scripts\rhca.exe run --keep-awake
echo.
echo rhca run stopped (exit code %errorlevel%). Restarting in 60 seconds; close this window to stop.
powershell -NoProfile -Command "Start-Sleep -Seconds 60"
goto loop
