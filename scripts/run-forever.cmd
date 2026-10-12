@echo off
rem Keeps `rhca run` (shadow mode: it never orders) going: starts it, and
rem restarts it a minute after it stops for any reason. A shortcut to this file
rem in the Windows Startup folder starts it at sign-in (docs/runbook.md).
rem Close this window to stop it for good.
title rhca run (shadow mode)
cd /d "%~dp0.."
:loop
.venv\Scripts\rhca.exe run --keep-awake
echo.
echo rhca run stopped (exit code %errorlevel%). Restarting in 60 seconds; close this window to stop.
powershell -NoProfile -Command "Start-Sleep -Seconds 60"
goto loop
