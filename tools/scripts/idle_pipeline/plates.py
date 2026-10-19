# -*- coding: utf-8 -*-
"""眨眼板 v2 (1010b): 贴板区严格限制在眼部椭圆 + 环带仅平移配准 + 对称闸门 + 合成眼睑兜底.

Owen 1010b 诊断: Kate/Eleanor 的 H3 源视频镜头没锁死 -> i2v 逐帧重绘眉毛/眼袋/整块眼窝.
旧版"矩形差异mask"把重绘区整块贴进眨眼板, 眨眼=整块眼窝换贴图(正片区域在动+重影),
且 M/X 两块不同重绘内容的板交替混合 -> 双闪振荡.

v2 规则:
  1) 贴板 mask = 左右眼椭圆(盖上下睑+睫毛, 排除眉毛/脸颊). 椭圆外逐像素 == plate_art.
  2) 贴前对'环带'(眼周矩形 - 椭圆膨胀)做 TRANSLATION-only ECC 消残余平移;
     残余平移 > ALIGN_MAX_PX 判不合格.
  3) 椭圆内差异区逐通道统计对齐到 plate_art (去亮度/色彩漂移).
  4) 对称闸门: X/M 板两眼变化像素比 < SYM_MIN -> 判'单眼游走' -> 该板退化'合成眼睑'
     (眼椭圆区镜像眼下皮肤纹理 + 闭眼弧线睫毛线; 双眼绝对对称, 眉毛零改动).
"""
import os, sys, json
import numpy as np, cv2, face_recognition as fr
from common import get_args

ALIGN_MAX_PX = 4.0      # 环带配准后残余平移上限 (1010b 标定: Kai 3.58 合格基准 -> 取4.0)
SYM_MIN = 0.35          # 板两眼变化量对称下限
MIN_EYE_PX = 120        # 每眼最少闭合变化像素(否则这板没用)


def eye_geom(pts):
    """pts: 6点眼轮廓 -> (cx, cy, 眼宽ew, 上睑椭圆, 下睑椭圆)"""
    e = np.array(pts, float)
    cx, cy = e.mean(0)
    ew = float(np.linalg.norm(e[0] - e[3]))
    up = (int(round(cx)), int(round(cy - 0.06 * ew)), max(2, int(0.74 * ew)), max(2, int(0.46 * ew)))
    lo = (int(round(cx)), int(round(cy + 0.13 * ew)), max(2, int(0.74 * ew)), max(2, int(0.38 * ew)))
    return float(cx), float(cy), ew, up, lo


def make_ell_mask(H, W, eyes, scale=1.0):
    m = np.zeros((H, W), np.uint8)
    for (cx, cy, ew, up, lo) in eyes:
        for (ex, ey, ax, ay) in (up, lo):
            cv2.ellipse(m, (ex, ey), (max(2, int(ax * scale)), max(2, int(ay * scale))), 0, 0, 360, 255, -1)
    return m


