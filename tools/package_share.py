#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SpeakTosi 打包器 (1019r2)
================================
把【主程序】打成可分享给同事的 zip。主程序自足 —— 不依赖任何外部目录
(例如桌面备份 91Talk-AI、D:\\Software 开发机路径)。

四档:
  share (默认)  源码 + 必要素材 + 13 位老师 + Electron 前端依赖(node_modules)
                => 同事解压即能开界面; 模型/环境另装(见 docs/assets-download.md)
  full          完整包 = share + 全部模型 + 3 个 venv + runtime + 克隆语音运行环境
                (开发机 D:\\AI-ModeLs\\Voice-Pro 的 env/代码/CosyVoice2-0.5B 模型)
                => 开机即用: 不下载模型、不装环境, 解压→双击即跑
  lowmem        【低配版】= full 减去"运行时根本不加载"的死重(audiocpp 的 models/gpu/venv/cpu,
                约 7.7G: 那是 1015 已退役的本地 qwen3-TTS 服务), 其余一模一样,
                运行档位默认 auto(16~24G 内存机器自动落到 lite 单 TTS)。
                => 给 16~24G 内存的同事机; 功能与 full 无差别(引擎仍在, 只是不带死重)
  lite          纯代码(最小, 用于代码审查/对比)

纯净/脱敏(所有档强制, 见 SANITIZE_*):
  - 会话/记忆: history.json/sessions.json 清空, persona.json 复位为默认老师 soul
  - 个人标定: landmarks.json / .calib / app/config/electron.json(窗口位置+挂眼开关) 一律不带
  - 运行时状态: tts_endpoints.json / runtime_mode_active.json 不带(启动时重建)
  - 密钥: *.env 全排除 + 打包前 key 嗅探(命中即拒); 新机由首次配置向导填自己的 token

用法:
  双击  tools\\package_share.bat              -> share 档
  双击  tools\\package_full.bat               -> full 档(完整包, 约 38G, 局域/网盘拷贝)
  python tools/package_share.py --full        -> 同上(命令行)
  python tools/package_share.py --lite        -> 纯代码

产出(每次写 <name> 与 latest 两个副本):
  dist/SpeakTosi_share_<日期>.zip   + dist/安装说明_share.txt
  dist/SpeakTosi_full_<日期>.zip    + dist/安装说明_full.txt

安全: 打包前扫描密钥模式, 命中即拒绝入包; *.env 一律排除(仅保留 example 模板)。
      第三方运行环境(voice-pro env) 不做逐文件扫描(无本项目密钥, 且文件数 20 万+)。
