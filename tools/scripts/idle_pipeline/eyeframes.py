import os, sys
import numpy as np, cv2
from common import get_args

def main():
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    args = get_args()
    
    art = cv2.imdecode(np.fromfile(args.art, np.uint8), cv2.IMREAD_COLOR)
    if art is None:
        print(f"Error: Could not load art image from {args.art}")
        sys.exit(1)
        
    H, W = art.shape[:2]
    EG = cv2.cvtColor(art, cv2.COLOR_BGR2GRAY).astype(np.float32)
    
    cap = cv2.VideoCapture(args.src)
    if not cap.isOpened():
        print(f"Error: Could not open video {args.src}")
        sys.exit(1)
        
    warped_dir = os.path.join(args.work, 'warped')
    os.makedirs(warped_dir, exist_ok=True)
    
    frm = []
    i = 0
    print(f"Aligning frames from {args.src} to {args.art} ...")
    while True:
        ok, f = cap.read()
        if not ok: break
        g = cv2.cvtColor(f, cv2.COLOR_BGR2GRAY).astype(np.float32)
        M = np.eye(2, 3, dtype=np.float32)
        try:
            cv2.findTransformECC(EG, g, M, cv2.MOTION_AFFINE, (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 250, 1e-6), None, 3)
            if abs(M[0,2]) > 150 or abs(M[1,2]) > 150: M = np.eye(2, 3, dtype=np.float32)
        except Exception: 
            M = np.eye(2, 3, dtype=np.float32)
        
        w = cv2.warpAffine(f, M, (W, H), flags=cv2.INTER_LANCZOS4, borderMode=cv2.BORDER_REPLICATE)
        out_path = os.path.join(warped_dir, f"{i:04d}.png")
        cv2.imencode('.png', w)[1].tofile(out_path)
        frm.append(w)
        i += 1
    
    cap.release()
    print(f"Aligned {len(frm)} frames.")
    
    # Check if we have at least 20 frames
    if len(frm) < 20:
        print("Error: Video is too short.")
        sys.exit(1)
        
    def lab(img, txt, sc):
        r = cv2.resize(img, None, fx=sc, fy=sc, interpolation=cv2.INTER_NEAREST)
        cv2.putText(r, txt, (4, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.62, (0, 0, 255), 2)
        return r

    # Use a fixed center crop just for visualization (since we don't have landmarks in this script yet)
    cy, cx = int(H*0.3), int(W*0.5)
    crop_art = art[cy-25:cy+25, cx-70:cx+70] if cy-25>0 else art[:50, :140]
    rows = [lab(crop_art, 'ART', 4)]
    
    for k in range(min(11, len(frm)-1), min(21, len(frm))):
        crop_f = frm[k][cy-25:cy+25, cx-70:cx+70] if cy-25>0 else frm[k][:50, :140]
        rows.append(lab(crop_f, f'f{k}', 4))
        
    W0 = max(r.shape[1] for r in rows)
    Hs = sum(r.shape[0] for r in rows)
    out = np.zeros((Hs, W0, 3), np.uint8)
    y = 0
    for r in rows:
        out[y:y+r.shape[0], :r.shape[1]] = r
        y += r.shape[0]
        
    diag_path = os.path.join(args.work, 'diag_eyeframes.png')
    cv2.imencode('.png', out)[1].tofile(diag_path)
    print(f"WROTE diag strip to {diag_path}")

if __name__ == '__main__':
    main()
