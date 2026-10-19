import os, sys, json
import numpy as np, cv2, face_recognition as fr
from common import get_args

def main():
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    args = get_args()
    
    art = cv2.imdecode(np.fromfile(args.art, np.uint8), cv2.IMREAD_COLOR)
    H, W = art.shape[:2]
    
    bb_list = fr.face_landmarks(cv2.cvtColor(art, cv2.COLOR_BGR2RGB))
    if not bb_list:
        print("Error: Could not detect face in art image.")
        sys.exit(1)
    bb = bb_list[0]
    le = np.array(bb['left_eye'], float)
    re_ = np.array(bb['right_eye'], float)
    
    def box(pts, pad=4):
        return (int(pts[:,0].min())-pad, int(pts[:,0].max())+pad, int(pts[:,1].min())-pad, int(pts[:,1].max())+pad)
    
    BL = box(le)
    BR = box(re_)
    # Control box (forehead)
    CT = (int(le[:,0].min())-6, int(re_[:,0].max())+6, int(le[:,1].min())-46, int(le[:,1].min())-18)
    
    print('Lbox', BL, 'Rbox', BR, 'ctrl(forehead)', CT)
    
    def mdiff(a, b, bx):
        x0, x1, y0, y1 = bx
        dd = np.abs(a[y0:y1, x0:x1].astype(np.float32) - b[y0:y1, x0:x1].astype(np.float32)).mean()
        return float(dd)
    
    warped_dir = os.path.join(args.work, 'warped')
    
    rows = []
    i = 0
    while True:
        p = os.path.join(warped_dir, f"{i:04d}.png")
        if not os.path.exists(p): break
        w = cv2.imdecode(np.fromfile(p, np.uint8), cv2.IMREAD_COLOR)
        rows.append((i, mdiff(w, art, BL), mdiff(w, art, BR), mdiff(w, art, CT)))
        i += 1
        
    if not rows:
        print("Error: No warped frames found.")
        sys.exit(1)
        
    ctrl = float(np.median([c for _, _, _, c in rows]))
    print(f'ctrl median {ctrl:.2f}')
    
    clusters = []
    current_cluster = []
    
    print(' idx   Lrel   Rrel')
    for i, l, r, c in rows:
        l_rel = l - ctrl
        r_rel = r - ctrl
        flag = ''
        if min(l_rel, r_rel) > 12: flag = 'BOTH'
        elif l_rel > 12: flag = 'L-only'
        elif r_rel > 12: flag = 'R-only'
        
        if flag == 'BOTH':
            current_cluster.append(i)
        else:
            if current_cluster:
                clusters.append(current_cluster)
                current_cluster = []
        
        print(f'{i:4d} {l_rel:6.1f} {r_rel:6.1f}  {flag}')
        
    if current_cluster:
        clusters.append(current_cluster)
        
    print(f"Found {len(clusters)} blink clusters.")
    if len(clusters) < 1:
        print("Error: eyescan 检测不到眨眼簇（少于 1 次），源视频不合格！")
        sys.exit(1)
        
    # Find the best cluster (highest peak) —— v2(1010b): 对称优先.
    # 旧版取 (l+r) 总和 -> 单眼闭+另眼重绘 也能得高分, 板出现'单眼游走'.
    # 新版取 min(l, r) 最大: 必须双眼都真的闭到位.
    best_peak_val = -1
    best_vx = -1
    for cl in clusters:
        for idx in cl:
            _, l, r, _c = rows[idx]
            lr = max(l - ctrl, 0.0)
            rr = max(r - ctrl, 0.0)
            val = min(lr, rr)                       # 双眼对称闭合深度
            if val > best_peak_val:
                best_peak_val = val
                best_vx = idx
                
    # Find VM (half-closed frame). Look around VX for a frame whose value is closest to half of peak.
    target_val = best_peak_val / 2.0
    best_vm = -1
    best_vm_diff = 999999
    
    # search +/- 10 frames around VX
    for offset in range(-10, 11):
        if offset == 0: continue
        idx = best_vx + offset
        if 0 <= idx < len(rows):
            _, l, r, _ = rows[idx]
            val = (l - ctrl) + (r - ctrl)
            if val > 12: # At least some diff to be a half-blink, not open eyes
                diff = abs(val - target_val)
                if diff < best_vm_diff:
                    best_vm_diff = diff
                    best_vm = idx
                    
    if best_vm == -1:
        # Fallback if no frame is nicely half-closed
        print("Warning: Could not find perfect M frame, using VX-1 or VX+1")
        best_vm = best_vx - 1 if best_vx > 0 else best_vx + 1
        
    print(f"Selected VX (X frame) = {best_vx}, VM (M frame) = {best_vm}")
    
    # Save to JSON
    out_data = {
        'VX': best_vx,
        'VM': best_vm,
        'clusters': clusters,
        'ctrl_median': ctrl
    }
    json.dump(out_data, open(os.path.join(args.work, 'eyescan.json'), 'w'), indent=1)

if __name__ == '__main__':
    main()
