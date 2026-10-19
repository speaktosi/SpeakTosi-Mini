# -*- coding: utf-8 -*-
"""H3 图生视频出片器: 导师原画 -> 5 秒 idle 源视频(含自然眨眼).

用法:
  python h3_idle_gen.py --avatar coach_eleanor
  python h3_idle_gen.py --avatar coach_eleanor --art <png> --qs http://127.0.0.1:17860 --dur 5

产出: coach_runs/idle_src/<avatar>_h3.mp4  (供 r13 眨眼/待机流水线使用)
说明: 需要 QuickStudio(H3) 已在 --qs 端口运行; 显存不足时先停 SpeakTosi 栈.
"""
import argparse, json, os, sys, time, uuid, urllib.request, pathlib

R = pathlib.Path(r"C:\Speaktosi")
HERE = pathlib.Path(__file__).parent
PROMPT_FILE = HERE / "idle_prompt_zh.txt"


def http_json(url, obj=None, method=None, timeout=60):
    data = json.dumps(obj).encode() if obj is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8", "replace"))


def upload_image(qs, path):
    boundary = "----h3idle" + uuid.uuid4().hex
    raw = open(path, "rb").read()
    parts = []
    for name, val in (("asset_type", "image"), ("slot", "asset")):
        parts.append(("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n" % (boundary, name, val)).encode())
    parts.append(("--%s\r\nContent-Disposition: form-data; name=\"file\"; filename=\"%s\"\r\nContent-Type: image/png\r\n\r\n"
                  % (boundary, os.path.basename(path))).encode() + raw + b"\r\n")
    parts.append(("--%s--\r\n" % boundary).encode())
    body = b"".join(parts)
    req = urllib.request.Request(qs + "/api/v1/assets", data=body,
                                 headers={"Content-Type": "multipart/form-data; boundary=" + boundary})
    return json.loads(urllib.request.urlopen(req, timeout=180).read().decode("utf-8", "replace"))


def art_path(avatar, override=None):
    if override:
        return pathlib.Path(override)
    return R / "engine" / "data" / "avatars" / avatar / "full_imgs" / "00000000.png"


def snap32(v):
    return max(256, min(2160, int(round(v / 32.0)) * 32))


def submit(qs, asset_id, w, h, prompt, dur, seed=-1, quality="balanced"):
    job = {"feature": "minimax-h3", "mode": "i2v",
           "inputs": {"prompt": prompt, "first_frame": asset_id},
           "parameters": {"width": w, "height": h, "duration": dur,
                          "quality_mode": quality, "model_mode": "high_quality", "seed": seed}}
    r = http_json(qs + "/api/v1/jobs", job, timeout=180)
    jid = r.get("job_id") or r.get("id") or (r.get("job") or {}).get("job_id")
    if not jid:
        raise SystemExit("建任务失败: %s" % json.dumps(r, ensure_ascii=False)[:400])
    return jid, r


def wait_job(qs, jid, timeout=2400):
    t0 = time.time(); last = None
    while time.time() - t0 < timeout:
        try:
            st = http_json("%s/api/v1/jobs/%s" % (qs, jid), timeout=60)
        except Exception:
            time.sleep(10); continue
        s = (st.get("status") or st.get("state") or "").lower()
        pr = st.get("progress")
        if pr != last:
            print("   %s %s" % (s, pr), flush=True); last = pr
        if s in ("succeeded", "success", "completed", "done", "finished"):
            return st
        if s in ("failed", "error", "canceled", "cancelled"):
            raise SystemExit("任务失败: %s" % json.dumps(st, ensure_ascii=False)[:500])
        time.sleep(15)
    raise SystemExit("任务超时(%ds)" % timeout)


def fetch_output(qs, jid, dst):
    outs = http_json("%s/api/v1/jobs/%s/outputs" % (qs, jid), timeout=180)
    url = []

    def walk(o):
        if isinstance(o, dict):
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
        elif isinstance(o, str):
            if o.lower().endswith((".mp4", ".webm")) or "/outputs/" in o or "/files/" in o:
                url.append(o)
    walk(outs)
    if not url:
        raise SystemExit("未找到输出视频: %s" % json.dumps(outs, ensure_ascii=False)[:400])
    # 优先带查询参数的 /view 端点: ComfyUI 的 /video/<file>.mp4 直链会返回错误页(假视频).
    # 下载后校验字节数, <100KB 视为失败, 依次换下一个候选 URL.
    cands = [x for x in url if '?' in x] + [x for x in url if '?' not in x]
    pathlib.Path(dst).parent.mkdir(parents=True, exist_ok=True)
    last_err = None
    for cu in cands:
        u = cu if cu.startswith("http") else qs + "/" + cu.lstrip("/")
        try:
            with urllib.request.urlopen(u, timeout=900) as r:
                data = r.read()
        except Exception as e:
            last_err = e; continue
        if len(data) >= 100_000:
            with open(dst, "wb") as f:
                f.write(data)
            return dst, u
        last_err = "too small (%d bytes from %s)" % (len(data), u)
    raise SystemExit("下载输出视频失败: %s / %s" % (last_err, url[:3]))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--avatar", required=True)
    ap.add_argument("--art")
    ap.add_argument("--qs", default="http://127.0.0.1:17860")
    ap.add_argument("--dur", type=float, default=5)
    ap.add_argument("--seed", type=int, default=-1)
    ap.add_argument("--quality", default="balanced")
    ap.add_argument("--out")
    ap.add_argument("--prompt-file")
    a = ap.parse_args()
    qs = a.qs.rstrip("/")
    art = art_path(a.avatar, a.art)
    if not art.exists():
        raise SystemExit("找不到原画: %s" % art)
    import cv2, numpy as np
    img = cv2.imdecode(np.fromfile(str(art), np.uint8), cv2.IMREAD_COLOR)
    h, w = img.shape[:2]
    W, H = snap32(w), snap32(h)
    pf = pathlib.Path(a.prompt_file) if a.prompt_file else PROMPT_FILE
    prompt = pf.read_text(encoding="utf-8")
    out = a.out or str(R / "coach_runs" / "idle_src" / ("%s_h3.mp4" % a.avatar))
    print("=== H3 idle 出片 ===")
    print("  原画 %s (%dx%d) -> 生成 %dx%d %ss" % (art.name, w, h, W, H, a.dur))
    up = upload_image(qs, str(art))
    aid = up.get("asset_id") or up.get("id")
    if not aid:
        raise SystemExit("上传失败: %s" % json.dumps(up, ensure_ascii=False)[:300])
    print("  上传 -> asset_id=%s" % aid)
    jid, raw = submit(qs, aid, W, H, prompt, a.dur, a.seed, a.quality)
    print("  job=%s %s" % (jid, json.dumps(raw, ensure_ascii=False)[:150]))
    t0 = time.time()
    wait_job(qs, jid)
    print("  生成完成 %ds" % int(time.time() - t0))
    p, u = fetch_output(qs, jid, out)
    print("  视频 -> %s (%d bytes)" % (p, os.path.getsize(p)))
    json.dump({"avatar": a.avatar, "job_id": jid, "url": u, "out": str(p), "art": str(art),
               "size": [W, H], "dur": a.dur, "seed": a.seed},
              open(str(R / "coach_runs" / "idle_src" / ("%s_h3.json" % a.avatar)), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
