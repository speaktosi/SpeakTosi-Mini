@echo off
REM ============================================================
REM  SpeakTosi - fix absolute paths after unzip/move
REM  Run this ONCE if the app was NOT unzipped to C:\Speaktosi
REM  (idempotent: safe to run multiple times; auto-backs up *.pathbak)
REM ============================================================
setlocal
cd /d "%~dp0..\.."
echo [SpeakTosi] root = %CD%
where python >nul 2>nul || (echo [ERROR] Python not found in PATH. Install Python 3.10+ first. & pause & exit /b 1)
python "tools\scripts\portable_paths.py" --apply
echo.
pause