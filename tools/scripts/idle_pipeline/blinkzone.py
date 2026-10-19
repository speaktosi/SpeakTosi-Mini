# -*- coding: utf-8 -*-
"""闸门5 (1010b): 眨眼区域完整性 —— 治'正片区域在动/重影'.

对成品帧逐眨眼窗口验证三件事:
  A. 眉颊稳定带(眼框上方一条, 椭圆外)相邻帧位移 <= SHIFT_TH px
     (phaseCorrelate) -> 眨眼时眼窝外不许动;
  B. 闭合单调: 眼框内相对'安静帧'的差异量沿眨眼窗口 升->峰->降, 不许双闪振荡;
  C. 双眼对称: 峰值帧左右眼差异量之比在 [SYM_LO, SYM_HI].
任一 FAIL 即退出码 1, idle_build 会中断, 不许安装不合格品.
"""
import os, sys, json
import numpy as np, cv2, face_recognition as fr
from common import get_args

SHIFT_TH = 0.55     # px/相邻帧, 眉颊带
SYM_LO, SYM_HI = 0.55, 1.8


def imu(p):
    return cv2.imdecode(np.fromfile(p, np.uint8), cv2.IMREAD_COLOR)


def pshift(a, b, box):
    x0, x1, y0, y1 = box
    ga = cv2.cvtColor(b[y0:y1, x0:x1], cv2.COLOR_BGR2GRAY).astype(np.float32)
    gb = cv2.cvtColor(a[y0:y1, x0:x1], cv2.COLOR_BGR2GRAY).astype(np.float32)
    h, w = ga.shape
    ph = int(2 ** np.ceil(np.log2(h * 2))); pw = int(2 ** np.ceil(np.log2(w * 2)))
    A = np.zeros((ph, pw), np.float32); B = np.zeros((ph, pw), np.float32)
    A[:h, :w] = ga; B[:h, :w] = gb
    wy = np.hanning(ph)[:, None]; wx = np.hanning(pw)[None, :]
    (dx, dy), resp = cv2.phaseCorrelate(A * wy * wx, B * wy * wx)
    return float(np.hypot(dx, dy))