"""
from __future__ import annotations
import argparse
import datetime
import fnmatch
import os
import shutil
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import mini_edition as mini          # 1020mini: 2 师简化版档规则(见该文件顶部说明)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIST = os.path.join(ROOT, "dist")
STAMP = datetime.datetime.now().strftime("%Y%m%d_%H%M")

# ---------- 顶层白名单: 只打这些(其余一律不碰) ----------
TOP_ENTRIES = [
    "app", "engine", "audiocpp", "runtime", "avatar", "config", "docs", "tools",
    "start_speaktosi.vbs", "SETUP.bat", "README.md", "LICENSE", ".gitignore",
]
# 1020mini-code(GitHub 仓库档)额外顶层项: 开源仓库的元数据/文档(默认白名单里没有 .github)
TOP_ENTRIES_EXTRA = {
    "mini-code": [".github", ".gitattributes", "CHANGELOG.md", "CONTRIBUTING.md",
                  "SECURITY.md", "CODE_OF_CONDUCT.md", "README.en.md"],
}

# ---------- 目录名排除(任意层级命中即跳过整棵子树) ----------
EXCL_DIRS_BASE = {
    "__pycache__", ".git", ".github", ".idea", ".vscode",
    "_archive_1019", "_archive_pre1018", "backup_1018_mt",
    "coach_runs", "probe_1017",            # 训练产物/探针产物
    "eval_cache", "audio_cache",            # 运行缓存
    # 1019fix(冷启实测抓到的真 bug): customvideo 曾被当"训练素材"整体排除 —— 大错!
    #   engine/app.py:167-175 viseme 引擎启动就要读 engine/data/customvideo/<avatar>_idle/*.png,
    #   缺了直接 RuntimeError → 8010 永远起不来(卡通系老师全废, 老 full/share 包同病)。
    #   现在保留 customvideo(约 800M: 13 个 *_idle 序列 + image/), 只排冗余备份 *_bak_*(见 walker)。
    "userdata",                             # app/config/userdata 会话数据(含 lockfile)
    "logs",
}
# 1019r2 坑: 原来把 "dist"/"build" 当通用排除 → 把 app/node_modules/electron/dist 也杀了,
#   包内没 electron.exe, 同事解压后根本开不了界面。包根 dist/ 已由顶层白名单挡住, 无需再排。
# 1019 改动: share 档【要带 node_modules】—— start_speaktosi.vbs 就是拉
#   app\node_modules\electron\dist\electron.exe, 不带的话同事解压后开不了界面。
EXCL_DIRS_SHARE = {"venv", ".venv", "models"}
EXCL_DIRS_LITE = {"venv", ".venv", "models", "node_modules",
                  "runtime", "gpu", "cpu", "assets", "web", "webui", "avatars", "data"}

# ---------- 1019lowmem【低配版】: full 减去运行时根本不加载的死重 ----------
# 实测依据(2026-10-19 逐进程取证): 活着的 4 个服务只用 engine/venv + app/backend/.venv +
# runtime/py311 + runtime/voice-pro; audiocpp\{models,gpu,venv,cpu} 是 1015 已退役的
# 本地 qwen3-TTS 服务残骸(audiocpp_server.exe 不再被 SERVICES 引用), 但占 7.7G。
# 只保留 audiocpp\webui(ffmpeg.exe + voice/*.wav 音色参考音频 —— TTS 真需要)。
# 坑(1019 实测踩到): 这里只能用【全局通名之外的】规则 —— 早先误把 "venv"/"models"/"gpu"/"cpu"
# 放进本集合, 它们是任意层级的通名, 会连 engine/venv、engine/models、runtime/voice-pro/... 一起砍掉
# (dry-run 立刻现形: 33.4G -> 16.0G, 包直接废)。所以低配档只做"具体子树"排除。
EXCL_DIRS_LOWMEM = {"logs"}
# 低配版额外排除的具体子树(相对包根, 正斜杠)
EXCL_SUBPATHS_LOWMEM = {
    "audiocpp/venv", "audiocpp/models", "audiocpp/gpu", "audiocpp/cpu",
    "app/backend/eval_cache",
}
# ---------- 1020mini-code【GitHub 仓库档】: 只发代码+文档, 不带模型/运行时/形象包/待机帧 ----------
# 依据: 开源仓库不该塞几百 M 二进制; 缺的部分由 docs/ASSETS.md + Release 说明补齐。
EXCL_DIRS_MINICODE = {"venv", ".venv", "models", "node_modules", "runtime",
                      "gpu", "cpu", "userdata", "logs"}
EXCL_SUBPATHS_MINICODE = {
    "engine/data/avatars",        # 两位老师的 musetalk 形象包(约 200M)
    "engine/data/customvideo",    # 待机帧序列(约 85M)
    "audiocpp/models", "audiocpp/venv", "audiocpp/gpu", "audiocpp/cpu",
    "app/backend/eval_cache",
}

# ---------- 1019 纯净/脱敏: 入包前重写或剔除的个人数据 ----------
SANITIZE_DROP = {
    "app/backend/config/landmarks.json",        # 个人五官标定(每人脸不同)
    "app/backend/config/.calib",
    "app/config/electron.json",                 # 窗口位置/挂眼开关(开发机状态)
    "engine/data/tts_endpoints.json",           # 双 worker 端点登记(启动时按档位重建)
    "engine/data/runtime_mode_active.json",     # 上次生效档位(启动时重建)
    "engine/data/tts_runtime.json",             # 1019fix: 上一版写错成 app/backend/config/, 没剔到真的那份
    "engine/data/customvideo/eyegazecheck_report.json",   # 开发机挂眼探针报告
}
SANITIZE_CFG_FORCE = {                          # app.json: 复位到"出厂默认老师"
    "llm_api_key": "",
    "avatar_id": "aiteacher_demo",
    "teacher_id": "aiteacher_demo",
    "avatar_voice": "default",
    "audiocpp_tts_voice": "default",
}
DEFAULT_PERSONA_SRC = "app/backend/config/personas/brenda.json"   # 默认老师(aiteacher_demo=Brenda)的 soul
RUNTIME_MODE_DEFAULT = '{\n  "mode": "auto",\n  "note": "安装时默认自动: 按内存/显存落 full/lite/ultra。设置页可改。"\n}\n'

# ---------- 文件排除 glob ----------
EXCL_FILE_GLOBS = [
    "*.bak*", "*.good_*", "*.pre_*", "*.old", "*.old_*",
    "*.pathbak", "*.log", "*.tmp", "*.pyc", "*.pyo", ".ui_alive_*",
    "desktop.ini", "Thumbs.db", ".DS_Store", "main-log.txt",
    "hermes_tmp_lx", "C:Users*", "*.zip", "*.sys",
    "*.env", ".env", "*.env.bak",
    "_portable_root.json", ".portable_root.json",
]
# 例外: 这些即使匹配上面也要保留
KEEP_ALWAYS = {"student_keys.example.env", "requirements.txt", "package.json",
               "package-lock.json", ".gitignore", "LICENSE", "README.md"}

# ---------- 大二进制: 直接存储(压缩不了, 省 70% 打包时间) ----------
NOCOMP_EXT = {".safetensors", ".pt", ".pth", ".bin", ".onnx", ".pyd", ".dll",
              ".exe", ".lib", ".zip", ".npz", ".npy", ".mp4", ".wav", ".mp3",
              ".jpg", ".jpeg", ".png", ".webp", ".whl", ".dat", ".ckpt", ".pkg"}

# ---------- 1019: full 档的外部资源(克隆语音运行环境在开发机 D:) ----------
# 结构必须镜像 Voice-Pro 原样: 看门狗取 <VP>\installer_files\env\Scripts\python.exe,
# 语音服务取 <VP>\cosyvoice 与 <VP>\model\cosyvoice\CosyVoice2-0.5B
_excl = os.environ.get("SPEAKTOSI_VP")
VP_SRC = _excl if (_excl and os.path.isdir(_excl)) else r"D:\AI-ModeLs\Voice-Pro"
EXTERNAL_SETS = {
    "full": [
        (os.path.join(VP_SRC, "installer_files", "env"), "runtime/voice-pro/installer_files/env"),
        (os.path.join(VP_SRC, "installer_files", "python"), "runtime/voice-pro/installer_files/python"),
        (os.path.join(VP_SRC, "cosyvoice"), "runtime/voice-pro/cosyvoice"),
        (os.path.join(VP_SRC, "third_party"), "runtime/voice-pro/third_party"),
        (os.path.join(VP_SRC, "model", "cosyvoice", "CosyVoice2-0.5B"),
         "runtime/voice-pro/model/cosyvoice/CosyVoice2-0.5B"),
    ],
}
EXTERNAL_SETS["lowmem"] = EXTERNAL_SETS["full"]   # 1019lowmem: 克隆语音运行环境一模一样(不能省)
EXTERNAL_SETS["mini"] = EXTERNAL_SETS["full"]     # 1020mini: 自包含档同理(不带就开不了口)
EXTERNAL_SETS["mini-code"] = []                   # 1020mini-code: 仓库档不带大件(模型按 docs/ASSETS.md 取)

# ---------- 密钥嗅探 ----------
SECRET_PAT = __import__("re").compile(
    r"((?<![A-Za-z0-9])sk-[A-Za-z0-9\-_]{16,}"          # 1019r2: 加左边界, 免得 prose 里的 "...risk-priority..." 被当成 key
    r"|(?:API_KEY|APIKEY|SECRET|ACCESS_KEY|AUTH_TOKEN)\s*[=:]\s*[\"'][A-Za-z0-9\-_]{20,}[\"']"
    r")",
    __import__("re").IGNORECASE,
)
TEXT_EXT = {".py", ".js", ".ts", ".json", ".txt", ".md", ".yaml", ".yml",
            ".env", ".bat", ".vbs", ".sh", ".cfg", ".ini", ".html", ".css"}


def excluded_dir(name: str, excl_dirs: set) -> bool:
    return name in excl_dirs


def excluded_file(name: str) -> bool:
    if name in KEEP_ALWAYS:
        return False
    for g in EXCL_FILE_GLOBS:
        if fnmatch.fnmatch(name.lower(), g.lower()):
            return True
    return False


def sniff_secret(path: str) -> str | None:
    ext = os.path.splitext(path)[1].lower()
    if ext not in TEXT_EXT:
        return None
    try:
        if os.path.getsize(path) > 2_000_000:
            return None
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            txt = f.read()
    except OSError:
        return None
    m = SECRET_PAT.search(txt)
    return m.group(0)[:40] if m else None


def collect(mode: str):
    excl_dirs = set(EXCL_DIRS_BASE)
    subpath_excl = set()
    if mode == "share":
        excl_dirs |= EXCL_DIRS_SHARE
    elif mode == "lowmem":
        excl_dirs |= EXCL_DIRS_LOWMEM
        subpath_excl |= EXCL_SUBPATHS_LOWMEM
    elif mode == "lite":
        excl_dirs |= EXCL_DIRS_LITE
    elif mode == "mini":                  # 1020mini: 低配版为底 + 只装两位老师
        excl_dirs |= EXCL_DIRS_LOWMEM
        subpath_excl |= EXCL_SUBPATHS_LOWMEM
    elif mode == "mini-code":             # 1020mini-code: 代码档为底 + 只装两位老师
        excl_dirs |= EXCL_DIRS_MINICODE
        subpath_excl |= EXCL_SUBPATHS_MINICODE
        excl_dirs.discard(".github")      # 仓库档要带 issue/PR 模板 + CI
    # full: 仅基础排除

    picked, skipped, secrets = [], [], []
    for top in list(TOP_ENTRIES) + TOP_ENTRIES_EXTRA.get(mode, []):
        src = os.path.join(ROOT, top)
        if not os.path.exists(src):
            continue
        if os.path.isfile(src):
            if not excluded_file(os.path.basename(src)):
                picked.append((src, top))
            continue
        if excluded_dir(os.path.basename(src), excl_dirs):
            skipped.append(top + os.sep)
            continue
        for dirpath, dirnames, filenames in os.walk(src):
            rel = os.path.relpath(dirpath, ROOT).replace(os.sep, "/")
            keep = []
            for d in dirnames:
                if excluded_dir(d, excl_dirs) or d.startswith("_archive"):
                    skipped.append((rel + "/" + d).lstrip("./") + "/")
                    continue
                if "_bak_" in d or d.endswith(("_bak", ".bak")):   # 1019fix: 冗余备份(如 coach_kai_idle_bak_1791632188)
                    skipped.append((rel + "/" + d).lstrip("./") + "/ (冗余备份)")
                    continue
                sub = (rel + "/" + d) if rel != "." else d
                if sub in subpath_excl:                      # 1019lowmem: 整棵子树按相对路径排除
                    skipped.append(sub + "/ (lowmem 死重)")
                    continue
                keep.append(d)
            dirnames[:] = keep
            vendor = ("site-packages" in rel.lower() or "node_modules" in rel.lower()
                      or "third_party" in rel.lower())
            for fn in filenames:
                if excluded_file(fn):
                    skipped.append(os.path.join(rel, fn))
                    continue
                rrel = (rel + "/" + fn) if rel != "." else fn
                if rrel in SANITIZE_DROP:                    # 1019 纯净化: 个人标定/运行时状态不带
                    skipped.append(rrel + " (脱敏剔除)")
                    continue
                if mode in ("mini", "mini-code"):            # 1020mini: 只留 Arthur/Mia 的资产
                    why = mini.drop_reason(rrel, mode)
                    if why:
                        skipped.append(rrel + " (mini: " + why + ")")
                        continue
                fp = os.path.join(dirpath, fn)
                # 1019r2: 第三方库不扫密钥 —— pip/setuptools 的 SPDX 许可证表里就有 sk-xxxx
                #   形态的假阳性(实测把 _spdx.py 误判成密钥拒入包), 它们不含本项目密钥。
                hit = None if vendor else sniff_secret(fp)
                if hit:
                    secrets.append((os.path.relpath(fp, ROOT), hit))
                    continue
                picked.append((fp, rrel))

    # 1019: full 档带上外部克隆语音运行环境(不扫密钥, 文件量巨大)
    for src_dir, dst_rel in EXTERNAL_SETS.get(mode, []):
        if not os.path.isdir(src_dir):
            print(f"[!] 外部资源不存在, 已跳过: {src_dir}")
            continue
        n0 = len(picked)
        for dirpath, dirnames, filenames in os.walk(src_dir):
            dirnames[:] = [d for d in dirnames if not excluded_dir(d, excl_dirs)]
            rel = os.path.relpath(dirpath, src_dir)
            for fn in filenames:
                if excluded_file(fn):
                    continue
                fp = os.path.join(dirpath, fn)
                arc = dst_rel if rel == "." else dst_rel + "/" + rel.replace(os.sep, "/")
                picked.append((fp, arc.replace("\\", "/") + "/" + fn))
        print(f"[package_share] 外部资源 {src_dir} -> {dst_rel}: {len(picked) - n0} 个文件")
    return picked, skipped, secrets


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


SETUP_BAT = r"""@echo off
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
runtime\py312\python.exe tools\scripts\first_run_setup.py
echo.
echo   现在双击 start_speaktosi.vbs 启动(推荐, 无黑框)。
echo.
pause
"""


SITE_BLOCK = """
SpeakTosi 官网与定制服务
  https://www.speaktosi.com    (域名与备案办理中, 即将上线)
  需要【专属 AI 老师形象 / 克隆声音 / 课程定制】, 欢迎从官网联系我们。
