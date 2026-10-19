# -*- coding: utf-8 -*-
"""一键待机 idle 视频: 导师原画 -> 5 秒 idle(含自然眨眼) -> 装进人物包.

用法:
  python make_idle_video.py --avatar coach_eleanor            # 全流程
  python make_idle_video.py --coaches coach_eleanor coach_kate coach_kai
  python make_idle_video.py --avatar coach_eleanor --skip-gen # 已有源视频: 只渲染+安装
  python make_idle_video.py --avatar coach_x --src <mp4>      # 指定源视频

流程: 停 SpeakTosi 栈(腾显存; 有人连麦则拒绝) -> 起 QuickStudio/H3(17860) -> 出片 -> 抽帧
   -> 构图锁定归一(面部三点仿射, 治 i2v 推镜) -> r13 工序(眨眼板/呼吸微晃/渲染/三道闸门)
   -> 安装 customvideo/<id>_idle + custom_config_<id>.json -> 停 QS -> 拉起 SpeakTosi -> 台账
"""
import argparse, json, os, subprocess, sys, time, glob, pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
ENG = ROOT / "engine"
AV = ENG / "data" / "avatars"
CV = ENG / "data" / "customvideo"
SRC = ROOT / "coach_runs" / "idle_src"
WORK = ROOT / "coach_runs" / "idle_work"
PIPE = ROOT / "tools" / "scripts" / "idle_pipeline"
PYV = str(ENG / "venv" / "Scripts" / "python.exe")
PYW = str(ENG / "venv" / "Scripts" / "pythonw.exe")
WD = str(ENG / "aiteacher_watchdog.py")
QS = "http://127.0.0.1:17860"
QS_ROOT = r"D:\AI-ModeLs\Minimax.workbench-V260906"


def run(cmd, timeout=1800, env_clean=True, quiet=True):
    env = dict(os.environ)
    if env_clean:
        for k in ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP"):
            env.pop(k, None)
    env["PYTHONIOENCODING"] = "utf-8"
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
    if not quiet and r.returncode != 0:
        print((r.stdout or "")[-1500:]); print((r.stderr or "")[-1500:])
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def sh(cmd, timeout=120):
    r = subprocess.run(["powershell", "-NoProfile", "-Command", cmd],
                       capture_output=True, text=True, timeout=timeout)
    return (r.stdout or "") + (r.stderr or "")


def listeners():
    out = subprocess.run(["netstat", "-ano"], capture_output=True, text=True).stdout
    d = {}
    for ln in out.splitlines():
        f = ln.split()
        if len(f) >= 5 and f[3] == "LISTENING" and ":" in f[1]:
            # 注意: 旧版只认 127.0.0.1 前缀 -> 0.0.0.0 绑定的 8010/8011 永远"看不见"(假超时)
            try:
                d[int(f[1].rsplit(":", 1)[1])] = f[4]
            except Exception:
                pass
    return d


def wd_pids():
    out = sh("Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*aiteacher_watchdog*' "
             "-and ($_.Name -eq 'python.exe' -or $_.Name -eq 'pythonw.exe') } | Select-Object -ExpandProperty ProcessId")
    return [x.strip() for x in out.splitlines() if x.strip().isdigit()]


def ext_sessions():
    """占用 8011 的外部会话数(排除本机 electron 应用常驻画面)"""
    out = subprocess.run(["netstat", "-ano"], capture_output=True, text=True).stdout
    pids = set()
    for ln in out.splitlines():
        f = ln.split()
        if len(f) >= 5 and f[3] == "ESTABLISHED" and f[2].endswith(":8011"):
            pids.add(f[4])
    n = 0
    for p in pids:
        nm = subprocess.run(["tasklist", "/FI", "PID eq " + p, "/FO", "CSV", "/NH"],
                            capture_output=True, text=True).stdout.lower()
        if "electron" not in nm:
            n += 1
    return n


def stop_stack(force=False):
    n = ext_sessions()
    if n and not force:
        print("  [拒绝停机] 8011 有 %d 个外部会话(可能有人连麦). 确认无人后加 --force" % n)
        return False
    for p in wd_pids():
        sh("Stop-Process -Id %s -Force -ErrorAction SilentlyContinue" % p)
    time.sleep(2)
    ls = listeners()
    for port in (8011, 8010, 18080):
        if port in ls:
            sh("Stop-Process -Id %s -Force -ErrorAction SilentlyContinue" % ls[port])
    time.sleep(4)
    print("  栈已停; VRAM free=%s MiB" % free_vram_check())
    return True


def start_stack():
    ps = ("$env:PYTHONPATH=$null; $env:PYTHONHOME=$null; $env:PYTHONIOENCODING='utf-8'; "
          "Start-Process -FilePath '%s' -ArgumentList '%s' -WorkingDirectory '%s' -WindowStyle Hidden" % (PYW, WD, str(ENG)))
    sh(ps)
    t0 = time.time()
    while time.time() - t0 < 480:
        ls = listeners()
        if all(p in ls for p in (8011, 18080, 8765)):      # 8010 为卡通引擎, 重启窗口可能瞬时缺席 -> 只警告
            return True, int(time.time() - t0)
        time.sleep(15)
    return False, int(time.time() - t0)


