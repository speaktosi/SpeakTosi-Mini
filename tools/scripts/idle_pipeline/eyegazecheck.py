# -*- coding: utf-8 -*-
"""闸门4: 眼睛出界检查 —— 眼区局部滑动 / 脸颊对照 位移比.

固定像素量的"目光"形变在小脸上会被放大成"眼睛滑出眼窝"(Owen 1010 指出)。
Arthur(已接受基准, 眼宽33.5px) 比值 2.32; 未修正的新三位曾达 5.5-6.0 -> 肉眼即出界.
阈值 2.2, 超阈即 FAIL(渲染时 render.py 已按眼宽成比例缩放 gaze).
"""
import cv2, numpy as np, os, glob, json, argparse, sys
import face_recognition

def imu(p):
    return cv2.imdecode(np.fromfile(p, np.uint8), cv2.IMREAD_COLOR)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--art', required=True)
    ap.add_argument('--dir', required=True, help='成品帧目录')
    ap.add_argument('--thumb', type=float, default=2.2)
    a = ap.parse_args()
    art = imu(a.art); H, W = art.shape[:2]
    lms = face_recognition.face_landmarks(cv2.cvtColor(art, cv2.COLOR_BGR2RGB))
    if not lms:
        print(json.dumps({'ok': None, 'why': 'art face not found'})); return 0
    eyes = []
    for k in ('left_eye', 'right_eye'):
        p = np.array(lms[0][k], float)
        eyes.append((p.mean(axis=0)[0], p.mean(axis=0)[1], float(np.linalg.norm(p[0]-p[3]))))
    ew = float(np.mean([e[2] for e in eyes]))
    fs = sorted(glob.glob(os.path.join(a.dir, '*.png')))
    n = len(fs)
    if n < 4:
        print(json.dumps({'ok': False, 'why': 'frames<4'})); return 1
    # v3.1(1010c): GAZE 段 32..46; kai blinkseq 成品帧窗 ~11..24, 采样避开
    idx = [min(n - 1, max(0, int(round(x * n / 125.0)))) for x in (34, 38, 42, 46)]
    fr = [imu(fs[i]) for i in idx]
    ratios = []
    for x, y in zip(fr[:-1], fr[1:]):
        g0 = cv2.cvtColor(x, cv2.COLOR_BGR2GRAY); g1 = cv2.cvtColor(y, cv2.COLOR_BGR2GRAY)
        fl = cv2.calcOpticalFlowFarneback(g0, g1, None, 0.5, 3, 21, 3, 5, 1.2, 0)
        mag = np.hypot(fl[..., 0], fl[..., 1])
        em = np.zeros((H, W), bool)
        for (cx, cy, w) in eyes:
            r = max(8, int(w*0.9))
            em[int(max(0, cy-r)):int(min(H, cy+r)), int(max(0, cx-r)):int(min(W, cx+r))] = True
        cmx = np.mean([e[0] for e in eyes]); cmy = np.mean([e[1] for e in eyes]); wem = ew
        cm = np.zeros((H, W), bool)
        cm[int(min(H-1, cmy+wem*1.6)):int(min(H, cmy+wem*2.6)), int(max(0, cmx-wem)):int(min(W, cmx+wem))] = True
        e = float(mag[em].mean()); c = float(mag[cm].mean())
        ratios.append(e/max(c, 1e-3))
    r = float(np.mean(ratios))
    out = {'ok': r <= a.thumb, 'ratio': round(r, 2), 'thumb': a.thumb,
           'eye_w': round(ew, 1), 'ref_arthur_ratio': 2.32}
    try:
        rep = os.path.join(os.path.dirname(os.path.abspath(a.dir)), 'eyegazecheck_report.json')
        json.dump(out, open(rep, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    except Exception as e:
        print('warn: report write failed %s' % e)
    print(json.dumps(out, ensure_ascii=False))
    return 0 if out['ok'] else 1

if __name__ == '__main__':
    sys.exit(main())