"""


def install_txt(mode: str) -> str:
    if mode in ("mini", "mini-code"):
        return mini_install_txt(mode)
    head = """SpeakTosi 完整包 · 开机即用
==================================

本包已自带: 全部模型 + 全部 Python 运行环境 + Electron 前端依赖 + 克隆语音运行环境。
同事拿到后【不需要下载模型、不需要装环境】。

一、解压
  解压到任意【英文、无空格】路径(例如 D:\\SpeakTosi)。
  路径随便放都行 —— 首次启动会自动修正包内绝对路径。
  磁盘剩余建议 >= 5GB(会话/缓存会另占)。

二、启动
  A) 双击 start_speaktosi.vbs        (推荐, 无黑框; 首次会静默自愈一次路径)
  B) 排障用: 先双击 SETUP.bat 看自检输出, 再双击 start_speaktosi.vbs

三、填云端 Token(必须, 否则对话/翻译不可用) —— 已做成向导, 不用手改文件
  双击 start_speaktosi.vbs 后会自动弹出【首次配置】窗口:
  选平台 -> 粘贴自己的 Key -> 点「保存并测试」, 显示"云端已连通"即完成。
  (以后再改: 桌面老师 -> 右键托盘 -> 设置 -> 顶部「云端 Token」区块, 同页可换运行档位。)
  手工方式(排障用): 复制 config\\student_keys.example.env -> app\\backend\\.env 再填 Key。