def start_qs():
    if 17860 in listeners():
        print("  QuickStudio 已在 17860")
        return True
    bat = PIPE / "start_qs.bat"
    if not bat.exists():
        print("  [缺] %s" % bat)
        return False
    sh("Start-Process -FilePath '%s' -WindowStyle Hidden" % bat)
    t0 = time.time()
    while time.time() - t0 < 300:
        if 17860 in listeners():
            print("  QuickStudio UP (%ds)" % int(time.time() - t0))
            return True
        time.sleep(10)
    print("  [警告] QuickStudio 300s 未起")
    return False


def free_vram_check():
    o = subprocess.run(["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader"],
                       capture_output=True, text=True).stdout
    try:
        return int(o.strip().split()[0])
    except Exception:
        return -1


def one(avatar, src=None, skip_gen=False):
    art = AV / avatar / "full_imgs" / "00000000.png"
    if not art.exists():
        print("  [缺原画] %s" % art)
        return {"avatar": avatar, "ok": False, "why": "no art"}
    SRC.mkdir(parents=True, exist_ok=True)
    video = pathlib.Path(src) if src else (SRC / ("%s_h3.mp4" % avatar))
    if not src and not skip_gen:
        rc, out = run([PYV, str(PIPE / "h3_idle_gen.py"), "--avatar", avatar, "--qs", QS])
        print("  " + "\n  ".join([x for x in out.strip().splitlines() if x.strip()][-5:]))
        if rc != 0:
            return {"avatar": avatar, "ok": False, "why": "h3 gen rc=%d" % rc, "tail": out[-500:]}
    if not video.exists():
        return {"avatar": avatar, "ok": False, "why": "no src video"}
    fr = SRC / ("frames_%s" % avatar)
    if fr.exists():
        import shutil; shutil.rmtree(fr)
    fr.mkdir(parents=True)
    run(["ffmpeg", "-y", "-i", str(video), "-vsync", "0", str(fr / "%04d.png"), "-loglevel", "error"], 600)
    n = len(list(fr.glob("*.png")))
    print("  抽帧 %d" % n)
    nm = SRC / ("norm_%s" % avatar)
    rc, out = run([PYV, str(PIPE / "normalize_frames.py"), "--art", str(art), "--in", str(fr), "--out", str(nm)], 1800)
    try:
        log = json.loads([x for x in out.strip().splitlines() if x.startswith("{")][-1])
        print("  构图锁定归一 ok=%s noface=%d scale %.2f~%.2f" % (
            log.get("ok"), len(log.get("noface", [])), log.get("scale_min", 0), log.get("scale_max", 0)))
    except Exception:
        print("  归一输出: %s" % out[-200:])
    nv = SRC / ("norm_%s.mp4" % avatar)
    run(["ffmpeg", "-y", "-framerate", "25", "-i", str(nm / "%04d.png"), "-c:v", "libx264",
         "-pix_fmt", "yuv420p", "-crf", "12", str(nv), "-loglevel", "error"], 600)
    rc, out = run([PYV, str(PIPE / "idle_build.py"), "--avatar", avatar, "--src", str(nv)], 3000)
    if rc != 0:
        return {"avatar": avatar, "ok": False, "why": "idle_build rc=%d" % rc, "tail": out[-600:]}
    fin = WORK / avatar / "final"
    rc, out = run([PYV, str(PIPE / "idle_install.py"), "--avatar", avatar, "--frames", str(fin)], 900)
    installed = CV / (avatar + "_idle")
    nf = len(list(installed.glob("*.png"))) if installed.exists() else 0
    cfg = ENG / "data" / ("custom_config_%s.json" % avatar)
    ok = rc == 0 and nf > 0 and cfg.exists()
    print("  安装 %s: %d 帧  config=%s" % ("OK" if ok else "FAIL", nf, cfg.exists()))
    return {"avatar": avatar, "ok": ok, "frames": nf, "src": str(video), "installed": str(installed)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--avatar")
    ap.add_argument("--coaches", nargs="*")
    ap.add_argument("--src")
    ap.add_argument("--skip-gen", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--keep-running", action="store_true")
    a = ap.parse_args()
    avs = a.coaches or ([a.avatar] if a.avatar else [])
    if not avs:
        ap.error("--avatar 或 --coaches 必填")
    print("=" * 64)
    print("一键待机 idle 视频: %s" % ", ".join(avs))
    print("=" * 64)
    need_gen = (a.src is None) and (not a.skip_gen)
    print("VRAM free=%s MiB  监听=%s" % (free_vram_check(), listeners()))
    stopped = False
    if need_gen:
        if not stop_stack(a.force):
            sys.exit(3)
        stopped = True
        start_qs()
    rep = {}
    try:
        for av in avs:
            print("\n---------- %s ----------" % av)
            rep[av] = one(av, src=a.src, skip_gen=a.skip_gen)
    finally:
        if stopped and not a.keep_running:
            ls = listeners()
            for port in (17860, 8188):
                if port in ls:
                    sh("Stop-Process -Id %s -Force -ErrorAction SilentlyContinue" % ls[port])
            print("\n=== 停 QuickStudio, 拉起 SpeakTosi 栈 ===")
            ok, dt = start_stack()
            print("  服务 %s (%ds)" % ("UP" if ok else "TIMEOUT", dt))
    led = ROOT / "coach_runs" / ("IDLE_LEDGER_%s.json" % time.strftime("%m%d_%H%M"))
    json.dump(rep, open(str(led), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("\n台账: %s" % led)
    print("结果: %s" % json.dumps({k: v.get("ok") for k, v in rep.items()}, ensure_ascii=False))
    return 0 if all(v.get("ok") for v in rep.values()) else 4


if __name__ == "__main__":
    sys.exit(main())