def main():
    args = get_args()
    art = imu(args.art)
    H, W = art.shape[:2]
    lms = fr.face_landmarks(cv2.cvtColor(art, cv2.COLOR_BGR2RGB))
    if not lms:
        print('FAIL: no face in art'); return 1
    le = np.array(lms[0]['left_eye'], float)
    re_ = np.array(lms[0]['right_eye'], float)
    eb = np.vstack([le, re_])
    ex0, ex1 = int(eb[:, 0].min()) - 4, int(eb[:, 0].max()) + 4
    ey0, ey1 = int(eb[:, 1].min()) - 2, int(eb[:, 1].max()) + 2
    brow_box = (ex0, ex1, max(0, ey0 - 22), max(1, ey0 - 4))          # 眉毛带
    eyeL_box = (int(le[:, 0].min()) - 4, int(le[:, 0].max()) + 4, int(le[:, 1].min()) - 4, int(le[:, 1].max()) + 4)
    eyeR_box = (int(re_[:, 0].min()) - 4, int(re_[:, 0].max()) + 4, int(re_[:, 1].min()) - 4, int(re_[:, 1].max()) + 4)

    render = json.load(open(os.path.join(args.work, 'render_report.json')))
    BLINK = render['blink']
    F = lambda t: imu(os.path.join(args.out, '%04d.png' % t))
    # v3(1010c): 真实眨眼序列模式 -> 窗口=[b-soff, b+L-soff], 且头部晃动加大后
    # 眉带位移底噪上升: 阈值改 max(SHIFT_TH, 2.2*全片中位位移), 仍拦眨眼引起的异常跳动.
    SEQ = bool(render.get('seq'))
    Ls = int(render.get('seq_L', 0)); SOFF = max(0, int(render.get('seq_off', 0)))

    fails = []
    report = {'blinks': {}, 'brow_shift': {}, 'brow_max': 0.0, 'seq': SEQ}
    # 全片眉颊相邻帧位移(找眨眼窗口的异常尖峰)
    prev = F(0)
    worst = 0.0
    shifts = {}
    for t in range(1, args.frames):
        cur = F(t)
        s = pshift(prev, cur, brow_box)
        shifts[t] = round(s, 3)
        worst = max(worst, s)
        prev = cur
    report['brow_max'] = round(worst, 3)
    import numpy as _np
    seq_closing = None
    if SEQ:
        # v3.1(1010c): 真实序列模式 -> 闭合单调/对称 直接在 blinkseq 帧上验(与 plate 对比),
        # 不依赖成品 quiet 帧: Kai 源 38-75 有头部漂移平台期, 成品 quiet 帧被污染 -> 误报 osc.
        # seq 的眉颊像素由铁律==plate(逐像素), 所以 seq 的'闭->半开->闭'只能在 seq 里, 查 seq 才是查根.
        sd_ = os.path.join(args.work, 'plates', 'blinkseq')
        sj_ = json.load(open(os.path.join(sd_, '..', 'blinkseq.json')))
        comps = [imu(os.path.join(sd_, '%04d.png' % k)) for k in range(sj_['L'])]
        def dref(boxa, f, ref):
            x0, x1, y0, y1 = boxa
            return float(np.abs(f[y0:y1, x0:x1].astype(np.float32) - ref[y0:y1, x0:x1].astype(np.float32)).mean())
        seq_prof = [dref(eyeL_box, c, art) + dref(eyeR_box, c, art) for c in comps]
        seq_sym = [dref(eyeL_box, comps[sj_['off']], art) / max(dref(eyeR_box, comps[sj_['off']], art), 1e-6)]
        pk = int(_np.argmax(seq_prof))
        thr = 0.15 * max(seq_prof)
        plateau = (max(seq_prof) - seq_prof[sj_['off']]) <= 0.05 * max(seq_prof)
        left = seq_prof[:pk]; right = seq_prof[pk:]
        dips = sum(1 for i in range(1, len(left)) if left[i - 1] - left[i] > thr)
        rises = sum(1 for i in range(1, len(right)) if right[i] - right[i - 1] > thr)
        seq_fails = []
        if (dips + rises) >= 2:
            seq_fails.append('blinkseq 闭合非单调(双闪): %s' % [round(v, 1) for v in seq_prof])
        if abs(pk - sj_['off']) > 2 and not plateau:
            # v3.1: off 帧与闭合峰差<=5%峰高 = 全闭平台(源噪声抖动), 不误杀
            seq_fails.append('blinkseq 峰值帧 off=%d 与闭合峰 %d 不符' % (sj_['off'], pk))
        if not (SYM_LO <= seq_sym[0] <= SYM_HI):
            seq_fails.append('blinkseq 峰值双眼不对称 ratio=%.2f' % seq_sym[0])
        report['seq_prof'] = [round(v, 2) for v in seq_prof]
        if seq_fails:
            fails.extend(seq_fails)
    med_shift = float(_np.median(list(shifts.values()))) if shifts else 0.0
    brow_th = max(SHIFT_TH, 2.2 * med_shift)
    report['brow_th'] = round(brow_th, 3)
    report['brow_med'] = round(med_shift, 3)
    print('brow: med=%.3fpx th=%.3fpx (warp=%.2f seq=%s)' % (med_shift, brow_th,
            json.load(open(os.path.join(args.work, 'render_report.json'))).get('warp', 1.0), SEQ))

    for b in BLINK:
        quiet = b - 8
        fq = F(quiet)

        def dbox(box, f):
            x0, x1, y0, y1 = box
            return float(np.abs(f[y0:y1, x0:x1].astype(np.float32) - fq[y0:y1, x0:x1].astype(np.float32)).mean())

        if SEQ:
            # 真实序列模式: 闭合单调/对称已由 seq_prof 直查(上面), 成品侧只查眉带尖峰(防装配错版).
            entry = {'brow_shift_win': round(max(shifts.get(t, 0) for t in range(b - 8, b + 10)), 3),
                     'peak_offset': SOFF - 8 + 0, 'osc': False, 'sym_ratio': 1.0, 'via': 'seq'}
        else:
            prof = [dbox(eyeL_box, F(t)) + dbox(eyeR_box, F(t)) for t in range(b - 5, b + 7)]
            pk = int(np.argmax(prof))
            thr = 0.15 * max(prof)
            left = prof[:pk]; right = prof[pk:]
            dips = sum(1 for i in range(1, len(left)) if left[i - 1] - left[i] > thr)
            rises = sum(1 for i in range(1, len(right)) if right[i] - right[i - 1] > thr)
            osc = (dips + rises) >= 2
            dl = dbox(eyeL_box, F(b)); dr = dbox(eyeR_box, F(b))
            ratio = dl / max(dr, 1e-6)
            brow_win = max(shifts.get(t, 0) for t in range(b - 3, b + 5))
            entry = {'brow_shift_win': round(brow_win, 3), 'peak_offset': pk - 5,
                     'osc': bool(osc), 'sym_ratio': round(ratio, 2)}
            if not (SYM_LO <= ratio <= SYM_HI):
                fails.append('blink@%d 双眼不对称 ratio=%.2f' % (b, ratio))
            if osc:
                fails.append('blink@%d 闭合非单调(双闪振荡)' % b)
        report['blinks'][str(b)] = entry
        if entry['brow_shift_win'] > brow_th:
            fails.append('blink@%d 眉颊带位移 %.2fpx > %.2f (正片区域在动)' % (b, entry['brow_shift_win'], brow_th))

    json.dump(report, open(os.path.join(args.work, 'blinkzone_report.json'), 'w'), indent=1)
    if fails:
        print('BLINKZONE FAIL:')
        for x in fails:
            print('  -', x)
        return 1
    print('BLINKZONE OK  brow_max=%.2fpx  blinks=%s' % (worst, json.dumps(report['blinks'])))
    return 0


if __name__ == '__main__':
    sys.exit(main())
