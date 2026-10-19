#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SpeakTosi 新增导师训练 —— Coach Eleanor / Kate / Kai (1010)

用法:
  python train_new_coaches.py           # 只做 CPU 预备: 原画 -> 绿幕静帧源视频 + 体检(不动服务)
  python train_new_coaches.py --go      # 全流程: 停看门狗/8011/TTS -> genavatar x3 -> 闸门 -> 拉起看门狗 -> 验证
  可选 --force  跳过"有人正在连麦"守卫(仅在你确认无人使用时)

产物: engine/data/avatars/coach_{eleanor,kate,kai}/ (musetalk 包)
      coach_runs/new3_src/coach_*_static.mp4 (源视频, 与 avator_info.video_path 对应)
"""
import os, sys, json, time, glob, shutil, subprocess

os.environ.pop("PYTHONPATH", None); os.environ.pop("PYTHONHOME", None)
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import numpy as np, cv2

ROOT = r"C:\Speaktosi"; ENG = os.path.join(ROOT, "engine")
AV = os.path.join(ENG, "data", "avatars")
PYV = os.path.join(ENG, "venv", "Scripts", "python.exe")
PYW = os.path.join(ENG, "venv", "Scripts", "pythonw.exe")
WD = os.path.join(ENG, "aiteacher_watchdog.py")
SRCDIR = os.path.join(ROOT, "coach_runs", "new3_src")
H, W, N, FPS = 896, 704, 125, 24
COACHES = [("coach_eleanor", "Coach-Eleanor"), ("coach_kate", "Coach-Kate"),
           ("coach_kai", "Coach-Kai")]
PORTS = (8010, 8011, 18080, 8765)
GO = "--go" in sys.argv; FORCE = "--force" in sys.argv

def run(a, t=120):
    try:
        r = subprocess.run(a, capture_output=True, text=True, timeout=t)
        return (r.stdout or "") + (r.stderr or "")
    except Exception as e:
        return "ERR %s" % e

def imu(p):
    return cv2.imdecode(np.fromfile(str(p), dtype=np.uint8), cv2.IMREAD_UNCHANGED)

def green_src(aid, png):
    """原画 RGBA -> 绿幕合成 (与 _r12_build.py 同公式) -> 125 帧静帧源视频"""
    art = imu(png)
    assert art.shape[2] == 4, "需要 RGBA: %s" % png
    bgr = cv2.resize(art[..., :3], (W, H), interpolation=cv2.INTER_AREA)
    al = cv2.resize(art[..., 3:4].astype(np.float32) / 255.0, (W, H), interpolation=cv2.INTER_AREA)
    if al.ndim == 2: al = al[..., None]
    raw = bgr.astype(np.float32); a2 = al[..., 0:1]
    fg = np.where(a2 > 0.25, (raw - 255.0 * (1.0 - a2)) / np.maximum(a2, 1e-6), raw)
    fg = np.clip(fg, 0, 255)
    op = (a2[..., 0] > 0.25).astype(np.uint8); fil = fg.copy(); kn = op.copy()
    for _ in range(10):
        d = cv2.dilate(fil, np.ones((3, 3), np.uint8)); dk = cv2.dilate(kn, np.ones((3, 3), np.uint8))
        q = (kn == 0) & (dk > 0); fil[q] = d[q]; kn[q] = 1
    comp = np.clip(fil * al + np.array([0, 255, 0], np.float32) * (1 - al), 0, 255).astype(np.uint8)
    os.makedirs(SRCDIR, exist_ok=True)
    tmp = os.path.join(SRCDIR, "_f_%s" % aid)
    if os.path.exists(tmp): shutil.rmtree(tmp)
    os.makedirs(tmp)
    for i in range(N):
        cv2.imencode(".png", comp)[1].tofile(os.path.join(tmp, "%04d.png" % i))
    mp4 = os.path.join(SRCDIR, "%s_static.mp4" % aid)
    ff = os.path.join(ENG, "ffmpeg.exe")
    if not os.path.exists(ff): ff = "ffmpeg"
    run([ff, "-y", "-loglevel", "error", "-framerate", str(FPS), "-i",
         os.path.join(tmp, "%04d.png"), "-c:v", "libx264", "-pix_fmt", "yuv420p",
         "-crf", "10", mp4], 300)
    shutil.rmtree(tmp, ignore_errors=True)
    gfrac = float((comp[:, :, 1] > 200).mean())
    print("  [%s] 源视频 %s  %d 帧  绿占比 %.1f%%  %.1fMB" %
          (aid, os.path.basename(mp4), N, gfrac * 100, os.path.getsize(mp4) / 1e6))
    return mp4

def listeners():
    d = {}
    for line in run(["netstat", "-ano"]).splitlines():
        p = line.split()
        if len(p) >= 5 and "LISTENING" in line:
            for port in PORTS:
                if p[1].endswith(":%d" % port): d[port] = int(p[4])
    return d

def est_conns():
    """到 8011 的**外部**活跃会话数。
    只统计"客户端不是 electron.exe"的连接 —— 本机桌面应用开着画面时也会常驻一条连接,
    那不是有人连麦。本机 electron 视角不算占用。"""
    n = 0
    for l in run(["netstat", "-ano"]).splitlines():
        f = l.split()
        if len(f) >= 5 and f[3] == "ESTABLISHED" and f[2].endswith(":8011"):
            if "electron" not in run(["tasklist", "/FI", "PID eq " + f[4], "/FO", "CSV", "/NH"]).lower():
                n += 1
    return n

def wd_pids():
    ps = ("Get-CimInstance Win32_Process -Filter \"Name='python.exe' or Name='pythonw.exe'\" "
          "| Where-Object { $_.CommandLine -like '*aiteacher_watchdog*' } | ForEach-Object { $_.ProcessId }")
    return [int(x) for x in run(["powershell", "-NoProfile", "-Command", ps]).split() if x.isdigit()]

def relaunch_wd():
    ps = ("$env:PYTHONPATH=$null; $env:PYTHONHOME=$null; $env:PYTHONIOENCODING='utf-8'; "
          "Start-Process -FilePath '%s' -ArgumentList '%s' -WorkingDirectory '%s' -WindowStyle Hidden" % (PYW, WD, ENG))
    return run(["powershell", "-NoProfile", "-Command", ps], 30)

def vram():
    o = run(["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader"])
    try: return int(o.strip().split()[0])
    except Exception: return -1

def genavatar(mp4, aid):
    """引擎 venv 跑 musetalk genavatar (与 _r12_deploy.py 同调用)"""
    code = ("import os,sys\n"
            'sys.path.insert(0, os.environ["PYTHONPATH"])\n'
            'os.chdir(os.environ["PYTHONPATH"])\n'
            "from avatars.musetalk.genavatar import generate_avatar\n"
            "vid = sys.argv[1]; aid = sys.argv[2]\n"
            'generate_avatar(vid, aid, save_path="data/avatars", bbox_shift=0, extra_margin=10, parsing_mode="jaw", version="v15")\n')
    env = {k: v for k, v in os.environ.items() if k.upper() not in ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP")}
    env["PYTHONPATH"] = ENG
    t0 = time.time()
    p = subprocess.run([PYV, "-c", code, mp4, aid], cwd=ENG, env=env,
                       capture_output=True, text=True, timeout=2400)
    out = (p.stdout or "") + (p.stderr or "")
    print("  genavatar rc=%s  %ds" % (p.returncode, int(time.time() - t0)))
    return p.returncode == 0, out[-900:]

def gate(aid, mp4):
    d = os.path.join(AV, aid)
    need = ["latents.pt", "coords.pkl", "mask_coords.pkl"]
    has = all(os.path.exists(os.path.join(d, f)) for f in need)
    nf = len(glob.glob(os.path.join(d, "full_imgs", "*.png")))
    hasmask = os.path.isdir(os.path.join(d, "mask"))
    ok = has and nf >= 1 and hasmask
    if ok:
        info = {"avatar_id": aid, "bbox_shift": 0,
                "video_path": os.path.join(ROOT, "coach_runs", "new3_src", "%s_static.mp4" % aid)}
        with open(os.path.join(d, "avator_info.json"), "w", encoding="utf-8") as f:
            json.dump(info, f, ensure_ascii=False, indent=1)
        src = os.path.join(ROOT, "avatar", {"coach_eleanor": "Coach-Eleanor",
                                            "coach_kate": "Coach-Kate",
                                            "coach_kai": "Coach-Kai"}.get(aid, "") + ".png")
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(d, "cover.png"))
    size = sum(os.path.getsize(p) for p in glob.glob(os.path.join(d, "**", "*"), recursive=True) if os.path.isfile(p))
    print("  [%s] 闸门 %s  artifacts=%s full_imgs=%d mask=%s 体积=%.1fMB" %
          (aid, "PASS" if ok else "FAIL", has, nf, hasmask, size / 1e6))
    return ok, {"artifacts": has, "full_imgs": nf, "mask": hasmask, "mb": round(size / 1e6, 1)}

def wait_port(port, timeout=420):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if port in listeners(): return True, int(time.time() - t0)
        time.sleep(10)
    return False, int(time.time() - t0)

def synth(voice, text="Hello, this is a quick voice check for the new teacher."):
    import urllib.request
    body = json.dumps({"model": "qwen3-tts", "input": text,
                       "voice": voice, "response_format": "wav"}).encode()
    r = urllib.request.Request("http://127.0.0.1:18080/v1/audio/speech", data=body,
                               headers={"Content-Type": "application/json"})
    try:
        return urllib.request.urlopen(r, timeout=180).read(), None
    except Exception as e:
        return b"", "%s: %s" % (type(e).__name__, str(e)[:140])

def api(path):
    import urllib.request
    try:
        return json.loads(urllib.request.urlopen("http://127.0.0.1:8765" + path, timeout=20).read().decode("utf-8", "replace")), None
    except Exception as e:
        return None, "%s: %s" % (type(e).__name__, str(e)[:120])

def main():
    print("=" * 64)
    print("SpeakTosi 新增导师训练: Eleanor / Kate / Kai   mode=%s" % ("GO(会停服)" if GO else "PREP(只做准备)"))
    print("=" * 64)
    print("=== 1) 原画 -> 绿幕静帧源视频 (CPU, 不动服务) ===")
    srcs = {}
    for aid, name in COACHES:
        png = os.path.join(ROOT, "avatar", name + ".png")
        if not os.path.exists(png):
            print("  [%s] 缺原画: %s" % (aid, png)); return 2
        srcs[aid] = green_src(aid, png)
    print("\n=== 2) 环境体检 ===")
    print("  VRAM free = %s MiB   监听 = %s" % (vram(), listeners()))
    n = est_conns()
    print("  到 8011 的活跃会话 = %d %s" % (n, "(可能有人正在连麦)" if n else "(空闲)"))
    if not GO:
        print("\n[PREP 完成] 未动任何服务。要建包: python train_new_coaches.py --go")
        return 0
    if n > 0 and not FORCE:
        print("\n[拒绝停机] 有 %d 条活跃连接, 可能有人正在连麦。确认无人后加 --force。" % n)
        return 3
    print("\n=== 3) 停 看门狗 + 8011 + TTS (保 8010) ===")
    for pid in wd_pids():
        print("  kill wd", pid, run(["taskkill", "/F", "/PID", str(pid)]).strip()[:40])
    time.sleep(2)
    ls = listeners()
    for port in (8011, 18080):
        if port in ls:
            print("  kill", port, ls[port], run(["taskkill", "/F", "/T", "/PID", str(ls[port])]).strip().replace("\n", " ")[:70])
    time.sleep(3)
    print("  停后 监听=%s   VRAM free=%s MiB" % (listeners(), vram()))
    print("\n=== 4) genavatar x3 (GPU, 每个 1-3 分钟) ===")
    rep = {}
    for aid, name in COACHES:
        print("-- %s" % aid)
        okc, tail = genavatar(srcs[aid], aid)
        if not okc: print("   失败尾巴: %s" % tail[-500:])
        gk, info = gate(aid, srcs[aid]); info["genavatar_rc"] = okc; rep[aid] = info
    print("\n=== 5) 拉起看门狗 -> 冷启 ===")
    relaunch_wd()
    for port in (8011, 8010, 18080):
        ok, dt = wait_port(port); print("  port %d %s (%ds)" % (port, "UP" if ok else "TIMEOUT", dt))
    print("\n=== 5b) 重启 app 后端(8765) 让 AVATARS 重载 (应用可探测式自愈) ===")
    ls = listeners()
    if 8765 in ls:
        print("  kill 8765", ls[8765], run(["taskkill", "/F", "/PID", str(ls[8765])]).strip()[:50])
        time.sleep(5)
        ok, dt = wait_port(8765, 150)
        print("  8765 %s (%ds)" % ("UP" if ok else "TIMEOUT", dt))
    print("\n=== 6) 验证 ===")
    for voice in ("eleanor", "kate", "kai"):
        b, err = synth(voice)
        print("  TTS %-8s %s" % (voice, ("OK %dB" % len(b)) if not err else "ERR " + err))
    av, e1 = api("/avatars"); vs, e2 = api("/voices")
    if av is not None:
        lst = av if isinstance(av, list) else av.get("avatars", [])
        ids = [a.get("avatar_id") for a in lst if isinstance(a, dict)]
        print("  /avatars = %d 项 新3位在否: %s" % (len(ids), [i for i in ids if i in ("coach_eleanor", "coach_kate", "coach_kai")]))
    else: print("  /avatars ERR", e1)
    if vs is not None:
        lst = vs if isinstance(vs, list) else vs.get("voices", [])
        names = [v.get("id") or v.get("name") for v in lst if isinstance(v, dict)]
        print("  /voices = %d 项 新声线在否: %s" % (len(names), [x for x in names if x in ("eleanor", "kate", "kai")]))
    else: print("  /voices ERR", e2)
    rp = os.path.join(ROOT, "coach_runs", "new3_train_report.json")
    with open(rp, "w", encoding="utf-8") as f:
        json.dump(rep, f, ensure_ascii=False, indent=1)
    print("\n报告: %s" % rp)
    return 0 if all(v.get("artifacts") for v in rep.values()) else 4

if __name__ == "__main__":
    sys.exit(main())