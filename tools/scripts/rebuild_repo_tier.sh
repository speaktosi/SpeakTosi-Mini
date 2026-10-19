#!/usr/bin/env bash
# 1020 一键重建 GitHub 仓库档(代码档)并提交。
# 用法: tools/scripts/rebuild_repo_tier.sh ["提交信息"]
# 说明: identity/消息固定, 避免每次手打不一致; 只发代码+文档, 不含模型/运行时/形象包。
set -euo pipefail
cd "$(dirname "$0")/../.."                       # -> 包根
TREE="dist/SpeakTosi-Mini-2Teachers"
MSG="${1:-SpeakTosi Mini: rebuild}"

env -u PYTHONPATH -u PYTHONHOME python tools/package_share.py --mini-code --tree "$TREE"

cd "$TREE"
git init -q 2>/dev/null || true
git config user.name  "SpeakTosi contributors"
git config user.email "maintainers@example.com"
git add -A
git -c core.autocrlf=false commit -q -m "$MSG"
echo "[repo-tier] HEAD=$(git rev-parse --short HEAD)  文件数(不含.git)=$(find . -path ./.git -prune -o -type f -print | wc -l)"