四、首次启动
  冷启动约 1~3 分钟(要加载数字人 + 克隆语音模型), 托盘图标变绿即可开始对话。
  之后热启动约 10 秒。

五、硬件与运行档位(本包默认 auto, 按机器自动挑, 一般不用管)
  Windows 10/11 x64 + NVIDIA 显卡(RTX 50 系最佳), 显存 >= 12GB(推荐 16GB)
  内存 >= 16GB(推荐 32GB), 磁盘 >= 60GB

  auto 判定表(看门狗启动时写入 engine\\data\\runtime_mode_active.json, 设置页可看可改):
    内存>=29G 且 显存>=16G  -> full  双 TTS(开口最快) + 双引擎
    内存16~28G 或 显存11~15G -> lite  单 TTS(其余全保留, 开口略慢) <- 16~24G 机器
    内存<18G 或 显存<11G     -> ultra 单 TTS + 只跑卡通系引擎(真人系老师不可用)
  硬要手选: 设置页「机器运行档位」下拉, 或改 config\\runtime_mode.json 后重启。

六、端口(被占会启动失败)
  8010 / 8011 数字人引擎 · 18091 / 18092 克隆语音 · 18999 看门狗单实例锁 · 8765 后端

七、出问题看哪
  app\\backend\\logs 与 日志/ 目录; 托盘右键 -> 启动/修复。
"""
    if mode == "lowmem":
        head += """
八、本包是【低配版】(给 16~24G 内存的机器)
  与完整包功能一致, 只是不带 7.7G 运行时根本不加载的旧引擎残留
  (audiocpp\\models | gpu | venv | cpu —— 1015 已退役的本地 qwen3-TTS)。
  运行档位默认 auto: 内存 16~28G 会自动落 lite(单 TTS), <18G 落 ultra(只有卡通系老师)。
  想恢复被裁掉的那部分(一般不需要): 见 docs\\assets-download.md 的 audiocpp 段。
"""
        return head + SITE_BLOCK
    if mode == "full":
        return head + SITE_BLOCK
    return """SpeakTosi 分享包 · 安装说明
==================================

一、快速开始
  1) 解压到任意【英文、无空格】路径, 例如 C:\\Speaktosi
  2) 双击 start_speaktosi.vbs  (推荐, 无黑框; 首次自动修正路径)
     排障可先双击 SETUP.bat 看自检
  3) 首次冷启动 1~3 分钟, 托盘图标变绿即可对话
  (本包已带 Electron 前端依赖 node_modules, 解压即可开界面)

二、缺模型时(share / lite 档)
  见 docs\\assets-download.md, 按表下载后放到指定目录:
    engine\\models\\...     MuseTalk V15 / whisper / sd-vae / rembg
    audiocpp\\models\\...   Qwen3-TTS / Qwen3-ASR
  想省事就要【完整包 full 档】(已含全部模型与运行环境)。