def main():
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    args = get_args()
    scan_info = json.load(open(os.path.join(args.work, 'eyescan.json')))
    VX, VM = scan_info['VX'], scan_info['VM']

    img = cv2.imdecode(np.fromfile(args.art, np.uint8), cv2.IMREAD_COLOR)
    H, W = img.shape[:2]
    bb_list = fr.face_landmarks(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))
    if not bb_list:
        print("Error: Could not detect face in art image.")
        sys.exit(1)
    bb = bb_list[0]
    le = np.array(bb['left_eye'], float)
    re_ = np.array(bb['right_eye'], float)
    tl = np.array(bb['top_lip'], float)
    bl = np.array(bb['bottom_lip'], float)
    mouth = np.vstack([tl, bl])

    EYEC = (le.mean(0), re_.mean(0))
    MOUTHC = mouth.mean(0)
    eb = np.vstack([le, re_])
    ex0, ex1 = int(eb[:, 0].min()) - 14, int(eb[:, 0].max()) + 14
    ey0, ey1 = int(eb[:, 1].min()) - 12, int(eb[:, 1].max()) + 10

    geom = dict(
        eyec=[[float(v) for v in EYEC[0]], [float(v) for v in EYEC[1]]],
        mouthc=[float(v) for v in MOUTHC],
        ebox=[ex0, ex1, ey0, ey1],
        corners=[[float(v) for v in tl[0]], [float(v) for v in tl[6]]],
        lids=[[float(v) for v in le[[5, 4]].mean(0)], [float(v) for v in re_[[5, 4]].mean(0)]]
    )
    json.dump(geom, open(os.path.join(args.work, 'geom.json'), 'w'), indent=1)

    # ---- 基础 plate_art: 微笑/暖眼微提形变(与旧版一致) ----
    Y, X = np.mgrid[0:H, 0:W].astype(np.float32)

    def gauss(c, s):
        return np.exp(-(((X - c[0]) ** 2 + (Y - c[1]) ** 2) / (2.0 * s * s)))
    sdx = np.zeros((H, W), np.float32)
    sdy = np.zeros((H, W), np.float32)

    def add(c, s, dxv, dyv):
        w = gauss(c, s)
        sdx[:] += w * dxv
        sdy[:] += w * dyv

    add(tl[0], 18, -1.0, -3.6)
    add(tl[6], 18, 1.0, -3.6)
    add(tl[3], 20, 0, -1.1)
    add(bl[3], 20, 0, 0.6)
    add((mouth[:, 0].min() - 18, MOUTHC[1] - 14), 22, 0, -1.8)
    add((mouth[:, 0].max() + 18, MOUTHC[1] - 14), 22, 0, -1.8)
    add(le[[5, 4]].mean(0), 8, 0, -0.8)
    add(re_[[5, 4]].mean(0), 8, 0, -0.8)
    plate_art = cv2.remap(img, (X + sdx).astype(np.float32), (Y + sdy).astype(np.float32),
                          cv2.INTER_LANCZOS4, borderMode=cv2.BORDER_REPLICATE)

    plates_dir = os.path.join(args.work, 'plates')
    os.makedirs(plates_dir, exist_ok=True)
    cv2.imencode('.png', plate_art)[1].tofile(os.path.join(plates_dir, 'art.png'))

    # ---- 眼部椭圆 / 环带 ----
    eyes = [eye_geom(le), eye_geom(re_)]
    m_ell = make_ell_mask(H, W, eyes, 1.0)
    m_zone = cv2.dilate(m_ell, np.ones((15, 15), np.uint8))    # 允许差异的最大区(含羽化尾巴)
    m_ell_dil = cv2.dilate(m_ell, np.ones((11, 11), np.uint8))
    rx0, rx1 = max(0, ex0 - 40), min(W, ex1 + 40)
    ry0, ry1 = max(0, ey0 - 45), min(H, ey1 + 40)
    ring = np.zeros((H, W), bool)
    ring[ry0:ry1, rx0:rx1] = True
    ring &= (m_ell_dil == 0)
    m_eye = []
    for (cx, cy, ew, up, lo) in eyes:
        m1 = np.zeros((H, W), np.uint8)
        for (ex, ey, ax, ay) in (up, lo):
            cv2.ellipse(m1, (ex, ey), (ax, ay), 0, 0, 360, 255, -1)
        m_eye.append(m1 > 0)

    def readvid(p, idx):
        cap = cv2.VideoCapture(p)
        f, k = None, 0
        while True:
            ok, a = cap.read()
            if not ok:
                break
            if k == idx:
                f = a
                break
            k += 1
        cap.release()
        return f

    def align_translation(f0):
        """环带 TRANSLATION-only ECC; 椭圆内置平不让眼闭合干扰."""
        g0 = cv2.cvtColor(plate_art, cv2.COLOR_BGR2GRAY).astype(np.float32)
        g1 = cv2.cvtColor(f0, cv2.COLOR_BGR2GRAY).astype(np.float32)
        base = float(g0[ry0:ry1, rx0:rx1].mean())
        A = g0.copy(); B = g1.copy()
        A[~ring] = base; B[~ring] = base
        M0 = np.eye(2, 3, dtype=np.float32)
        try:
            cv2.findTransformECC(A, B, M0, cv2.MOTION_TRANSLATION,
                                 (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 300, 1e-6), None, 3)
        except Exception:
            M0 = np.eye(2, 3, dtype=np.float32)
        if abs(M0[0, 2]) > 150 or abs(M0[1, 2]) > 150:
            M0 = np.eye(2, 3, dtype=np.float32)
        w = cv2.warpAffine(f0, M0, (W, H), flags=cv2.INTER_LANCZOS4, borderMode=cv2.BORDER_REPLICATE)
        return w, float(np.hypot(M0[0, 2], M0[1, 2]))

    def composite(wframe):
        """整椭圆贴'已对齐闭眼帧'(羽化边), 椭圆外逐像素 == plate_art.
        1010b 修正: 旧版只贴'差异检出像素' -> 闭眼帧里与原画接近的像素会漏贴,
        睁开态的睫毛线/虹膜残留成重影. 整椭圆覆盖后睁开像素不可能残留."""
        d = np.linalg.norm(cv2.GaussianBlur(wframe, (0, 0), 2.6).astype(np.float32)
                           - cv2.GaussianBlur(plate_art, (0, 0), 2.6).astype(np.float32), axis=2)
        n_diff = int(((d > 8) & (m_ell > 0)).sum())       # 仅作对称闸门度量
        pt = wframe.astype(np.float32).copy()
        # 椭圆内逐通道统计对齐: 均值锁回 plate_art, 对比度限幅 [0.6,1.5] (不放大噪声)
        for ch in range(3):
            sel = m_ell > 0
            sp = pt[..., ch][sel].astype(np.float32)
            dp = plate_art[..., ch].astype(np.float32)[sel]
            if len(sp) == 0:
                continue
            ss = max(float(sp.std()), 1e-6)
            g = min(1.5, max(0.6, float(dp.std()) / ss))
            v = (sp - float(sp.mean())) * g + float(dp.mean())
            newc = pt[..., ch]
            newc[sel] = v
            pt[..., ch] = newc
        mf = cv2.GaussianBlur(m_ell.astype(np.float32) / 255.0, (0, 0), 1.6)
        o = plate_art.astype(np.float32) * (1 - mf[..., None]) + pt * mf[..., None]
        out = np.clip(o, 0, 255).astype(np.uint8)
        # 铁律: 膨胀区外(眉毛/脸颊/发丝)必须逐像素等于 plate_art
        out[m_zone == 0] = plate_art[m_zone == 0]
        return out, n_diff

    def blink_curve(p):
        """闭眼弧线: 内外眼角二次贝塞尔, 控制点微下垂."""
        c0, c3 = p[0], p[3]
        cx = (c0[0] + c3[0]) / 2.0
        cy = (c0[1] + c3[1]) / 2.0 + 0.02 * np.linalg.norm(c0 - c3)
        ctrl = np.array([cx, cy])
        ts = np.linspace(0, 1, 48)[:, None]
        return (1 - ts) ** 2 * c0 + 2 * (1 - ts) * ts * ctrl + ts ** 2 * c3

    def synth_blink():
        """合成闭眼板: 椭圆区镜像眼下皮肤纹理(保真实皮肤渐变) + 轻微柔化 + 睫毛弧线.
        双眼由同一算法生成 -> 绝对对称; 眉毛/脸颊逐像素不动."""
        synth = plate_art.astype(np.float32).copy()
        yy, xx = np.mgrid[0:H, 0:W]
        for i, (cx, cy, ew, up, lo) in enumerate(eyes):
            sel = m_eye[i]
            # 源行 = 以 (cy + 0.75ew) 为轴的镜像: 眼睑上的像素取自眼下皮肤
            ys = np.clip((2 * (cy + 0.75 * ew) - yy).astype(np.int32), 0, H - 1)
            xs = np.clip(xx, 0, W - 1)
            patch = plate_art[ys, xs].astype(np.float32)
            patch[~sel] = synth[~sel]
            synth = patch
        synth_u8 = np.clip(synth, 0, 255).astype(np.uint8)
        # 椭圆内柔化(去掉镜像纹理里残留的眼线硬边)
        soft = cv2.GaussianBlur(synth_u8, (0, 0), 1.1)
        blended = plate_art.astype(np.float32).copy()
        mf = cv2.GaussianBlur(m_ell.astype(np.float32) / 255.0, (0, 0), 1.6)
        blended = blended * (1 - mf[..., None]) + soft.astype(np.float32) * mf[..., None]
        out = np.clip(blended, 0, 255).astype(np.uint8)
        # 睫毛弧线
        for p in (le, re_):
            curve = blink_curve(p).astype(np.int32)
            cv2.polylines(out, [curve], False, (52, 45, 48), max(2, int(round(0.055 * np.linalg.norm(p[0] - p[3])))))
        line = np.zeros((H, W), np.float32)
        for p in (le, re_):
            curve = blink_curve(p).astype(np.int32)
            cv2.polylines(line, [curve], False, 1.0, max(2, int(round(0.045 * np.linalg.norm(p[0] - p[3])))))
        line = cv2.GaussianBlur(line, (0, 0), 0.9)
        dark = out.astype(np.float32) * (1 - 0.72 * line[..., None]) + np.array([58, 50, 55], np.float32) * (0.72 * line[..., None])
        out = np.clip(dark, 0, 255).astype(np.uint8)
        out[m_zone == 0] = plate_art[m_zone == 0]      # 铁律: 区外逐像素不动
        return out

    def judge(plate):
        diff = np.abs(plate.astype(np.int32) - plate_art.astype(np.int32)).sum(2) > 6
        nL = int((diff & m_eye[0]).sum())
        nR = int((diff & m_eye[1]).sum())
        sym = min(nL, max(nR, 1)) / max(max(nL, max(nR, 1)), 1)
        return nL, nR, round(sym, 2)

    report = {'VX': VX, 'VM': VM, 'align': {}, 'sym': {}, 'ebox': [ex0, ex1, ey0, ey1]}
    X_src = synth_blink()
    Xinfo = {'source': 'synthetic'}
    mv = readvid(args.src, VX) if os.path.exists(args.src) else None
    if mv is not None:
        wx, ares = align_translation(mv)
        Xp, _ = composite(wx)
        nL, nR, sym = judge(Xp)
        Xinfo = {'source': 'real', 'align': round(ares, 2), 'nL': nL, 'nR': nR, 'sym': sym}
        if ares > ALIGN_MAX_PX or sym < SYM_MIN or min(nL, nR) < MIN_EYE_PX:
            Xinfo['fallback'] = 'align/sym/coverage闸门不过'
            Xp = X_src
            Xinfo['used'] = 'synthetic'
        else:
            Xinfo['used'] = 'real'
        X_final = Xp
    else:
        X_final = X_src
        Xinfo['used'] = 'synthetic(no VX frame)'
    report['X'] = Xinfo
    cv2.imencode('.png', X_final)[1].tofile(os.path.join(plates_dir, 'X.png'))

    # M 板(半闭): 同样过闸门; 不合格直接复用 X 板(render 只依赖 X, M 仅存档)
    M_final = X_final
    Minfo = {'source': 'fallback_to_X'}
    mm = readvid(args.src, VM) if os.path.exists(args.src) else None
    if mm is not None and Xinfo.get('used') == 'real':
        wm, ares = align_translation(mm)
        Mp, _ = composite(wm)
        nL, nR, sym = judge(Mp)
        Minfo = {'source': 'real', 'align': round(ares, 2), 'nL': nL, 'nR': nR, 'sym': sym}
        if ares > ALIGN_MAX_PX or sym < SYM_MIN or min(nL, nR) < MIN_EYE_PX:
            Minfo['used'] = 'fallback'
            Mp = X_final
        else:
            Minfo['used'] = 'real'
        M_final = Mp
    report['M'] = Minfo
    cv2.imencode('.png', M_final)[1].tofile(os.path.join(plates_dir, 'M.png'))

    json.dump(report, open(os.path.join(args.work, 'plates_report.json'), 'w', encoding='utf-8'), ensure_ascii=False)
    print('plates v2:', json.dumps(report, ensure_ascii=False))

    # 自检: 膨胀区外必须逐像素 == plate_art
    for nm, pth in (('X', os.path.join(plates_dir, 'X.png')), ('M', os.path.join(plates_dir, 'M.png'))):
        pv = cv2.imdecode(np.fromfile(pth, np.uint8), cv2.IMREAD_COLOR)
        outside_equal = bool(np.array_equal(pv[m_zone == 0], plate_art[m_zone == 0]))
        print('  %s outside-zone == plate_art: %s' % (nm, outside_equal))
        if not outside_equal:
            sys.exit(1)
    return 0


if __name__ == '__main__':
    sys.exit(main())
