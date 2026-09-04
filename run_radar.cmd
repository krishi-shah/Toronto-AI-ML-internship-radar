@echo off
REM Wrapper for Windows Task Scheduler.
REM
REM Two things this exists to handle:
REM   1. Task Scheduler starts tasks in C:\Windows\System32, so radar.db and
REM      companies.py would not be found. %~dp0 is this file's own folder.
REM   2. pythonw.exe runs with no console, so nothing flashes on screen every
REM      ten minutes -- but that also means no visible output. Everything is
REM      appended to radar.log instead, so there is somewhere to look when the
REM      alerts go quiet.
REM
REM Usage:  run_radar.cmd            normal run
REM         run_radar.cmd --digest   send the queued 5pm digest
REM         run_radar.cmd --check    per-source status

cd /d "%~dp0"

REM Prefer pythonw (windowless). Fall back to python if it is not present.
set "PY=C:\Users\krish\AppData\Local\Python\pythoncore-3.14-64\pythonw.exe"
if not exist "%PY%" set "PY=pythonw.exe"

REM Keep the log from growing without bound: past ~5 MB, start a fresh one.
if exist radar.log (
    for %%F in (radar.log) do if %%~zF GTR 5000000 (
        if exist radar.log.old del radar.log.old
        ren radar.log radar.log.old
    )
)

echo.>> radar.log
echo ===== %DATE% %TIME% :: radar.py %* =====>> radar.log

REM UTF-8: tracker listings and job titles carry emoji, and the default
REM Windows codepage cannot encode them.
set PYTHONIOENCODING=utf-8

"%PY%" radar.py %* >> radar.log 2>&1

exit /b %ERRORLEVEL%
