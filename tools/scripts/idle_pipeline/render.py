# -*- coding: utf-8 -*-
"""渲染 v3 (1010c): Brenda式真实眨眼序列 + 头部晃动加大 + 微笑呼吸起伏.

Owen 1010c 三条:
  1) '眨眼斜度太' (Kate/Eleanor): 单板alpha斜坡 = 沿羽化椭圆边的斜向淡出伪影.
     -> 改用 blink_seq 真实闭眼帧序列(逐帧走). 无seq则回退旧斜坡.
  2) 'Kai 肤色割裂': blink_seq 色彩锁只由环带(椭圆外静止皮肤)推出, 不拉眼睑肤色.
  3) '要头部晃动+笑容': IDLE_WARP 默认2.1 (旧v1_subtle太小'只看到脸动');
     嘴角0.4px呼吸起伏(幅度是眨眼板零头, 不触发微笑闸门).

blinkseq.json {ok,L,off}: seq[0]=睁开(seq[k]==plate_art 逐像素,区外铁律保证),
seq[off]=峰值闭合, seq[-1]=回到睁开. 渲染时**丢弃末帧**(它与基帧重复),
窗口长度 = L, 峰值在 off. 眨眼窗口 BLINK 间隔 >= L 保证互不重叠.
包络单调性(闸门5 blinkzone)由"seq本身过闸"保证: blinkzone v3 直接验 seq.
"""
import os, sys, json, subprocess
import numpy as np, cv2
from common import get_args

