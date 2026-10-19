#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SpeakTosi 首次运行 / 搬家自愈 (1019)

目的: 整包可以解压到【任意路径】直接跑, 不用手工改配置。
(venv 里写的是绝对路径 —— 换目录就会失效, 所以第一次启动前必须自愈一次。)

做三件事, 全部幂等(路径已正确时只读不写, 约 1 秒):
  1) 修正各 venv 的 pyvenv.cfg: home 指向包内 runtime\\... , 并清掉指向开发机的历史行
  2) 调 tools/scripts/portable_paths.py 把配置/脚本里的旧绝对路径改成当前实际路径
  3) 自检(仅首次或换目录时): 关键解释器 / 模型 / 素材是否就位, 跑一次 import 冒烟

用法:
  runtime\\py312\\python.exe tools\\scripts\\first_run_setup.py            # 可视化
  runtime\\py312\\python.exe tools\\scripts\\first_run_setup.py --quiet    # 静默(启动器用)
  ... --check          # 强制重跑冒烟检查
"""
from __future__ import annotations

import datetime
import json
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))            # <pkg>/tools/scripts
ROOT = os.path.dirname(os.path.dirname(HERE))                # <pkg>
QUIET = "--quiet" in sys.argv
FORCE_CHECK = "--check" in sys.argv
MARKER = os.path.join(ROOT, "tools", "_portable_root.json")

# (venv 相对路径, home 应指向的包内相对路径)
VENV_HOMES = [
    ("engine/venv", "runtime/py312"),
    ("app/backend/.venv", "runtime/py311"),
    ("runtime/voice-pro/installer_files/env",
     "runtime/voice-pro/installer_files/python/cpython-3.12-windows-x86_64-none"),
]
DROP_IF_OUTSIDE = ("executable", "command", "base-executable", "base-exec-prefix", "base-prefix")

# 关键文件(相对包根); 第二列 False = 可选(share 档没有)
KEY_PATHS = [
    ("start_speaktosi.vbs", False),
    ("app/node_modules/electron/dist/electron.exe", True),
    ("app/backend/server.py", False),
    ("runtime/py312/python.exe", True),
    ("engine/aiteacher_watchdog.py", False),
    ("engine/models", True),
    ("engine/data/avatars", True),
    ("audiocpp", True),
    ("engine/tools/cosyvoice_lx_server_v3.py", False),
    ("runtime/voice-pro/installer_files/env/Scripts/python.exe", True),
]


def say(msg: str) -> None:
    if not QUIET:
        print(msg, flush=True)


def clean_env() -> dict:
    e = os.environ.copy()
    for k in ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP"):
        e.pop(k, None)
    return e


def fix_venv(rel_venv: str, rel_home: str) -> str:
    cfg = os.path.join(ROOT, *rel_venv.split("/"), "pyvenv.cfg")
    if not os.path.isfile(cfg):
        return "无配置(跳过)"
    home = os.path.join(ROOT, *rel_home.split("/"))
    if not os.path.isdir(home):
        return f"包内缺 {rel_home}(跳过)"
    try:
        with open(cfg, "r", encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except OSError as e:
        return f"读取失败({e})"
    out, changed, had_home = [], False, False
    for ln in lines:
        if "=" not in ln:
            out.append(ln)
            continue
        key, _, val = ln.partition("=")
        key, val = key.strip(), val.strip()
        if key == "home":
            had_home = True
            if os.path.normcase(val) != os.path.normcase(home):
                out.append("home = " + home)
                changed = True
            else:
                out.append(ln)
        elif key in DROP_IF_OUTSIDE and val and not val.lower().startswith(os.path.normcase(ROOT).lower()):
            changed = True          # 丢掉指向开发机的历史行
        else:
            out.append(ln)
    if not had_home:
        out.insert(0, "home = " + home)
        changed = True
    if changed:
        try:
            with open(cfg, "w", encoding="utf-8") as f:
                f.write("\n".join(out) + "\n")
        except OSError as e:
            return f"写入失败({e})"
        return "已修正 -> " + home
    return "已正确"


def _md5(p: str) -> str:
    import hashlib
    h = hashlib.md5()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def fix_venv_launcher(rel_venv: str, rel_home: str) -> str:
    """1019fix（冷启实测抓到的致命可移植性 bug）:
    uv 造的 venv，Scripts\\python.exe 是 uv 自家 launcher —— 它【无视 pyvenv.cfg 的 home】，
    跑起来 sys.path/sys.base_prefix 仍指向开发机（C:\\Users\\...\\uv\\python\\cpython-3.12... 或
    D:\\AI-ModeLs\\Voice-Pro\\...）。本机有这些目录所以一切正常，同事机没有 → venv 起不来。
    铁证: 改 home 后 base_prefix 不变; sys.path 里明晃晃写着开发机路径。
    修法: 换成 CPython 官方 venvlauncher（Lib\\venv\\scripts\\nt\\python.exe，读 home 找基座）→
          base 落到包内 runtime/py3xx，整包可搬到任意路径（已实测）。
    幂等: 与基座里的官方 launcher 同 md5 即跳过; 覆盖前备份为 *.uvlauncher.bak。"""
    venv = os.path.join(ROOT, *rel_venv.split("/"))
    base = os.path.join(ROOT, *rel_home.split("/"))
    src_dir = os.path.join(base, "Lib", "venv", "scripts", "nt")
    if not os.path.isfile(os.path.join(venv, "Scripts", "python.exe")):
        return "无 venv(跳过)"
    if not os.path.isdir(src_dir):
        return f"包内基座无官方 launcher({rel_home}/Lib/venv/scripts/nt) 跳过"
    done, same = [], 0
    for name in ("python.exe", "pythonw.exe"):
        src, dst = os.path.join(src_dir, name), os.path.join(venv, "Scripts", name)
        if not (os.path.isfile(src) and os.path.isfile(dst)):
            continue
        try:
            if os.path.getsize(src) == os.path.getsize(dst) and _md5(src) == _md5(dst):
                same += 1
                continue
            bak = dst + ".uvlauncher.bak"
            if not os.path.isfile(bak):
                shutil.copy2(dst, bak)
            shutil.copy2(src, dst)
            done.append(name)
        except OSError as e:
            return f"写入 {name} 失败({e})"
    if done:
        return "已换官方 launcher: " + ", ".join(done)
    return "已是官方 launcher(跳过)" if same else "无 python.exe(跳过)"


def venv_base_check(rel_venv: str, rel_home: str) -> str:
    """1019fix 的自检: 让 venv 里的解释器自己报 sys.base_prefix, 必须在包内。
    指向包外(开发机)即该 venv 换台机器必崩 —— 这是本次冷启实测的核心判据。"""
    py = os.path.join(ROOT, *rel_venv.split("/"), "Scripts", "python.exe")
    if not os.path.isfile(py):
        return "无解释器(跳过)"
    try:
        r = subprocess.run([py, "-c", "import sys;print(sys.base_prefix)"], capture_output=True, text=True,
                           timeout=120, env=clean_env(), cwd=ROOT)
        base = (r.stdout or "").strip().splitlines()[-1] if (r.stdout or "").strip() else ""
        if not base:
            return "跑不起来: " + ((r.stderr or "").strip()[-90:])
        ok = os.path.normcase(base).startswith(os.path.normcase(ROOT))
        return ("base 在包内 ✔ " if ok else "!! base 在包外(换机必崩): ") + base
    except Exception as e:
        return f"{type(e).__name__}: {e}"


def run_portable_paths() -> str:
    pp = os.path.join(HERE, "portable_paths.py")
    if not os.path.isfile(pp):
        return "无 portable_paths.py(跳过)"
    flag = []
    try:
        with open(pp, "r", encoding="utf-8", errors="ignore") as f:
            txt = f.read()
        if "--apply" in txt:
            flag = ["--apply"]
        elif "--write" in txt:
            flag = ["--write"]
    except OSError:
        pass
    try:
        r = subprocess.run([sys.executable, pp] + flag, cwd=ROOT, capture_output=True,
                           text=True, encoding="utf-8", errors="replace",
                           env=clean_env(), timeout=600)
    except Exception as e:
        return f"调用失败({e})"
    tail = [x for x in (r.stdout or "").strip().splitlines() if x.strip()][-2:]
    if r.returncode != 0:
        return "失败 rc=%d %s" % (r.returncode, (r.stderr or "")[-160:])
    return "ok" + (" | " + " | ".join(tail) if tail else "")


def smoke_import(rel_py: str, mod: str, timeout: int = 120) -> str:
    py = os.path.join(ROOT, *rel_py.split("/"))
    if not os.path.isfile(py):
        return "无解释器(跳过)"
    try:
        r = subprocess.run([py, "-c", "import %s; print('ok')" % mod], cwd=ROOT,
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", env=clean_env(), timeout=timeout)
    except Exception as e:
        return f"超时/异常({e})"
    return "ok" if r.returncode == 0 and "ok" in (r.stdout or "") else \
           "失败: " + ((r.stderr or r.stdout or "")[-160:]).replace("\n", " ")


RUNTIME_MODE_JSON = '{\n  "mode": "auto",\n  "note": "安装时默认自动: 按内存/显存落 full/lite/ultra。设置页可改。"\n}\n'


def _ram_vram_gb():
    """物理内存/显存总量(GB) —— ctypes + nvidia-smi, 零依赖。探测不到给 0。"""
    ram = vram = 0.0
    try:
        import ctypes

        class _MS(ctypes.Structure):
            _fields_ = [('dwLength', ctypes.c_ulong), ('dwMemoryLoad', ctypes.c_ulong),
                        ('ullTotalPhys', ctypes.c_ulonglong), ('ullAvailPhys', ctypes.c_ulonglong),
                        ('ullTotalPageFile', ctypes.c_ulonglong), ('ullAvailPageFile', ctypes.c_ulonglong),
                        ('ullTotalVirtual', ctypes.c_ulonglong), ('ullAvailVirtual', ctypes.c_ulonglong),
                        ('ullAvailExtendedVirtual', ctypes.c_ulonglong)]

        st = _MS()
        st.dwLength = ctypes.sizeof(_MS)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
            ram = st.ullTotalPhys / (1024.0 ** 3)
    except Exception:
        pass
    try:
        import subprocess
        o = subprocess.run(['nvidia-smi', '--query-gpu=memory.total', '--format=csv,noheader,nounits'],
                           capture_output=True, text=True, timeout=10).stdout
        nums = [int(x) for x in o.split() if x.strip().isdigit()]
        if nums:
            vram = nums[0] / 1024.0
    except Exception:
        pass
    return ram, vram


def _expect_mode(ram, vram):
    """与 engine/aiteacher_watchdog.py::_auto_mode 同一张表(改一处必须改两处)。"""
    if (ram and ram < 18) or (vram and vram < 11):
        return 'ultra'
    if (ram and ram < 29) or (vram < 1) or (vram < 16):
        return 'lite'
    return 'full'


def ensure_runtime_mode():
    p = os.path.join(ROOT, "config", "runtime_mode.json")
    if os.path.isfile(p):
        try:
            with open(p, encoding="utf-8") as f:
                m = (json.load(f) or {}).get("mode", "auto")
            return "运行档位文件已存在: mode=%s (设置页/AB 可改, 重启生效)" % m
        except Exception:
            pass
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(RUNTIME_MODE_JSON)
        return "已写入默认运行档位 config/runtime_mode.json = auto"
    except OSError as e:
        return "运行档位文件写入失败: %s" % e


def token_state():
    p = os.path.join(ROOT, "app", "backend", ".env")
    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            txt = f.read()
    except OSError:
        txt = ""
    have = any(k in txt for k in ("DASHSCOPE_API_KEY=", "ALIBABA_CODING_PLAN_API_KEY=", "BAILIAN_API_KEY="))
    return ("云端 Token 已配置(app/backend/.env) —— 直接启动即可"
            if have else
            "云端 Token 未配置 —— 启动后会自动弹出【首次配置】窗口, 粘贴自己的 Key 点『保存并测试』")


def main() -> int:
    say("=" * 60)
    say(" SpeakTosi 路径自愈 / 自检")
    say("=" * 60)
    say(f"包根目录: {ROOT}")

    # ---- 1. venv 路径 + launcher(可移植性) ----
    for rel_venv, rel_home in VENV_HOMES:
        say(f"[venv] {rel_venv:<38} {fix_venv(rel_venv, rel_home)}")
        say(f"[venv] {'':<38} {fix_venv_launcher(rel_venv, rel_home)}")
        say(f"[venv] {'':<38} {venv_base_check(rel_venv, rel_home)}")

    # ---- 2. 配置里的绝对路径 ----
    say(f"[path] portable_paths.py                    {run_portable_paths()}")

    # ---- 3. 自检 ----
    marker = {}
    try:
        if os.path.isfile(MARKER):
            with open(MARKER, "r", encoding="utf-8") as f:
                marker = json.load(f) or {}
    except Exception:
        marker = {}
    need_check = FORCE_CHECK or os.path.normcase(str(marker.get("root", ""))) != os.path.normcase(ROOT)
    if need_check:
        miss, opt_miss = [], []
        for rel, optional in KEY_PATHS:
            if not os.path.exists(os.path.join(ROOT, *rel.split("/"))):
                (opt_miss if optional else miss).append(rel)
        say(f"[self] 关键文件缺失: {miss if miss else '无'}")
        if opt_miss:
            say(f"[self] 可选缺失(share 档正常): {opt_miss}")
        for rel_py, mod in (("engine/venv/Scripts/python.exe", "numpy"),
                            ("app/backend/.venv/Scripts/python.exe", "numpy"),
                            ("runtime/voice-pro/installer_files/env/Scripts/python.exe", "torch")):
            say(f"[self] {mod:<6} @ {rel_py:<58} {smoke_import(rel_py, mod)}")
        try:
            with open(MARKER, "w", encoding="utf-8") as f:
                json.dump({"root": ROOT, "ts": datetime.datetime.now().isoformat(timespec="seconds"),
                           "ver": "1019r2", "missing": miss}, f, ensure_ascii=False, indent=2)
        except OSError:
            pass
    else:
        say("[self] 本路径此前已自检过, 跳过冒烟(要重跑: --check)")

    # ---- 4. 1019setup: 运行档位 + 云端 Token 引导(新机装上就能自愈出可用状态) ----
    ram, vram = _ram_vram_gb()
    exp = _expect_mode(ram, vram)
    say(f"[mode] 机器: 内存 {ram:.1f}G / 显存 {vram:.1f}G -> 预计档位 {exp}"
        + ("  [!] 极简档: 真人系老师不可用, 请在设置里选卡通系老师" if exp == "ultra" else ""))
    say(f"[mode] {ensure_runtime_mode()}")
    say(f"[cred] {token_state()}")

    say("完成。双击 start_speaktosi.vbs 启动(首次会弹【云端 Token 配置】窗口)。")
    return 0


if __name__ == "__main__":
    sys.exit(main())