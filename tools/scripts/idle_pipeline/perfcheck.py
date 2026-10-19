import os, sys, json
import numpy as np, cv2, face_recognition as fr
from common import get_args

def main():
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    args = get_args()
    
    art = cv2.imdecode(np.fromfile(args.art, np.uint8), cv2.IMREAD_COLOR)
    bb_list = fr.face_landmarks(cv2.cvtColor(art, cv2.COLOR_BGR2RGB))
    bb = bb_list[0]
    def box(pts, pad=4): return (int(pts[:,0].min())-pad, int(pts[:,0].max())+pad, int(pts[:,1].min())-pad, int(pts[:,1].max())+pad)
    LB = box(np.array(bb['left_eye'], float))
    RB = box(np.array(bb['right_eye'], float))
    
    def im(p): return cv2.imdecode(np.fromfile(p, np.uint8), cv2.IMREAD_COLOR)
    plate_art = im(os.path.join(args.work, 'plates', 'art.png'))
    
    def bd(a, b, bb):
        x0, x1, y0, y1 = bb
        return float(np.abs(a[y0:y1, x0:x1].astype(np.float32)-b[y0:y1, x0:x1].astype(np.float32)).mean())
        
    rows = []
    for t in range(args.frames):
        f = im(os.path.join(args.out, f'{t:04d}.png'))
        l = bd(f, plate_art, LB); r = bd(f, plate_art, RB)
        if l > 9 or r > 9: rows.append((t, round(l, 1), round(r, 1)))
        
    print('blink frames (L>9 or R>9):')
    for t, l, r in rows:
        print(f"  t={t:3d} L={l:5.1f} R={r:5.1f} {'BOTH' if (l>9 and r>9) else 'ONE'}")
        
    n0 = im(os.path.join(args.out, '0000.png'))
    n47 = im(os.path.join(args.out, '0047.png'))
    gaze_L = bd(n0, n47, LB)
    gaze_R = bd(n0, n47, RB)
    print(f"gaze eye-box diff t0 vs t47: L={gaze_L:.1f} R={gaze_R:.1f}")
    
    def crop(im0, bb, sc):
        x0, x1, y0, y1 = bb
        c = im0[y0:y1, x0:x1]
        return cv2.resize(c, None, fx=sc, fy=sc, interpolation=cv2.INTER_NEAREST)
        
    def lab(i, t):
        c = i.copy()
        cv2.putText(c, t, (5, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 255), 2)
        return c
        
    pan = [
        lab(crop(n0, LB, 3), 'GAZE t0'),
        lab(crop(n47, LB, 3), 'GAZE t47'),
        lab(crop(im(os.path.join(args.out, '0021.png')), LB, 3), 'BLINK M t21'),
        lab(crop(im(os.path.join(args.out, '0022.png')), LB, 3), 'BLINK X t22'),
        lab(crop(im(os.path.join(args.out, '0023.png')), LB, 3), 'BLINK M t23')
    ]
    W = sum(p.shape[1] for p in pan)
    H = max(p.shape[0] for p in pan)
    out = np.zeros((H, W, 3), np.uint8)
    x = 0
    for p in pan:
        out[:p.shape[0], x:x+p.shape[1]] = p
        x += p.shape[1]
        
    cv2.imwrite(os.path.join(args.work, 'perf_strip.png'), out)
    print('WROTE perf strip', out.shape)
    
    json.dump({'blink_frames': rows, 'gaze_diff': {'L': gaze_L, 'R': gaze_R}}, 
              open(os.path.join(args.work, 'perfcheck_report.json'), 'w'), indent=1)

if __name__ == '__main__':
    main()
