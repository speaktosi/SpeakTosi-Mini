@echo off
chcp 65001 >nul
REM SpeakTosi - one-click launch (console flashes then closes, no lingering black window)
cd /d "%~dp0app"
start "" node_modules\electron\dist\electron.exe .
exit