三、缺运行环境时(share / lite 档)
  runtime\\py312\\python.exe -m venv engine\\venv
  engine\\venv\\Scripts\\pip.exe install -r engine\\requirements.txt
  runtime\\py311\\python.exe -m venv app\\backend\\.venv
  app\\backend\\.venv\\Scripts\\pip.exe install -r app\\backend\\requirements-backend.txt
  克隆语音(TTS 18091/18092)还需要 Voice-Pro 运行环境, 见 docs\\assets-download.md。

四、填 API Key
  复制 config\\student_keys.example.env  ->  app\\backend\\.env, 填入自己的 Key

五、硬件
  Windows 10/11 x64 + NVIDIA RTX 50 系, 显存 >=12GB(推荐 16GB), 磁盘 >=25GB

六、端口
  8010 / 8011 数字人引擎, 18091 / 18092 克隆语音, 18999 看门狗单实例锁, 8765 后端
""" + SITE_BLOCK


def add_file(z: zipfile.ZipFile, fp: str, arc: str, big: bool) -> None:
    """大二进制直接存储, 文本才压缩 —— 20G 包能省一大半时间。"""
    try:
        sz = os.path.getsize(fp)
    except OSError:
        sz = 0
    ext = os.path.splitext(fp)[1].lower()
    if ext in NOCOMP_EXT and sz > 1_000_000:
        zi = zipfile.ZipInfo.from_file(fp, arc)
        zi.compress_type = zipfile.ZIP_STORED
        with open(fp, "rb") as fh, z.open(zi, "w") as dst:
            shutil.copyfileobj(fh, dst, 8 * 1024 * 1024)
    else:
        z.write(fp, arc, zipfile.ZIP_DEFLATED, compresslevel=1 if big else 6)


def build_sanitize_overrides(mode: str = "share") -> dict:
    """1019 纯净化: 返回 {包内相对路径(正斜杠): 文本内容} —— 用干净内容覆盖个人数据。
    history/sessions 清空; persona.json 复位成默认老师(Brenda)的 soul;
    app.json 抹掉 key + 复位默认老师; 额外补一份 runtime_mode.json(auto 档)。
    1020mini: 再叠一层 2 师版出厂配置(默认老师=Arthur / 音色=arthur / 只两位老师的人设)。"""
    import json as _json
    ov = {
        "app/backend/config/history.json": "[]\n",
        "app/backend/config/sessions.json": "{}\n",
        "config/runtime_mode.json": RUNTIME_MODE_DEFAULT,
    }
    psrc = os.path.join(ROOT, DEFAULT_PERSONA_SRC)
    if os.path.isfile(psrc):
        with open(psrc, encoding="utf-8") as f:
            ov["app/backend/config/persona.json"] = f.read()
    else:
        print("[!] 默认老师 soul 缺失, 改写出厂 persona: " + DEFAULT_PERSONA_SRC)
    csrc = os.path.join(ROOT, "app", "backend", "config", "app.json")
    try:
        with open(csrc, encoding="utf-8") as f:
            c = _json.load(f)
        c.update(SANITIZE_CFG_FORCE)
        ov["app/backend/config/app.json"] = _json.dumps(c, ensure_ascii=False, indent=2) + "\n"
    except Exception as e:
        print("[!] app.json 清洗失败(%s), 改写出厂默认" % e)
        ov["app/backend/config/app.json"] = _json.dumps(SANITIZE_CFG_FORCE, ensure_ascii=False, indent=2) + "\n"
    if mode in ("mini", "mini-code"):        # 1020mini: 覆盖上一层为 2 师版(必须在最后)
        ov.update(mini.build_overrides())
    return ov


def mini_install_txt(mode: str) -> str:
    """1020mini 安装说明(自包含档 / GitHub 代码档)。"""
    if mode == "mini-code":
        return """SpeakTosi Mini · 两位 AI 老师 · 代码档(GitHub 发布)
=========================================================
本档只含【代码 + 文档】, 不含模型/运行环境/形象素材 —— 面向 GitHub 仓库发布。
可直接跑的自包含包请取 Release 里的 SpeakTosi_Mini_*.zip(自带全部模型与环境)。

一、装依赖
  1) 前端 Electron:  cd app && npm install
  2) 数字人引擎:     python -m venv engine\\venv  (Python 3.10+)
                     engine\\venv\\Scripts\\pip install -r engine\\requirements.txt
  3) 后端:           python -m venv app\\backend\\.venv
                     app\\backend\\.venv\\Scripts\\pip install -r app\\backend\\requirements-backend.txt
  4) 克隆语音(18091/18092): 需要 Voice-Pro 运行环境, 见 docs/ASSETS.md

二、取素材与模型(必须)
  见 docs/ASSETS.md。最少需要:
    engine\\data\\avatars\\coach_arthur\\ 与 coach_mia\\   (MuseTalk 形象包)
    engine\\data\\customvideo\\coach_arthur_idle\\         (待机帧序列)
    engine\\models\\...                                   (MuseTalk V15 / whisper / sd-vae / rembg)
  这些二进制不在仓库里(见 .gitattributes 的 LFS 说明)。

三、启动
  双击 start_speaktosi.vbs(首次自动修正包内绝对路径); 排障先跑 SETUP.bat。
  首启会弹【首次配置】向导: 选平台 → 贴自己的 Key → 保存并测试。

四、端口
  8011 数字人引擎(MuseTalk) · 18091/18092 克隆语音 · 18999 看门狗单实例锁 · 8765 后端
  注: 本档只有 musetalk 一个引擎, 卡通系 8010 已裁。

