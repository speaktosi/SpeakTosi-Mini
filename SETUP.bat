@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
echo ============================================================
echo   SpeakTosi 首次安装 / 搬家自愈 + 自检
echo ============================================================
echo.
if not exist "runtime\py312\python.exe" (
  echo [!] 缺 runtime\py312\python.exe  -- 包不完整, 请重新解压
  pause & exit /b 1
)
runtime\py312\python.exe tools\scripts\first_run_setup.py --check
echo.
echo   现在双击 start_speaktosi.vbs 启动(推荐, 无黑框)。
echo.
pause