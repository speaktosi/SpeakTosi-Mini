# -*- coding: utf-8 -*-
"""真实眨眼序列 v3 (1010c): Brenda式"逐帧真实闭眼序列"替换单板alpha淡入.

Owen 1010c: 单板X+alpha斜坡 = 眼睑沿羽化椭圆边"斜向淡出"(Kate/Eleanor'眨眼斜度'),
且合成兜底板用镜像眼下皮肤填色 = Kai'一般肤色割裂'. Brenda 的idle之所以自然 =
她的待机本身就是源视频真实眨眼帧序列(129帧实拍渐变).

v3 规则:
  1) 从 eyescan 峰帧 VX 向两侧延展, 取'双眼同时闭合+对称'的最长真实窗口(<=15帧);
  2) 每帧: 环带平移ECC对齐到 plate_art -> 环带增益/偏置色彩锁定(只由椭圆外推出,
     真实眼睑肤色原样保留 —— 1010c 修正: 旧版把椭圆内均值锁到睁眼板,
     深色皮肤眼睑被拉向巩膜均值 = Kai'一般肤色'割裂感的真正来源);
  3) 椭圆外逐像素 == plate_art (眉毛/脸颊物理不动);
  4) 时序3帧中值抑制未锁镜源的逐帧重绘噪声(防重影);
  5) 逐帧对称闸门(>=SYM_MIN), 不过则从违规端裁掉; 窗口<5帧 -> ok=false,
     render 自动回退旧 alpha 斜坡路径.
输出 plates/blinkseq/*.png + plates/blinkseq.json {L, off, ok, ...}
"""
import os, sys, json
import numpy as np, cv2, face_recognition as fr
from common import get_args
from plates import eye_geom, make_ell_mask

MAXLEN = 24
FLOOR = 0.30      # 主闭合段下限 (相对峰值 min(l,r))
EDGE = 0.10       # 两端平滑延展区下限 (进出不pop帧)
SYM_MIN = 0.45
MINLR_GATE = 12.0

def imu(p):
    return cv2.imdecode(np.fromfile(p, np.uint8), cv2.IMREAD_COLOR)