def main():
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    args = get_args()

    geom = json.load(open(os.path.join(args.work, 'geom.json')))
    mot = json.load(open(os.path.join(args.work, 'motion_report.json')))
    P = dict(mot['params'])

    PL = os.path.join(args.work, 'plates')
    def im(p): return cv2.imdecode(np.fromfile(p, np.uint8), cv2.IMREAD_COLOR)
    art = im(os.path.join(PL, 'art.png'))
    pm = im(os.path.join(PL, 'M.png'))
    px = im(os.path.join(PL, 'X.png'))

    H, W = art.shape[:2]
    T = args.frames
    FPS = args.fps

    # ---- 眨眼序列 (v3 主路径) ----
    seq = None
    sj = os.path.join(PL, 'blinkseq.json')
    if os.path.exists(sj):
        info = json.load(open(sj))
        if info.get('ok') and info.get('L', 0) >= 5:
            sd = os.path.join(PL, 'blinkseq')
            seq = [im(os.path.join(sd, '%04d.png' % k)) for k in range(info['L'])]
            soff = int(info['off'])
            print('BLINKSEQ path: L=%d off=%d (real frames, alpha=1)' % (len(seq), soff))
    if seq is None:
        print('BLINKSEQ missing -> alpha-ramp fallback')

    # ---- 头部晃动倍率 (Owen 1010c: 加大, 旧值'只看到脸动') ----
    warp = float(os.environ.get('IDLE_WARP', '2.1'))
    P['rot'] *= warp; P['x'] *= warp; P['y'] *= warp

    CPIV = (W * 0.5, H * 0.56)
    BPIV = (W * 0.5, float(H))
    EYE = [tuple(geom['eyec'][0]), tuple(geom['eyec'][1])]
    _bl = os.environ.get('IDLE_BLINK_AT')
    BLINK = [int(x) for x in _bl.split(',')] if _bl else [18, 62, 100]
    # GAZE 段严格避开 blinkseq 窗口(kai 14..27 / eleanor 58..63 / kate 51..60) -> 取 32..46
    GAZE = (32, 46)

    REF_EYEW = 33.5
    ew = 0.0
    try:
        _lms = __import__('face_recognition').face_landmarks(cv2.cvtColor(art, cv2.COLOR_BGR2RGB))
        if _lms:
            ew = float(np.mean([np.linalg.norm(np.array(p[0], float) - np.array(p[3], float))
                                for p in (_lms[0]['left_eye'], _lms[0]['right_eye'])]))
    except Exception:
        pass
    if ew <= 0:
        ew = REF_EYEW
    K = min(1.0, 0.35 * ew / REF_EYEW)
    _gk = os.environ.get('IDLE_GAZE_K')
    if _gk is not None:
        try:
            K = float(_gk)
        except ValueError:
            pass
    GS = (-2.6 * K, 0.7 * K)
    SIG2 = 288.0 * (ew / REF_EYEW) ** 2
    print('eye_w %.1fpx  gaze K=%.2f  disp=(%.2f,%.2f)  warp=%.2f' % (ew, K, GS[0], GS[1], warp))

    Y, X = np.mgrid[0:H, 0:W].astype(np.float32)
    def sm(t): return t * t * (3 - 2 * t)
    WH = sm(np.clip((620.0 - Y) / (620.0 - 480.0), 0, 1))
    WB = sm(np.clip((Y - 480.0) / (680.0 - 480.0), 0, 1))

    # ---- 微笑呼吸起伏 (Owen 1010c: '来点笑容'). 嘴角 geom.corners 两端反向提升.
    _corn = geom.get('corners')
    SMILE_ON = os.environ.get('IDLE_SMILE', '1') != '0'

    def field(t, gz):
        ph = 2 * np.pi * t / T
        th = np.radians(P['rot']) * (0.6 * np.sin(ph) + 0.4 * np.sin(2 * ph + 1.1))
        dx = P['x'] * (0.7 * np.sin(2 * ph + 0.6) + 0.3 * np.sin(ph + 2.0))
        dy = P['y'] * (0.8 * np.sin(ph + 0.3) + 0.2 * np.sin(2 * ph + 1.7))
        s = 1.0 + P['br'] * np.sin(ph + 0.9)
        cx, cy = CPIV; bx, by = BPIV
        rx = X - cx; ry = Y - cy
        sx = WH * (np.cos(th) * rx - np.sin(th) * ry - rx) + WH * dx
        sy = WH * (np.sin(th) * rx + np.cos(th) * ry - ry) + WH * dy
        sx = sx + WB * ((s - 1.0) * (X - bx)); sy = sy + WB * ((s - 1.0) * (Y - by))
        if gz > 0.0:
            for (ex, ey) in EYE:
                w = np.exp(-(((X - ex) ** 2 + (Y - ey) ** 2) / SIG2))
                sx = sx + gz * w * GS[0]
                sy = sy + gz * w * GS[1]
        if SMILE_ON and _corn:
            sw = 0.4 * (0.5 - 0.5 * np.cos(2 * np.pi * 2 * t / T))   # 2峰/周期
            for (cc, sg) in zip(_corn, (-1.0, 1.0)):
                w = np.exp(-(((X - cc[0]) ** 2 + (Y - cc[1]) ** 2) / (2 * 22.0 * 22.0)))
                sx = sx + w * sg * sw * 0.5
                sy = sy - w * sw
        return (X + sx).astype(np.float32), (Y + sy).astype(np.float32)

    # ---- 眨眼混合: 真实序列逐帧(alpha恒1), 无斜坡无斜度 ----
    _nb = os.environ.get('IDLE_BLINK_FRAMES')
    NUP, NDOWN = (3, 4) if _nb is None else ((2, 3) if _nb == '3' else (4, 5))
    bm = {}
    used = []
    for b in BLINK:
        if seq is not None:
            # v3.3.3(1010c): 不丢末帧——桥接末帧是全睁前最后一级, 丢它=裸露跳变(尾部pop).
            L = len(seq)
            if any(abs(b - b0p) < (L + soff + 2) for b0p in used):
                print('WARN blink@%d 窗口与上一个重叠 -> 该窗口改用 alpha 斜坡' % b)
                for i in range(1, NUP + 1):
                    u = i / float(NUP + 1)
                    bm[b - NUP + i - 1] = (px, float(np.sin(0.5 * np.pi * u) ** 2))
                bm[b] = (px, 1.0)
                for i in range(1, NDOWN + 1):
                    u = i / float(NDOWN + 1)
                    bm[b + i] = (px, float(1.0 - np.sin(0.5 * np.pi * u) ** 2))
                used.append(b)
                continue
            for k in range(len(seq) - 1):
                bm[b - soff + k] = (seq[k], 1.0)
            used.append(b)
        else:
            for i in range(1, NUP + 1):
                u = i / float(NUP + 1)
                bm[b - NUP + i - 1] = (px, float(np.sin(0.5 * np.pi * u) ** 2))
            bm[b] = (px, 1.0)
            for i in range(1, NDOWN + 1):
                u = i / float(NDOWN + 1)
                bm[b + i] = (px, float(1.0 - np.sin(0.5 * np.pi * u) ** 2))

    a0, b0 = GAZE
    def gzf(t):
        if t < a0 or t > b0: return 0.0
        u = (t - a0) / float(b0 - a0)
        return float(np.sin(np.pi * u) ** 2)

    md = 0.0
    n = 0
    print('Rendering final frames...')
    for t in range(T):
        e = bm.get(t)
        plate = art if e is None else (e[0] if e[1] >= 1.0 else cv2.addWeighted(art, 1.0 - e[1], e[0], e[1], 0.0))
        gz = 0.0 if e is not None else gzf(t)
        mx, my = field(t, gz)
        md = max(md, float(np.hypot(mx - X, my - Y).max()))
        o = cv2.remap(plate, mx, my, cv2.INTER_LANCZOS4, borderMode=cv2.BORDER_REPLICATE)
        cv2.imencode('.png', o)[1].tofile(os.path.join(args.out, '%04d.png' % t))
        n += 1

    print(f'Rendered {n} frames, max |disp| {md:.2f} px')

    ff = 'ffmpeg'
    mp4 = os.path.join(args.work, 'final.mp4')
    subprocess.run([ff, '-y', '-loglevel', 'error', '-framerate', str(FPS),
                    '-i', os.path.join(args.out, '%04d.png'),
                    '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-crf', '12', mp4], check=False)

    json.dump(dict(maxdisp=md, frames=n, blink=BLINK, gaze=[a0, b0], params=P, w=W, h=H,
                   gaze_k=K, gs=[GS[0], GS[1]], eye_w=ew, warp=warp,
                   seq=bool(seq is not None), seq_L=(len(seq) - 1 if seq is not None else 0),
                   seq_off=(soff if seq is not None else -1)),
              open(os.path.join(args.work, 'render_report.json'), 'w'), indent=1)
    print('WROTE', mp4)

if __name__ == '__main__':
    main()