五、回答长度与官网
  回答长度: 设置页「回答长度」五档(不限 / 长回答200词 / 标准120词默认 / 简答80词 / 极简50词), 30 秒热生效。
  官网与定制服务: https://www.speaktosi.com  (专属 AI 老师形象 / 克隆声音 / 课程定制)
"""
    return """SpeakTosi Mini · 只装两位 AI 老师(Coach Arthur / Coach Mia)
=========================================================
本包是【自包含完整包】: 全部模型 + 全部 Python 运行环境 + Electron 前端依赖 +
克隆语音运行环境都在包内 —— 解压即用, 不用下载模型、不用装环境。

一、解压
  解压到任意【英文、无空格】路径(例 D:\\SpeakTosi)。首次启动会自动修正包内绝对路径。
  磁盘剩余建议 >= 20GB(会话/缓存/日志另占)。

二、启动
  A) 双击 start_speaktosi.vbs     (推荐, 无黑框; 首次静默自愈一次路径)
  B) 排障: 先双击 SETUP.bat 看自检输出, 再双击 start_speaktosi.vbs

三、填云端 Token(必须, 否则对话/翻译不可用)
  首启自动弹【首次配置】向导: 选平台 → 粘贴自己的 Key → 「保存并测试」→ 显示"云端已连通"。
  以后再改: 桌面老师 → 右键托盘 → 设置 → 顶部「云端 Token」。

四、首次启动
  冷启动 1~3 分钟(加载数字人 + 克隆语音模型), 托盘图标变绿即可对话; 之后热启动约 10 秒。

五、硬件
  Windows 10/11 x64 + NVIDIA 显卡, 显存 >= 12GB(推荐 16GB), 内存 >= 16GB(推荐 32GB)
  运行档位默认 auto(看门狗按内存/显存判 full/lite); 本档无卡通引擎, 故最低档 = lite
  (ultra 的定义是"只跑卡通引擎", 在本档不适用 —— 请求 ultra 会自动按 lite 跑)。

六、端口(被占会启动失败)
  8011 数字人引擎(MuseTalk) · 18091 / 18092 克隆语音 · 18999 看门狗单实例锁 · 8765 后端
  注: 卡通系引擎 8010 已裁掉(本档只有 Arthur / Mia 两位真人系老师)。

七、本包简化了什么
  只装 2 位老师: 其余 11 位老师的形象包、待机序列、人设、音色, 以及卡通系素材整族裁掉
  (界面/音色列表里都不会出现)。体积更小、冷启动更快(少一个引擎进程)、物料一致。
  要 13 师完整版或卡通系老师: tools\\package_share.py --lowmem 重新打包。

八、出问题看哪
  app\\backend\\logs 与 日志\\ 目录; 托盘右键 → 启动/修复; docs\\TROUBLESHOOTING.md。

九、回答长度怎么调(老师回复偏短/偏慢都改这里)
  桌面老师 → 右键托盘 → 设置 → 「回答长度」五档:
    不限(最长) / 长回答 约3-6句(200词) / 标准 约3-5句(120词 · 默认) / 简答 约2-4句(80词) / 极简 1-2句(50词 · 最快)
  改完约 30 秒内热生效(文字与语音两条链同时遵守), 不用重启也不用重打包。

十、官网与定制服务
  SpeakTosi 官网: https://www.speaktosi.com  (域名与备案办理中, 即将上线)
  本 Mini 包是开源免费版(仅 Arthur / Mia 两位老师)。想要专属 AI 老师形象、克隆声音、
  贴合行业的课程脚本, 或整机部署/二次开发支持 —— 见官网。
