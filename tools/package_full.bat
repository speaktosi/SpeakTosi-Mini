@echo off
chcp 65001 >nul
title SpeakTosi 完整包(开机即用)打包器
cd /d "%~dp0.."

set PY=runtime\py312\python.exe
if not exist "%PY%" set PY=python

echo ==========================================================
echo    SpeakTosi 完整包 (full) —— 同事拿到就能跑
echo ----------------------------------------------------------
echo    打入: 全部模型 + engine/venv + app/backend/.venv +
echo          audiocpp(含模型) + runtime + node_modules +
echo          克隆语音运行环境(Voice-Pro env + CosyVoice2-0.5B)
echo    预计产出 35~38G; 需要:
echo      1) 开发机克隆语音环境在位: D:\AI-ModeLs\Voice-Pro
echo         (换机器可用环境变量 SPEAKTOSI_VP 指定其它路径)
echo      2) C 盘剩余 80G 以上
echo ==========================================================
echo.
"%PY%" tools\package_share.py --full
echo.
echo 产出位于 dist\SpeakTosi_full_*.zip  —— 整包发给同事, 解压后双击 SETUP.bat / start_speaktosi.vbs
pause