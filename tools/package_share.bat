@echo off
chcp 65001 >nul
title SpeakTosi 分享包打包器
cd /d "%~dp0.."

set PY=runtime\py312\python.exe
if not exist "%PY%" set PY=python

echo ==========================================================
echo    SpeakTosi 分享包打包器
echo ==========================================================
echo.
echo    [1] share  推荐 - 约 3G   代码+素材+13位老师+Electron依赖(node_modules), 模型另下
echo    [2] full   完整包 - 约 38G  开机即用: 全部模型+3个venv+克隆语音运行环境, 解压双击即跑
echo    [3] lite   精简 - 约 50-100M  纯代码, 模型和环境全另装
echo.
set MODE=
set /p CH=请输入 1/2/3 后回车 (直接回车 = share): 
if "%CH%"=="2" set MODE=--full
if "%CH%"=="3" set MODE=--lite

echo.
"%PY%" tools\package_share.py %MODE%
echo.
echo 产出位于 dist\ 目录。可直接把这个 zip 发给同事。
pause