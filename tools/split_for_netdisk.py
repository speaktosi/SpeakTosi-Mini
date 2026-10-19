#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把 zip 切成百度网盘能吃下的分卷(默认 3.9G/卷, 免费账号单文件上限 4G 也稳), 并生成:
  <zip>.001 <zip>.002 ...            —— 原样字节分卷, 上传这些
  1-先双击我合并.bat                  —— 同事/你解压前跑一次, copy /b 拼回原 zip
  2-校验说明.txt                      —— 各卷 MD5 + 合并后 zip 的 MD5 + 收件方操作步骤
用法:
  python tools/split_for_netdisk.py <zip路径> [输出目录] [--part-mb 3900]
"""
from __future__ import annotations
import argparse
import hashlib
import os
import sys

CHUNK = 8 * 1024 * 1024


def md5_of(path: str, limit: int | None = None) -> str:
    h = hashlib.md5()
    left = limit
    with open(path, "rb") as f:
        while True:
            n = CHUNK if left is None else min(CHUNK, left)
            if n <= 0:
                break
            b = f.read(n)
            if not b:
                break
            h.update(b)
            if left is not None:
                left -= len(b)
    return h.hexdigest()


def split(zip_path: str, out_dir: str, part_mb: int) -> dict:
    size = os.path.getsize(zip_path)
    part = part_mb * 1024 * 1024
    n_parts = (size + part - 1) // part
    base = os.path.basename(zip_path)
    os.makedirs(out_dir, exist_ok=True)

    parts = []
    with open(zip_path, "rb") as src:
        for i in range(1, n_parts + 1):
            dst_name = "%s.%03d" % (base, i)
            dst = os.path.join(out_dir, dst_name)
            remaining = min(part, size - (i - 1) * part)
            with open(dst, "wb") as out:
                while remaining > 0:
                    b = src.read(min(CHUNK, remaining))
                    if not b:
                        raise IOError("源文件提前结束: %s" % zip_path)
                    out.write(b)
                    remaining -= len(b)
            parts.append((dst_name, os.path.getsize(dst)))
            print("  [%d/%d] %s  %.2f GB" % (i, n_parts, dst_name, parts[-1][1] / 1024**3), flush=True)

    # 1-先双击我合并.bat
    copy_expr = "+".join('"%s.%03d"' % (base, i) for i in range(1, n_parts + 1))
    bat = f"""@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================================
echo   第一步: 把 {n_parts} 个分卷拼回一个完整 zip
echo   (文件较大, 需要几分钟; 期间别关窗口)
echo ============================================================
echo.
if exist "{base}" (
  echo [!] 已经存在 {base} 了 —— 若它不是上次拼坏的, 请先删掉再跑本脚本。
  pause & exit /b 1
)
copy /b {copy_expr} "{base}"
if errorlevel 1 (
  echo.
  echo [X] 合并失败 —— 请确认 {n_parts} 个分卷都在本目录里, 且文件名没被改过。
  pause & exit /b 1
)
echo.
echo [OK] 合并完成: {base}
echo.
echo   下一步: 用解压软件(7-Zip / WinRAR / 好压 / 系统自带)打开这个 zip,
echo           解压到【英文、无空格】的路径, 例如 D:\\SpeakTosi
echo           然后双击解压出的 start_speaktosi.vbs 启动。
echo.
echo   校验(可选): 用记事本打开 2-校验说明.txt 对照 MD5。
pause
"""
    bat_path = os.path.join(out_dir, "1-先双击我合并.bat")
    with open(bat_path, "w", encoding="utf-8-sig") as f:
        f.write(bat)
    # 2-校验说明.txt
    tot = sum(s for _, s in parts)
    lines = [
        "SpeakTosi 分卷包 · 校验与使用说明",
        "=" * 46,
        "",
        "为什么分卷: 百度网盘对单文件有大小上限, 分卷(每卷 %.1f GB)任何账号都能上传/下载。" % (part_mb / 1024),
        "",
        "本目录内容:",
        "  1-先双击我合并.bat     <- 第一个动作: 双击它, 自动把分卷拼回完整 zip",
        "  %s.001 ... %s.%03d  <- 全部 %d 卷, 缺一不可" % (base, base, n_parts, n_parts),
        "  2-校验说明.txt         <- 本文件(含每卷 MD5)",
        "",
        "原 zip 大小: %d 字节 (%.2f GB)" % (size, size / 1024**3),
        "分卷合计:   %d 字节 (%.2f GB)   %s" % (tot, tot / 1024**3, "一致 ✔" if tot == size else "不一致 ✘"),
        "",
        "各卷 MD5:",
    ]
    for nm, sz in parts:
        print("  计算 %s 的 MD5 ..." % nm, flush=True)
        lines.append("  %s  %12d  %s" % (nm, sz, md5_of(os.path.join(out_dir, nm))))
    lines += [
        "",
        "完整 zip 的 MD5(合并后自检用):",
        "  %s  %s" % (base, md5_of(zip_path)),
        "",
        "Windows 下核对 MD5 的方法:",
        "  certutil -hashfile \"文件名\" MD5",
        "",
        "使用步骤:",
        "  1) 确认分卷数量 = %d, 文件名以 .001 结尾连续编号" % n_parts,
        "  2) 双击 1-先双击我合并.bat, 等它打印 [OK]",
        "  3) 用解压软件把 %s 解压到英文无空格路径" % base,
        "  4) 解压出的 SETUP.bat 可先跑一次自检(排障用), 然后双击 start_speaktosi.vbs",
        "  5) 首次启动会弹出【云端 Token 配置】窗口, 粘贴自己的 Key 点\"保存并测试\"",
        "",
    ]
    with open(os.path.join(out_dir, "2-校验说明.txt"), "w", encoding="utf-8-sig") as f:
        f.write("\n".join(lines))
    return {"zip": zip_path, "parts": parts, "n_parts": n_parts, "size": size, "bat": bat_path}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("zip")
    ap.add_argument("outdir", nargs="?", default="")
    ap.add_argument("--part-mb", type=int, default=3900)     # 3.9G < 免费账号 4G 上限
    a = ap.parse_args()
    z = os.path.abspath(a.zip)
    out = a.outdir or os.path.join(os.path.dirname(z), os.path.splitext(os.path.basename(z))[0] + "_split")
    print("[split] %s -> %s  每卷 %d MB" % (z, out, a.part_mb))
    r = split(z, out, a.part_mb)
    print("[split] 完成: %d 卷, 合计 %.2f GB, 目录 %s" % (r["n_parts"], sum(s for _, s in r["parts"]) / 1024**3, out))
    return 0


if __name__ == "__main__":
    sys.exit(main())