"""


MINI_GITHUB_DIR = os.path.join(ROOT, "tools", "mini_github")


def mini_docs_overlay() -> dict:
    """1020mini: tools/mini_github/** 是"发布件文档层"(README/INSTALL/ARCHITECTURE/.github 模板等),
    直接叠到产物上 —— zip 与目录树因此拿到同一套策展过的文档(含覆盖过期 README)。"""
    ov = {}
    if not os.path.isdir(MINI_GITHUB_DIR):
        print("[!] mini 文档层不存在, 跳过: " + MINI_GITHUB_DIR)
        return ov
    for dirpath, dirnames, filenames in os.walk(MINI_GITHUB_DIR):
        dirnames[:] = [d for d in dirnames if d not in ("__pycache__", ".git")]
        for fn in filenames:
            if fn.endswith((".pyc", ".zip")) or fn.lower() in ("desktop.ini", "thumbs.db"):
                continue
            fp = os.path.join(dirpath, fn)
            rel = os.path.relpath(fp, MINI_GITHUB_DIR).replace(os.sep, "/")
            try:
                with open(fp, "r", encoding="utf-8") as f:
                    ov[rel] = f.read()
            except UnicodeDecodeError:
                with open(fp, "rb") as f:
                    ov[rel] = f.read()
    print("[mini] 文档层覆盖 %d 个文件(来自 tools/mini_github)" % len(ov))
    return ov


def build_transforms(mode: str, picked) -> dict:
    """1020mini: 文本补丁(读原文件 → 内存里改 → 写进产物)。非 mini 档返回空。"""
    if mode not in ("mini", "mini-code"):
        return {}
    out = {}
    for fp, rel in picked:
        if not fp or rel not in mini.RAW_PATCHERS:
            continue
        try:
            with open(fp, encoding="utf-8", errors="ignore") as f:
                txt = f.read()
        except OSError as e:
            print("[!] 待补丁文件读不了: %s (%s)" % (rel, e))
            continue
        new = mini.patch_file(rel, txt)
        if new is not None:
            out[rel] = new
            print("[mini] 文本补丁 %-32s %d -> %d 字节" % (rel, len(txt), len(new)))
    return out


def build_tree(picked, overrides, transforms, dest) -> int:
    """1020mini-code: 直接写目录树(git init 用), 不压 zip。"""
    n = 0
    for fp, rel in picked:
        out = os.path.join(dest, rel.replace("/", os.sep))
        os.makedirs(os.path.dirname(out), exist_ok=True)
        try:
            if rel in transforms or rel in overrides:
                data = transforms.get(rel) or overrides[rel]
                if isinstance(data, bytes):
                    with open(out, "wb") as f:
                        f.write(data)
                else:
                    with open(out, "w", encoding="utf-8", newline="") as f:
                        f.write(data)
            elif fp:
                shutil.copy2(fp, out)
            else:
                continue
        except OSError as e:
            print("[skip] 写不进去, 已跳过: %s (%s)" % (rel, e))
            continue
        n += 1
    if not os.path.isfile(os.path.join(dest, "SETUP.bat")):
        with open(os.path.join(dest, "SETUP.bat"), "w", encoding="utf-8", newline="") as f:
            f.write(SETUP_BAT)
    return n


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--full", action="store_true", help="完整包(含模型/环境/克隆语音, 开机即用)")
    ap.add_argument("--lowmem", action="store_true", help="低配版(给 16~24G 内存机器, =full 减死重)")
    ap.add_argument("--lite", action="store_true", help="纯代码(最小)")
    ap.add_argument("--mini", action="store_true",
                    help="1020mini 简化版: 只装 Arthur+Mia 两位老师(自包含, 解压即用)")
    ap.add_argument("--mini-code", action="store_true",
                    help="1020mini 代码档: 给 GitHub 发布(无模型/运行时/形象包)")
    ap.add_argument("--out", default="", help="输出 zip 路径")
    ap.add_argument("--tree", default="", help="只写目录树不压 zip(配合 --mini-code), 例 dist/SpeakTosi-Mini-2Teachers")
    ap.add_argument("--dry", action="store_true", help="只统计不打包(体检/比对档位体积)")
    args = ap.parse_args()
    mode = ("mini" if args.mini else
            ("mini-code" if args.mini_code else
             ("lowmem" if args.lowmem else
              ("full" if args.full else ("lite" if args.lite else "share")))))

    print(f"[package_share] 模式={mode}  根目录={ROOT}")
    picked, skipped, secrets = collect(mode)
    overrides = build_sanitize_overrides(mode)
    if mode in ("mini", "mini-code"):
        overrides.update(mini_docs_overlay())          # 1020mini: 发布件文档层(README/docs/.github)
    transforms = build_transforms(mode, picked)
    for rel in overrides:                     # 覆盖项也要进包(即使源文件被排除/不存在)
        if not any(r == rel for _, r in picked):
            picked.append((None, rel))
    sanitized = [r for _, r in picked if r in overrides]
    dropped = [s for s in skipped if "脱敏剔除" in s]
    if secrets:
        print("\n[!] 检测到疑似密钥, 已拒绝入包:")
        for rel, hit in secrets:
            print(f"    {rel}  <- 命中 {hit!r}")
    total = 0
    for fp, _ in picked:
        if not fp:
            continue
        try:
            total += os.path.getsize(fp)
        except OSError:
            pass

    if mode == "mini":                          # 1020mini: 产物内写真实构建信息(免文档数字过期)
        rel_bi = "BUILD-INFO.txt"
        if not any(r == rel_bi for _, r in picked):
            picked.append((None, rel_bi))
        overrides[rel_bi] = mini.build_info_text(mode, picked, skipped, total)

    os.makedirs(DIST, exist_ok=True)
    out_name = {"full": "full", "lowmem": "lite", "share": "share", "lite": "codesrc",
                "mini": "Mini", "mini-code": "MiniCode"}[mode]
    out_zip = args.out or os.path.join(DIST, f"SpeakTosi_{out_name}_{STAMP}.zip")
    print(f"[package_share] 入包文件 {len(picked)} 个, 原始体积 {human(total)}")
    print(f"[package_share] 排除条目 {len(skipped)} 个")
    print(f"[package_share] 脱敏覆盖 {len(sanitized)} 个: " + ", ".join(sorted(sanitized)))
    if dropped:
        print(f"[package_share] 脱敏剔除 {len(dropped)} 个: " + ", ".join(sorted(x.split(' ')[0] for x in dropped)))
    if args.tree:                              # 1020mini-code: 直接产出目录树(git init / 人工过目)
        dest = args.tree if os.path.isabs(args.tree) else os.path.join(ROOT, args.tree)
        print(f"[package_share] 目录树模式 -> {dest}")
        n_tree = build_tree(picked, overrides, transforms, dest)
        if mode != "mini-code":        # 仓库档已有 docs/INSTALL.md, 不再塞重复的安装说明
            with open(os.path.join(dest, "安装说明.txt"), "w", encoding="utf-8", newline="") as f:
                f.write(install_txt(mode))
            n_tree += 1
        print(f"[package_share] 写出 {n_tree} 个文件")
        if mode in ("mini", "mini-code"):
            rels = []
            for dirpath, dirnames, filenames in os.walk(dest):
                for fn in filenames:
                    rels.append(os.path.relpath(os.path.join(dirpath, fn), dest).replace(os.sep, "/"))
            tbad = mini.check_names(rels, require_assets=(mode != "mini-code"))
            texts = {}
            for rel in ("app/backend/server.py", "engine/aiteacher_watchdog.py",
                        "tools/scripts/tts_demo_voice.json"):
                p = os.path.join(dest, rel.replace("/", os.sep))
                if os.path.isfile(p):
                    with open(p, encoding="utf-8", errors="ignore") as f:
                        texts[rel] = f.read()
            tbad += mini.audit_teacher_refs(texts)
            print("[ok] mini 目录树自检通过(只有两位老师/无他师资产/音色两位)" if not tbad
                  else f"[!] mini 目录树自检 {len(tbad)} 条问题:")
            for x in tbad[:25]:
                print("    " + x)
        return 0

    if args.dry:
        print("[package_share] --dry: 只统计, 不写 zip")
        return 0

    big = mode in ("full", "lowmem")
    with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True) as z:
        n = 0
        for fp, rel in picked:
            arc = "SpeakTosi/" + rel.replace("\\", "/")
            try:
                if rel in overrides:                       # 1019 纯净化: 干净内容直接写入
                    z.writestr(arc, overrides[rel])
                elif rel in transforms:                    # 1020mini: 文本补丁后的内容
                    z.writestr(arc, transforms[rel])
                elif fp:
                    add_file(z, fp, arc, big)
                else:
                    continue
            except (PermissionError, OSError) as e:
                print(f"[skip] 无法读取, 已跳过: {rel}  ({e})")
                continue
            n += 1
            if big and n % 5000 == 0:
                print(f"    已写入 {n}/{len(picked)} ...", flush=True)
        z.writestr("SpeakTosi/安装说明.txt", install_txt(mode))
        if not any(str(rel).replace("\\", "/") == "SETUP.bat" for _, rel in picked):
            z.writestr("SpeakTosi/SETUP.bat", SETUP_BAT)

    size = os.path.getsize(out_zip)
    print(f"[package_share] 产出 {out_zip}  ({human(size)})")

    bad = []
    with zipfile.ZipFile(out_zip) as z:
        names = z.namelist()
        for nm in names:
            low = os.path.basename(nm).lower()
            if "91talk" in nm.lower():
                bad.append("残留外部备份引用: " + nm)
            if low.endswith((".bak", ".log")) or ".bak_" in low or ".good_" in low or ".pre_" in low:
                bad.append("残留备份/日志: " + nm)
            if low == ".env" or (low.endswith(".env") and "example" not in low):
                bad.append("残留 .env: " + nm)
            if low in ("landmarks.json", "electron.json", ".calib") or nm.endswith("config/electron.json"):
                bad.append("残留个人标定/界面状态: " + nm)
    if mode == "lowmem":
        dead = [nm for nm in names if nm.startswith(("SpeakTosi/audiocpp/models/", "SpeakTosi/audiocpp/gpu/",
                                                    "SpeakTosi/audiocpp/venv/", "SpeakTosi/audiocpp/cpu/"))]
        if dead:
            bad.append("lowmem 档仍含死重 %d 个: %s" % (len(dead), dead[0]))
    print(f"[package_share] zip 内条目 {len(names)} 个")
    if bad:
        print("[!] 自检发现问题:")
        for b in bad[:20]:
            print("    " + b)
    else:
        print("[ok] 自检通过: 无外部备份引用 / 无备份日志 / 无 .env")

    must = ["SpeakTosi/start_speaktosi.vbs", "SpeakTosi/SETUP.bat", "SpeakTosi/README.md",
            "SpeakTosi/app/main.js", "SpeakTosi/engine/aiteacher_watchdog.py",
            "SpeakTosi/tools/scripts/first_run_setup.py", "SpeakTosi/安装说明.txt"]
    if mode in ("share", "full", "lowmem"):
        must.append("SpeakTosi/app/renderer/setup_wizard.html")   # 1019setup 首启 token 向导
        must.append("SpeakTosi/app/backend/config/persona.json")
    if mode == "share":
        must.append("SpeakTosi/app/node_modules/electron/dist/electron.exe")
    if mode in ("full", "lowmem"):
        must += ["SpeakTosi/runtime/py312/python.exe",
                 "SpeakTosi/engine/venv/Scripts/python.exe",
                 "SpeakTosi/runtime/voice-pro/installer_files/env/Scripts/python.exe"]
    if mode == "lowmem":
        must.append("SpeakTosi/audiocpp/webui/ffmpeg.exe")        # 后端切音频/导出要用
        must.append("SpeakTosi/engine/data/voice_registry.json")
    miss = [m for m in must if m not in names]
    print("[ok] 关键文件齐全" if not miss else "[!] 缺关键文件: " + ", ".join(miss))

    if mode in ("mini", "mini-code"):          # 1020mini 档专项自检
        print("[mini] persona 生成器自检: " + mini.verify_persona_generator())
        mbad = mini.check_names(names, require_assets=(mode != "mini-code"))
        texts = {}
        for rel in ("app/backend/server.py", "engine/aiteacher_watchdog.py",
                    "tools/scripts/tts_demo_voice.json"):
            if rel in overrides:
                texts[rel] = overrides[rel]
            elif rel in transforms:
                texts[rel] = transforms[rel]
            else:
                p = os.path.join(ROOT, rel.replace("/", os.sep))
                if os.path.isfile(p):
                    with open(p, encoding="utf-8", errors="ignore") as f:
                        texts[rel] = f.read()
        mbad += mini.audit_teacher_refs(texts)
        print("[ok] mini 自检通过: 只有 Arthur/Mia、无他师资产、音色两位" if not mbad
              else f"[!] mini 自检 {len(mbad)} 条问题:")
        for x in mbad[:25]:
            print("    " + x)

    with open(os.path.join(DIST, f"安装说明_{mode}.txt"), "w", encoding="utf-8") as f:
        f.write(install_txt(mode))
    return 0


if __name__ == "__main__":
    sys.exit(main())