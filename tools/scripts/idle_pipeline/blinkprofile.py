import cv2, numpy as np, os, json, sys
import face_recognition
R = r'C:\Speaktosi'
def imu(p):
    return cv2.imdecode(np.fromfile(p, np.uint8), cv2.IMREAD_COLOR)
def eyes_of(img):
    lms = face_recognition.face_landmarks(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))
    if not lms:
        return None
    out = []
    for k in ('left_eye', 'right_eye'):
        p = np.array(lms[0][k], float)
        out.append((float(p[:, 0].mean()), float(p[:, 1].mean()), float(np.linalg.norm(p[0] - p[3]))))
    return out
def shift(a, b, cx, cy, r=26):
    x0, x1 = int(cx - r), int(cx + r)
    y0, y1 = int(cy - r), int(cy + r)
    ga = cv2.cvtColor(a[y0:y1, x0:x1], cv2.COLOR_BGR2GRAY).astype(np.float32)
    gb = cv2.cvtColor(b[y0:y1, x0:x1], cv2.COLOR_BGR2GRAY).astype(np.float32)
    (dx, dy), resp = cv2.phaseCorrelate(ga, gb)
    return dx, dy, resp
def prof(d, art, eyes, i0=16, i1=27):
    fs = sorted(f for f in os.listdir(d) if f.endswith('.png'))
    ga = cv2.cvtColor(art, cv2.COLOR_BGR2GRAY).astype(np.float32)
    rows = []
    for i in range(i0, min(i1, len(fs))):
        f = imu(os.path.join(d, fs[i]))
        gf = cv2.cvtColor(f, cv2.COLOR_BGR2GRAY).astype(np.float32)
        tot, mv = 0.0, 0.0
        for (cx, cy, ew) in eyes:
            r = 22
            x0, x1, y0, y1 = int(cx - r), int(cx + r), int(cy - r), int(cy + r)
            tot += float(np.abs(gf[y0:y1, x0:x1] - ga[y0:y1, x0:x1]).mean())
            dx, dy, _ = shift(art, f, cx, cy, r - 4)
            mv = max(mv, (dx * dx + dy * dy) ** 0.5)
        rows.append((i, round(tot / len(eyes), 2), round(mv, 2)))
    return rows
if __name__ == '__main__':
    for av in ('coach_arthur', 'coach_eleanor', 'coach_kate', 'coach_kai'):
        ap_ = os.path.join(R, 'engine', 'data', 'avatars', av, 'full_imgs', '00000000.png')
        d = os.path.join(R, 'engine', 'data', 'customvideo', av + '_idle')
        if not (os.path.exists(ap_) and os.path.isdir(d)):
            print(av, 'skip')
            continue
        art = imu(ap_)
        e = eyes_of(art)
        ew = round(sum(x[2] for x in e) / len(e), 1)
        print('%-15s eyew=%-6s ' % (av, ew) + ' '.join('%d:%.1f/%.1f' % r for r in prof(d, art, e)))
