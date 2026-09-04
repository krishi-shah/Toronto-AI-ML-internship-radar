@echo off
REM Digest wrapper for Windows Task Scheduler.
REM
REM This exists because schtasks cannot parse a /tr value that contains BOTH a
REM quoted path with a space AND an argument -- "...\run_radar.cmd" --digest
REM fails with "Invalid argument/option". A separate argument-free wrapper
REM sidesteps the quoting entirely.

call "%~dp0run_radar.cmd" --digest
exit /b %ERRORLEVEL%
