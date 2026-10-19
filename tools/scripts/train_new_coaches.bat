@echo off
chcp 65001 >nul
REM ============================================================
REM  SpeakTosi - Train new coaches (Eleanor / Kate / Kai)
REM  Double-click to run. It will STOP the avatar engine + TTS
REM  for about 15-25 minutes, then bring them back automatically.
REM ============================================================
cd /d "%~dp0"
echo.
echo  === SpeakTosi New Coach Training ===
echo  This will STOP the avatar engine + TTS for ~15-25 min.
echo  Do NOT run while someone is in a live session.
echo.
set /p ok="Continue? (y/N) "
if /i not "%ok%"=="y" goto end
"%CD%\..\..\engine\venv\Scripts\python.exe" "%CD%\train_new_coaches.py" --go
echo.
echo  === DONE (see report in coach_runs\new3_train_report.json) ===
pause
exit /b
:end
echo Cancelled.
pause