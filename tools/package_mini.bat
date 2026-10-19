@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0.."
echo ==================================================================
echo   SpeakTosi Mini 打包（只装 Coach Arthur + Coach Mia 两位老师）
echo ==================================================================
echo.

where python >nul 2>nul
if errorlevel 1 (
  echo [!] 找不到 python，请先把 Python 加入 PATH（或改本文件里的 python 为完整路径）
  pause & exit /b 1
)

echo [1/3] 体检：只统计体积，不写文件（改过规则后先看这个）
python tools\package_share.py --mini --dry
if errorlevel 1 (
  echo [!] 体检失败，见上面的输出
  pause & exit /b 1
)
echo.

echo [2/3] 打自包含 Mini 版（约 25 GB，视磁盘 10~30 分钟）
python tools\package_share.py --mini
if errorlevel 1 (
  echo [!] 打包失败，见上面的输出
  pause & exit /b 1
)
echo.

echo [3/3] 打 GitHub 仓库档（只有代码 + 文档，秒级完成）
python tools\package_share.py --mini-code --tree dist\SpeakTosi-Mini-2Teachers
if errorlevel 1 (
  echo [!] 仓库档生成失败，见上面的输出
  pause & exit /b 1
)
echo.

echo ==================================================================
echo 完成：
echo   dist\SpeakTosi_Mini_*.zip                       自包含包（发给人用）
echo   dist\SpeakTosi-Mini-2Teachers\                  仓库档（git init 后推 GitHub）
echo   dist\安装说明_mini.txt / 安装说明_mini-code.txt   安装说明
echo 打包器已在最后跑过自检（两位老师/无他师资产/音色两位/关键文件/无密钥）。
echo ==================================================================
pause