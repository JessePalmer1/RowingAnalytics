@echo off
REM Double-click this, or run `run` from a terminal, to start the race replay UI.
REM Any arguments are passed through, e.g.  run -Sync
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\run.ps1" %*
if errorlevel 1 pause
