import os, sys, json, subprocess
import numpy as np, cv2
from common import get_args

def main():
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    args = get_args()
    
    # We apply motion test on the art image to provide variants
    img = cv2.imdecode(np.fromfile(args.art, np.uint8), cv2.IMREAD_COLOR)
    H, W = img.shape[:2]
    T = args.frames
    FPS = args.fps
    
    PIVOT_H = (W/2.0, 500.0)
    PIVOT_B = (W/2.0, H*0.98)
    Y_H0, Y_H1 = 470.0, 640.0
    Y_B0, Y_B1 = 480.0, 700.0
    
    VARIANTS = {
     'v1_subtle': dict(rot=0.35, x=1.5, y=0.9, br=0.0030),
     'v2_clear':  dict(rot=0.70, x=3.0, y=1.8, br=0.0055),
     'v3_strong': dict(rot=1.20, x=5.0, y=3.0, br=0.0090),
    }
    
    def sstep(a):
        a = np.clip(a, 0, 1)
        return a*a*(3-2*a)
        
    yy = np.arange(H, dtype=np.float32)[:, None]
    wx = (1-sstep((yy-Y_H0)/(Y_H1-Y_H0)))
    wb = sstep((yy-Y_B0)/(Y_B1-Y_B0))
    
    def field(t, P):
        ph = 2*np.pi*t/T
        th = np.radians(P['rot']*(0.62*np.sin(ph)+0.38*np.sin(2*ph+1.1)))
        dx = P['x']*(0.70*np.sin(2*ph+0.6)+0.30*np.sin(ph+2.0))
        dy = P['y']*(0.80*np.sin(ph+0.3)+0.20*np.sin(2*ph+1.7))
        s = 1.0+P['br']*np.sin(ph+0.9)
        xx = np.arange(W, dtype=np.float32)[None, :]
        X = np.broadcast_to(xx, (H, W)).copy()
        Y = np.broadcast_to(yy, (H, W)).copy()
        c = np.array(PIVOT_H)
        rx = X-c[0]; ry = Y-c[1]
        ct, st = np.cos(th), np.sin(th)
        mhx = (ct*rx-st*ry)-rx+dx
        mhy = (st*rx+ct*ry)-ry+dy
        b = np.array(PIVOT_B)
        bx = X-b[0]; by = Y-b[1]
        mbx = (s-1)*bx; mby = (s-1)*by
        mx = X+wx*mhx+wb*mbx+wb*(dy*0.4)
        my = Y+wx*mhy+wb*mby
        return mx.astype(np.float32), my.astype(np.float32)
        
    ff = 'ffmpeg'
    def enc(frames_dir, mp4):
        subprocess.run([ff, '-y', '-loglevel', 'error', '-framerate', str(FPS), '-i', os.path.join(frames_dir, '%04d.png'),
                        '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-crf', '12', mp4], check=False)
                        
    report = {}
    motion_dir = os.path.join(args.work, 'motion')
    os.makedirs(motion_dir, exist_ok=True)
    
    for name, P in VARIANTS.items():
        d = os.path.join(motion_dir, name)
        os.makedirs(d, exist_ok=True)
        disp = []; amad = []; prev = None
        for t in range(T):
            mx, my = field(t, P)
            out = cv2.remap(img, mx, my, cv2.INTER_LANCZOS4, borderMode=cv2.BORDER_REPLICATE)
            cv2.imencode('.png', out)[1].tofile(os.path.join(d, f'{t:04d}.png'))
            disp.append(float(np.hypot(mx-np.arange(W)[None,:], my-yy).max()))
            if prev is not None:
                amad.append(float(np.abs(out.astype(np.float32)-prev.astype(np.float32)).mean()))
            prev = out
        mp4 = os.path.join(motion_dir, name+'.mp4')
        enc(d, mp4)
        report[name] = dict(max_disp_px=round(max(disp), 2), adj_mad_mean=round(float(np.mean(amad)), 3), mp4=mp4)
        print(name, 'max_disp %.2fpx adj_mad %.3f' % (max(disp), np.mean(amad)), flush=True)
        
    idx = []
    for t in range(T):
        mx, my = field(t, VARIANTS['v1_subtle'])
        idx.append((float(mx[300,:].mean()), t))
    xs = [a for a, _ in idx]
    left = idx[int(np.argmin(xs))][1]
    right = idx[int(np.argmax(xs))][1]
    
    pan = []
    for t, lab in [(0, 'neutral'), (left, 'left-extreme'), (right, 'right-extreme')]:
        mx, my = field(t, VARIANTS['v1_subtle'])
        o = cv2.remap(img, mx, my, cv2.INTER_LANCZOS4, borderMode=cv2.BORDER_REPLICATE)
        pan.append(o)
        
    mx, my = field(right, VARIANTS['v1_subtle'])
    d8 = np.clip(np.abs(mx-np.arange(W)[None,:])*8, 0, 255).astype(np.uint8)
    d8 = cv2.applyColorMap(d8, cv2.COLORMAP_JET)
    pan.append(d8)
    
    grid = np.hstack([cv2.resize(p, (W//2, H//2)) for p in pan])
    cv2.imencode('.png', grid)[1].tofile(os.path.join(motion_dir, 'motion_compare.png'))
    
    report['selected'] = 'v1_subtle'
    report['params'] = VARIANTS['v1_subtle']
    json.dump(report, open(os.path.join(args.work, 'motion_report.json'), 'w'), indent=1)
    print('WROTE', motion_dir, '| Selected variant: v1_subtle', flush=True)

if __name__ == '__main__':
    main()
