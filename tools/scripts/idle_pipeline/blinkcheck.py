import os, sys, json
import numpy as np, cv2, face_recognition as fr
from common import get_args

def main():
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    args = get_args()
    
    art = cv2.imdecode(np.fromfile(args.art, np.uint8), cv2.IMREAD_COLOR)
    bb_list = fr.face_landmarks(cv2.cvtColor(art, cv2.COLOR_BGR2RGB))
    if not bb_list:
        print("Error: blinkcheck could not detect face.")
        sys.exit(1)
    bb = bb_list[0]
    def box(pts, pad=4): return (int(pts[:,0].min())-pad, int(pts[:,0].max())+pad, int(pts[:,1].min())-pad, int(pts[:,1].max())+pad)
    LB = box(np.array(bb['left_eye'], float))
    RB = box(np.array(bb['right_eye'], float))
    
    def im(t): return cv2.imdecode(np.fromfile(os.path.join(args.out, f'{t:04d}.png'), np.uint8), cv2.IMREAD_COLOR)
    
    def bd(a, b, bb):
        x0, x1, y0, y1 = bb
        return float(np.abs(a[y0:y1, x0:x1].astype(np.float32)-b[y0:y1, x0:x1].astype(np.float32)).mean())
        
    render_rep = json.load(open(os.path.join(args.work, 'render_report.json')))
    BLINK = render_rep['blink']
    
    need = set([0, args.frames-1])
    for b in BLINK:
        need.update([b-6, b-10, b+1, b+2, b+3, b+5])
    f = {t: im(t) for t in need}
    
    report = {'blinks': {}}
    for b in BLINK:
        base = b-6; pre = b-10
        M1 = bd(f[b+1], f[base], LB)
        X = bd(f[b+2], f[base], LB)
        M2 = bd(f[b+3], f[base], LB)
        after = bd(f[b+5], f[base], LB)
        ctrl = bd(f[base], f[pre], LB)
        report['blinks'][b] = {'ctrl': ctrl, 'M1': M1, 'X': X, 'M2': M2, 'after': after}
        print(f"blink@{b} ctrl={ctrl:.1f} | M(t+1)={M1:.1f} X={X:.1f} M2={M2:.1f} | after={after:.1f}")
        
    seam_L = bd(im(args.frames-1), im(0), LB)
    seam_R = bd(im(args.frames-1), im(0), RB)
    report['loop_seam'] = {'L': seam_L, 'R': seam_R}
    print(f"loop seam |f{args.frames-1}-f0| L={seam_L:.1f} R={seam_R:.1f}")
    
    json.dump(report, open(os.path.join(args.work, 'blinkcheck_report.json'), 'w'), indent=1)

if __name__ == '__main__':
    main()
