@echo off
REM 启动 云来AI工作台 / QuickStudio(MiniMax H3 视频后端 17860) —— 供 idle 出片使用
cd /d D:\AI-ModeLs\Minimax.workbench-V260906
set PYTHONPATH=
set PYTHONHOME=
set PYTHONNOUSERSITE=1
set HF_HOME=%CD%\models
set TORCH_HOME=%CD%\models
set PYANNOTE_CACHE=%CD%\models\pyannote
set KERAS_BACKEND=torch
set GIT_PATH=%CD%\runtime\yunlaiai\Git\bin
set FFMPEG_PATH=%CD%\runtime\yunlaiai\ffmpeg\bin
set PATH=%GIT_PATH%;%FFMPEG_PATH%;%PATH%
.\runtime\yunlaiai\python.exe -B -s start.py --quickstudio-port 17860 --comfy-port 8188 > "%~dp0qs_start.log" 2>&1
