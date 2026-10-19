# -*- coding: utf-8 -*-
"""构图锁定归一: 把 i2v 逐帧用面部三点(双眼中心+下巴)仿射映射回基准原画的几何.

解决 H3/MiniMax 等 i2v 模型的"缓慢推镜"偏移, 使 eye-scan/眨眼板提取能在
与原画同一构图下进行(模型无关, 确定性).

用法: python normalize_frames.py --art <png> --in <frames_dir> --out <dir> [--json <log>]
"""
import argparse, glob, json, os, sys, pathlib
import cv2, numpy as np
import face_recognition


def imu(p):
    im = cv2.imdecode(np.fromfile(p, np.uint8), cv2.IMREAD_UNCHANGED)
    if im.ndim == 3 and im.shape[2] == 4:
        im = im[:, :, :3]
    return im


def anchors(img):
    """返回 (左眼中心, 右眼中心, 下巴) 三个锚点; 无脸返回 None"""
    lms = face_recognition.face_landmarks(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))
    if not lms:
        return None
    f = lms[0]
    le = np.mean(np.array(f['left_eye'], float), axis=0)
    re = np.mean(np.array(f['right_eye'], float), axis=0)
    ch = np.array(f['chin'], float)[8] if len(f['chin']) > 8 else np.mean(np.array(f['chin'], float), axis=0)
    return np.float32([le, re, ch])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--art', required=True)
    ap.add_argument('--in', dest='ind', required=True)
    ap.add_argument('--out', required=True)
    ap.add_argument('--json')
    a = ap.parse_args()
    art = imu(a.art)
    H, W = art.shape[:2]
    da = anchors(art)
    if da is None:
        raise SystemExit('基准原画未检到人脸: %s' % a.art)
    outd = pathlib.Path(a.out); outd.mkdir(parents=True, exist_ok=True)
    fs = sorted(glob.glob(os.path.join(a.ind, '*.png')))
    log = {'art': a.art, 'n_in': len(fs), 'ok': 0, 'noface': [], 'scales': []}
    for f in fs:
        img = imu(f)
        sa = anchors(img)
        if sa is None:
            log['noface'].append(os.path.basename(f)); continue
        M = cv2.getAffineTransform(sa, da)
        sc = float(np.sqrt(abs(M[0, 0] * M[1, 1] - M[0, 1] * M[1, 0])))
        w = cv2.warpAffine(img, M, (W, H), flags=cv2.INTER_AREA, borderMode=cv2.BORDER_REPLICATE)
        cv2.imencode('.png', w)[1].tofile(str(outd / os.path.basename(f)))
        log['scales'].append(round(sc, 4)); log['ok'] += 1
    if log['scales']:
        s = np.array(log['scales'])
        log['scale_min'] = float(s.min()); log['scale_max'] = float(s.max())
        log['scale_median'] = float(np.median(s))
    print("归一: ok=%s noface=%d scale %.3f~%.3f 中位 %.3f" % (
        log['ok'], len(log['noface']), log.get('scale_min', 0), log.get('scale_max', 0), log.get('scale_median', 0)))
    if a.json:
        json.dump(log, open(a.json, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    return 0


if __name__ == '__main__':
    sys.exit(main())