def main():
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    args = get_args()
    work = args.work
    pdir = os.path.join(work, 'plates')
    sj_path = os.path.join(pdir, 'blinkseq.json')
    if os.environ.get('IDLE_NO_SEQ') == '1':
        json.dump({'ok': False, 'why': 'IDLE_NO_SEQ'}, open(sj_path, 'w'))
        print('blinkseq skipped (IDLE_NO_SEQ=1)')
        return

    art = imu(args.art)
    plate = imu(os.path.join(pdir, 'art.png'))
    H, W = plate.shape[:2]
    lms = fr.face_landmarks(cv2.cvtColor(art, cv2.COLOR_BGR2RGB))
    if not lms:
        json.dump({'ok': False, 'why': 'no face'}, open(sj_path, 'w')); print('FAIL no face'); return
    le = np.array(lms[0]['left_eye'], float)
    re_ = np.array(lms[0]['right_eye'], float)

    def box(p, pad=4):
        return (int(p[:, 0].min()) - pad, int(p[:, 0].max()) + pad,
                int(p[:, 1].min()) - pad, int(p[:, 1].max()) + pad)
    LB, RB = box(le), box(re_)
    CT = (int(le[:, 0].min()) - 6, int(re_[:, 0].max()) + 6,
          int(le[:, 1].min()) - 46, int(le[:, 1].min()) - 18)

    # ---- 眼部椭圆 / 环带 (与 plates.py 同几何) ----
    eyes = [eye_geom(le), eye_geom(re_)]
    m_ell = make_ell_mask(H, W, eyes, 1.0)
    # v4.1(1010d) 眉毛锁定: 椭圆竖轴 eyew*2.3 从眼睑起算, 实测上缘盖住眉毛18+px,
    # 旧版注释"眉毛物理不动"与代码不符 -> H3 重绘眉(相对plate漂移2.5~5px)在 mf~1
    # 区顶替 plate 眉 = Owen 报的"眼睛及周围错位". 现在眉多边形(膨胀5)从
    # m_ell/m_zone/m_ell_dil/ring 全部扣除: 眉像素恒==plate, 且不参与ECC配准.
    lb = np.array(lms[0]['left_eyebrow'], float)
    rb = np.array(lms[0]['right_eyebrow'], float)
    m_brow = np.zeros((H, W), np.uint8)
    cv2.fillConvexPoly(m_brow, lb.astype(np.int32), 255)
    cv2.fillConvexPoly(m_brow, rb.astype(np.int32), 255)
    m_brow = cv2.dilate(m_brow, np.ones((5, 5), np.uint8))
    # v4.2(1010d) 鼻梁中缝锁定: 内眼角-鼻梁衔接是错位高发点(vision 报告). 鼻梁窄条
    # (|x-ncx|<=9, 鼻根->鼻尖) 从 m_ell 扣除, 恒 == plate, 不让源帧重绘的鼻梁顶替.
    nose = np.array(lms[0]['nose_bridge'], float)
    ncx = int(nose[:, 0].mean())
    ny_top, ny_bot = int(nose[0, 1]), int(nose[-1, 1])
    m_nose = np.zeros((H, W), np.uint8)
    m_nose[max(0, ny_top - 4):min(H, ny_bot + 6), max(0, ncx - 9):min(W, ncx + 10)] = 255
    m_ell[m_brow > 0] = 0
    m_ell[m_nose > 0] = 0
    m_zone = cv2.dilate(m_ell, np.ones((15, 15), np.uint8))
    m_zone[m_brow > 0] = 0
    m_ell_dil = cv2.dilate(m_ell, np.ones((11, 11), np.uint8))
    m_ell_dil[m_brow > 0] = 0
    eb = np.vstack([le, re_])
    rx0, rx1 = max(0, int(eb[:, 0].min()) - 14 - 40), min(W, int(eb[:, 0].max()) + 14 + 40)
    ry0, ry1 = max(0, int(eb[:, 1].min()) - 12 - 45), min(H, int(eb[:, 1].max()) + 10 + 40)
    ring = np.zeros((H, W), bool)
    ring[ry0:ry1, rx0:rx1] = True
    ring &= (m_ell_dil == 0)
    ring &= (m_brow == 0)
    # ---- v4.2 大锚区(静态皮肤: 眉下缘->鼻尖+20, 排除眼椭圆/眉) + 欧氏ECC ----
    # 旧薄环(3055px)+纯平移解不了 H3 源的轻微头旋转 -> 眼睑落位偏(审计: 眼15.4px/眉4.95px).
    ryA0 = max(0, int(lb[:, 1].max()) + 2)
    ryA1 = min(H, int(nose[-1, 1]) + 22)
    m_anchor = np.zeros((H, W), bool)
    m_anchor[ryA0:ryA1, rx0:rx1] = True
    m_anchor &= (m_ell_dil == 0)
    m_anchor &= (m_brow == 0)
    anch_px = int(m_anchor.sum())
    assert anch_px > 8000, "anchor too small: %d" % anch_px
    ring = m_anchor.copy()
    _ANCHOR_OK = anch_px >= 8000  # 锚区够大才允许欧氏ECC, 否则退回薄环+平移

    # ---- 全帧闭合曲线 (与 eyescan 同度量) ----
    wd = os.path.join(work, 'warped')
    prof = []
    i = 0
    while True:
        p = os.path.join(wd, '%04d.png' % i)
        if not os.path.exists(p):
            break
        w = imu(p)
        def md(bx):
            x0, x1, y0, y1 = bx
            return float(np.abs(w[y0:y1, x0:x1].astype(np.float32)
                                - art[y0:y1, x0:x1].astype(np.float32)).mean())
        prof.append((md(LB), md(RB), md(CT)))
        i += 1
    if len(prof) < 20:
        json.dump({'ok': False, 'why': 'warped frames<20'}, open(sj_path, 'w')); print('FAIL frames'); return
    ctrl = float(np.median([c for _, _, c in prof]))
    minlr = [max(0.0, min(l - ctrl, r - ctrl)) for l, r, _c in prof]

    scan = json.load(open(os.path.join(work, 'eyescan.json')))
    # ---- v4.3(1010d) EAR 真闭眼峰 ----
    # 旧 minlr=|帧-art|差值: art 基准是微笑半闭眼, 睁眼帧整帧差异反而大 -> 假峰落在
    # '与art姿态接近的过渡段'(Kate实测: VX=51假峰18.2, 而全闭帧57-61 diff=0.0).
    # 改用眼竖直厚度(h, 与art无关): 闭眼深度 = OPEN中位 - h. 度量不足时回退旧法.
    # v4.3.1 FIX: 旧 _h 的均值混入眼角点0/3(y居中) -> 全闭时 h 仅降4px(假浅).
    # 换标准 EAR = (|p1-p5|+|p2-p4|)/2/|p0-p3|, 与眼宽归一(源实测: 睁0.6 全闭0.15).
    hs = []
    for i in range(len(prof)):
        w = imu(os.path.join(wd, '%04d.png' % i))
        lms2 = fr.face_landmarks(cv2.cvtColor(w, cv2.COLOR_BGR2RGB))
        if not lms2:
            hs.append(None)
            continue
        def _ear(key, _l=lms2):
            p = np.array(_l[0][key], float)
            v = abs(p[1][1] - p[5][1]) + abs(p[2][1] - p[4][1])
            return float(v / max(abs(p[0][0] - p[3][0]), 1.0))
        hs.append(min(_ear('left_eye'), _ear('right_eye')))
    _hv = [v for v in hs if v is not None]
    use_ear = False
    if len(_hv) >= 0.6 * len(prof):
        OPEN = float(np.median(_hv))
        edep = [max(0.0, OPEN - v) if v is not None else 0.0 for v in hs]
        epeak = float(max(edep))
        VX = int(np.argmax(edep))
        use_ear = OPEN > 1e-6 and epeak >= 0.20 * OPEN
        # 阈值0.20*OPEN(自然眨眼深度≈0.35~0.5*OPEN, '没眨'的源<=0.08)
    if use_ear:
        depth = edep
        dpeak = epeak
        peak = epeak
        print('EAR peak VX=%d OPEN=%.2f depth=%.2f' % (VX, OPEN, epeak))
    else:
        depth = minlr
        VX = scan['VX']
        _lo = max(0, VX - 12); _hi = min(len(minlr), VX + 13)
        VX = int(_lo + int(np.argmax(minlr[_lo:_hi])))
        dpeak = peak = minlr[VX]
    if dpeak < (0.25 * OPEN if use_ear else MINLR_GATE):
        json.dump({'ok': False, 'why': 'peak %.1f too low' % dpeak}, open(sj_path, 'w'))
        print('FAIL peak %.1f' % dpeak); return

    # ---- v3.1(1010c): 单调阶梯采样, 治'源双连眨' ----
    # Kai 源 t8..t26 是 闭(42)->半开凹槽(21)->闭(43)->开 的双连眨. 旧窗口延展按 FLOOR 吞下
    # 凹槽 -> 渲染忠实重放'闭->半开->闭' = 双闪(正是 Owen 报的病).
    # 新规则: 从 VX 向两侧只接受'闭合度单调下降'的帧, 一遇回升(另一次眨)立刻截断;
    # 端点闭合度 > 0.40*peak 时补 1 帧 50% blend plate_art, 保证进出无 pop.
    TOL = (max(0.015, 0.08 * peak) if use_ear else max(2.0, 0.05 * peak))

    def chain(step, floor):
        out = [VX]; last = depth[VX]; i = VX + step
        while 0 <= i < len(depth) and len(out) < 9:
            v = depth[i]
            if v < floor * peak:
                out.append(i); break
            if v > last + TOL:
                break                          # 回升 = 另一次眨眼 -> 截断
            if v < last - (max(0.012, 0.05 * peak) if use_ear else 1.0):
                out.append(i); last = v
            i += step
        return out

    close_idx = chain(-1, FLOOR)[::-1]
    open_idx = chain(1, EDGE)[1:]
    idxs = close_idx + open_idx
    print('staircase %s peak=%.1f' % (idxs, peak))

    # ---- 对齐+色彩锁(环带增益/偏置)+合成 ----
    g_plate = cv2.cvtColor(plate, cv2.COLOR_BGR2GRAY).astype(np.float32)
    aligned = []
    for k, idx in enumerate(idxs):
        f = imu(os.path.join(wd, '%04d.png' % idx))
        g1 = cv2.cvtColor(f, cv2.COLOR_BGR2GRAY).astype(np.float32)
        A = g_plate.copy(); B = g1.copy()
        base = float(g_plate[ring].mean())
        A[~ring] = base; B[~ring] = base
        # v4.2(1010d) 配准策略: 平移解 -> 欧氏解(旋转+平移), 欧氏相关显著更优且
        # rot<=6deg 才采用. H3 眨眼源自带 5~13deg 头倾, 纯平移解不了旋转残差 ->
        # 眼睑/眉相对 plate 落位偏 = "眼睛及周围错位" 的几何根. OpenCV5 返回 (cc, M).
        def _solve(motion, mask8):
            M = np.eye(2, 3, dtype=np.float32)
            try:
                res = cv2.findTransformECC(A, B, M, motion,
                                           (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 400, 1e-7),
                                           mask8, 5)
                if isinstance(res, tuple):
                    return float(res[0]), res[1]
                return None, M
            except Exception:
                return None, np.eye(2, 3, dtype=np.float32)

        mask8 = ring.astype(np.uint8) * 255
        cct, Mt = _solve(cv2.MOTION_TRANSLATION, mask8)
        M0 = Mt
        if _ANCHOR_OK:
            cce, Me = _solve(cv2.MOTION_EUCLIDEAN, mask8)
            rot = abs(float(np.degrees(np.arctan2(float(Me[1, 0]), float(Me[0, 0])))))
            if (cce is not None and cct is not None and rot <= 6.0
                    and cce > cct + 0.005 and abs(Me[0, 2]) <= 150 and abs(Me[1, 2]) <= 150):
                M0 = Me
        if abs(M0[0, 2]) > 150 or abs(M0[1, 2]) > 150:
            M0 = np.eye(2, 3, dtype=np.float32)
        aligned.append(cv2.warpAffine(f, M0, (W, H), flags=cv2.INTER_LANCZOS4,
                                      borderMode=cv2.BORDER_REPLICATE))

    def compose(fr_img):
        pa = plate[ring].astype(np.float32); wa = fr_img[ring].astype(np.float32)
        gains = np.clip(pa.std(0) / np.maximum(wa.std(0), 1e-6), 0.6, 1.6)
        offs = pa.mean(0) - gains * wa.mean(0)
        pt = np.clip(fr_img.astype(np.float32) * gains + offs, 0, 255)
        mf = cv2.GaussianBlur(m_ell.astype(np.float32) / 255.0, (0, 0), 1.6)
        o = plate.astype(np.float32) * (1 - mf[..., None]) + pt * mf[..., None]
        out = np.clip(o, 0, 255).astype(np.uint8)
        out[m_zone == 0] = plate[m_zone == 0]
        return out

    comps = [compose(a_) for a_ in aligned]

    # ---- 逐帧对称闸门 v3.3(1010c): 端点也裁(旧版 0<k<len-1 漏掉端点 ->
    #      Eleanor/Kate 源'单眼先闭'的端帧幸存进成品 = vision 报的不对称pop) ----
    m_eye = []
    for (cx, cy, ew, up, lo) in eyes:
        m1 = np.zeros((H, W), np.uint8)
        for (ex, ey, ax, ay) in (up, lo):
            cv2.ellipse(m1, (ex, ey), (ax, ay), 0, 0, 360, 255, -1)
        m_eye.append(m1 > 0)

    def sym_of(cp):
        diff = np.abs(cp.astype(np.int32) - plate.astype(np.int32)).sum(2) > 6
        nL = int((diff & m_eye[0]).sum()); nR = int((diff & m_eye[1]).sum())
        return min(nL, max(nR, 1)) / max(max(nL, max(nR, 1)), 1), nL, nR

    def op_lvl(cp):
        # v3.3.1 FIX: 旧版 d[:,y0:y1,x0:x1] 把 x 区间套进通道维 -> 空切片 -> 恒 nan,
        # 导致桥接/对称闸门全程失效(这就是'改了没生效'的真凶).
        d = np.abs(cp.astype(np.float32) - art.astype(np.float32)).mean(2)
        x0, x1, y0, y1 = LB
        dl = float(d[y0:y1, x0:x1].mean())
        x0, x1, y0, y1 = RB
        dr = float(d[y0:y1, x0:x1].mean())
        return dl, dr
    b0, b1 = op_lvl(plate)

    def asym_ok(cp):
        dl, dr = op_lvl(cp)
        dl -= b0; dr -= b1
        if max(dl, dr) < 3.0:            # 两眼看都没怎么闭 -> 谈不上不对称
            return True
        r = min(dl, dr) / max(max(dl, dr), 1e-6)
        return r >= 0.60                 # v3.3: 盒差比(旧像素数比0.45太松, 单眼先闭漏网)
    # ---- 对称裁剪 v3.3.1: 端点坏帧照裁(旧版 k==0 落进 else break -> 单眼先闭端帧漏网) ----
    syms = [round(sym_of(c)[0], 2) for c in comps]
    while len(comps) > 5:
        bad = [k for k in range(len(comps)) if (not asym_ok(comps[k])) or syms[k] < SYM_MIN]
        if not bad:
            break
        k = bad[0]
        vi = idxs.index(VX)
        if k < vi:
            idxs = idxs[1:]; comps = comps[1:]; syms = syms[1:]
        elif k > vi:
            idxs = idxs[:-1]; comps = comps[:-1]; syms = syms[:-1]
        else:
            break                        # VX 本身不对称: 交给 blinkzone 判退, 不自裁
    if VX not in idxs:
        json.dump({'ok': False, 'why': 'sym trim lost VX', 'syms': syms}, open(sj_path, 'w'))
        print('FAIL sym trim lost VX'); return

    # ---- v3.3.2 统一平滑: 相邻步长(含两端对全睁) > GATE -> 插桥, 迭代到达标 ----
    # v4.3(1010d): 桥从 plate-alpha 混合改为 **光流双向中间帧**(Farneback 运动插值).
    # 旧 blend=plate与闭帧50%均值 -> 半透明眼皮叠睁眼 = 重影错位(vision 实锤 t14/18/19
    # 跳变+错位). 光流桥是两真实帧的运动学中点, 眼睑连续无重影. 端点桥: 头=睁->首帧,
    # 尾=末帧->plate(睁), 天然单调.
    def lvl(cp):
        dl, dr = op_lvl(cp)
        return max(dl - b0, dr - b1)

    def flow_mid(A, B):
        # 运动学中点: A 前推半个 fAB, B 前推半个 fBA, 两半程帧均值.
        # (旧式 0.5*(fAB-fBA) 在流一致时等于全量位移 -> 退化回 alpha blend, 弃用)
        gA = cv2.cvtColor(A, cv2.COLOR_BGR2GRAY)
        gB = cv2.cvtColor(B, cv2.COLOR_BGR2GRAY)
        fAB = cv2.calcOpticalFlowFarneback(gA, gB, None, 0.5, 5, 15, 3, 5, 1.2, 0)
        fBA = cv2.calcOpticalFlowFarneback(gB, gA, None, 0.5, 5, 15, 3, 5, 1.2, 0)
        gx, gy = np.meshgrid(np.arange(W, dtype=np.float32), np.arange(H, dtype=np.float32))
        mA = cv2.remap(A, gx + 0.5 * fAB[..., 0], gy + 0.5 * fAB[..., 1],
                       cv2.INTER_LANCZOS4, borderMode=cv2.BORDER_REPLICATE)
        mB = cv2.remap(B, gx + 0.5 * fBA[..., 0], gy + 0.5 * fBA[..., 1],
                       cv2.INTER_LANCZOS4, borderMode=cv2.BORDER_REPLICATE)
        out = cv2.addWeighted(mA, 0.5, mB, 0.5, 0)
        out[m_zone == 0] = plate[m_zone == 0]   # 区外铁律: 桥帧区外仍逐像素==plate
        return out

    peak_lvl = max(lvl(c) for c in comps)
    GATE = max(3.0, 0.18 * peak_lvl)
    MAXIT = 32
    for _ in range(MAXIT):
        if len(comps) >= MAXLEN:
            break
        lv = [0.0] + [lvl(c) for c in comps] + [0.0]
        st = [abs(lv[i + 1] - lv[i]) for i in range(len(lv) - 1)]
        bad = [i for i, s in enumerate(st) if s > GATE]
        if not bad:
            break
        i = bad[0]
        if i == 0:                       # 全睁 -> 首帧
            comps.insert(0, flow_mid(plate, comps[0]))
            idxs = [idxs[0]] + idxs
        elif i == len(st) - 1:           # 末帧 -> 全睁
            comps.append(flow_mid(comps[-1], plate))
            idxs = idxs + [idxs[-1]]
        else:                            # 内部大跳: 光流运动中点
            a = comps[i - 1]; b = comps[i]
            comps.insert(i, flow_mid(a, b))
            idxs = idxs[:i] + [idxs[i - 1]] + idxs[i:]

    # v3.3.3 哨兵末帧=plate: render 固定丢弃 seq[-1](视为全睁重复). 桥接后末帧其实是
    # 半闭 blend -> 丢弃后 末用帧->全睁 裸露跳变(vision 判尾部pop). 显式压真 plate 在末尾,
    # 丢弃永远落在"重复的全睁"上, 无缝. idxs 同步+1 保 L 一致.
    comps.append(plate.copy()); idxs = idxs + [idxs[-1]]

    if len(idxs) < 6:
        json.dump({'ok': False, 'why': 'window<5', 'syms': syms}, open(sj_path, 'w'))
        print('FAIL window too short'); return

    sd = os.path.join(pdir, 'blinkseq')
    os.makedirs(sd, exist_ok=True)
    # 清旧残骸(防 render/诊断 glob 读到上一轮更长的序列)
    import glob as _g
    for f in _g.glob(os.path.join(sd, '*.png')):
        try: os.remove(f)
        except OSError: pass
    for k, cp in enumerate(comps):
        cv2.imencode('.png', cp)[1].tofile(os.path.join(sd, '%04d.png' % k))

    # 铁律自检: 区外逐像素==plate
    for k, cp in enumerate(comps):
        if not np.array_equal(cp[m_zone == 0], plate[m_zone == 0]):
            json.dump({'ok': False, 'why': 'zone viol @%d' % k}, open(sj_path, 'w'))
            print('FAIL zone viol'); return

    rep = {'ok': True, 'L': len(idxs), 'off': int(idxs.index(VX)), 'win': [int(idxs[0]), int(idxs[-1])],
           'VX': int(VX), 'src': [int(v) for v in idxs], 'syms': syms, 'peak': round(peak, 1)}
    json.dump(rep, open(sj_path, 'w', encoding='utf-8'), ensure_ascii=False)
    print('BLINKSEQ OK L=%d off=%d win=%d..%d syms=%s' % (len(idxs), idxs.index(VX), idxs[0], idxs[-1], syms))

if __name__ == '__main__':
    main()
