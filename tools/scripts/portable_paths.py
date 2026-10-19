#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""SpeakTosi 路径自愈 —— 换机/换盘解压后一键修正绝对路径。

原理：本文件位于 <根>\tools\scripts\portable_paths.py，根目录 = parents[2]。
把配置/代码里写死的旧根（C:\\Speaktosi、历史中文目录名）改写为当前实际根，
保持原斜杠风格；幂等、可重复执行；--apply 前自动生成 .pathbak 备份。

用法：
    python tools/scripts/portable_paths.py            # 干跑，只报告
    python tools/scripts/portable_paths.py --apply    # 实际改写（自动备份）
"""
import os, re, sys, json, shutil, pathlib, datetime

ROOT = pathlib.Path(__file__).resolve().parents[2]
OLD_ROOTS = [r"C:\Speaktosi", "C:/Speaktosi", r"c:\Speaktosi"]
APPLY = "--apply" in sys.argv
SKIP = ("node_modules", "site-packages", ".venv", os.sep + "venv", os.sep + "models",
        "runtime", "userdata", "__pycache__", ".git", "audiocpp")
EXTS = (".py", ".js", ".json", ".vbs", ".bat", ".txt", ".md", ".yaml", ".yml", ".html")
# 历史中文目录名（含 JSON 转义形态）→ 现名；仅当出现在**路径段**中（前面是 / 或 \）才改
LEGACY_RE = re.compile("(?<=[\\\\/])SpeakTosi-AI(" + "\u8001\u5e08" + "|" + "\\\\u8001\\\\u5e08" + ")")

def walk_files():
    for dp, dn, fn in os.walk(ROOT):
        if any(s in dp for s in SKIP):
            dn[:] = []
            continue
        for f in fn:
            if f.endswith(EXTS) and f != pathlib.Path(__file__).name:
                yield pathlib.Path(dp) / f

def rewrite(txt):
    """返回 (新文本, 命中数)。斜杠风格随原文保留。"""
    n = 0
    for old in OLD_ROOTS:
        if old in txt:
            keep = "\\" if "\\" in old else "/"
            new = str(ROOT).replace("/", keep) if keep == "\\" else str(ROOT).replace("\\", "/")
            if new == old:
                continue
            n += txt.count(old)
            txt = txt.replace(old, new)
    cnt = len(LEGACY_RE.findall(txt))
    if cnt:
        n += cnt
        txt = LEGACY_RE.sub("coach_runs", txt)
    return txt, n

def main():
    print(f"[root] {ROOT}")
    changes = []
    for p in walk_files():
        try:
            t = p.read_text(encoding="utf-8")
        except Exception:
            continue
        new, n = rewrite(t)
        if n:
            try:
                json.loads(new) if p.suffix == ".json" else None
            except Exception as e:
                print(f"  !! 跳过(改后非合法JSON) {p}: {e}")
                continue
            changes.append((p, n, len(t), len(new)))
    if not changes:
        print("[OK] 无需修改：所有路径已指向当前根 ✓")
        return 0
    print(f"[{'改写' if APPLY else '待改'}] {len(changes)} 个文件 / {sum(c[1] for c in changes)} 处：")
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M")
    for p, n, a, b in changes:
        print(f"   {n:>3}处  {p.relative_to(ROOT)}  ({a}→{b}B)")
        if APPLY:
            try:
                shutil.copy2(p, str(p) + f".pathbak-{ts}")
                p.write_text(rewrite(p.read_text(encoding="utf-8"))[0], encoding="utf-8")
            except Exception as e:
                print(f"      !! 写入失败: {e}")
    if APPLY:
        print(f"[完成] 已备份为 *.pathbak-{ts}；可直接启动 start_speaktosi.vbs")
    else:
        print("[干跑] 加 --apply 才会实际写入")
    return 0

if __name__ == "__main__":
    sys.exit(main())