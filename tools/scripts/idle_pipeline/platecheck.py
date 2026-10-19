import os, sys, json
import numpy as np, cv2
from common import get_args

def main():
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    args = get_args()
    
    def im(p): return cv2.imdecode(np.fromfile(p, np.uint8), cv2.IMREAD_COLOR)
    
    a0 = im(args.art)
    PL = os.path.join(args.work, 'plates')
    pa = im(os.path.join(PL, 'art.png'))
    pm = im(os.path.join(PL, 'M.png'))
    px = im(os.path.join(PL, 'X.png'))
    
    d = np.abs(pa.astype(np.int32)-a0.astype(np.int32)).sum(2)
    ys, xs = np.where(d>6)
    
    report = {
        'smile_diff_px': int((d>6).sum()),
        'smile_max_diff': int(d.max()),
        'black_px': {
            'plate': int((pa.sum(2)==0).sum()),
            'art': int((a0.sum(2)==0).sum())
        }
    }
    
    print('smile diff px', report['smile_diff_px'])
    print('bbox x %d-%d y %d-%d'%(xs.min(),xs.max(),ys.min(),ys.max()) if len(xs) else 'none')
    print('max diff', report['smile_max_diff'])
    print('black px: plate', report['black_px']['plate'], 'art', report['black_px']['art'])
    
    def gpx(im0): return int(((im0[:,:,0]<16)&(im0[:,:,1]>240)&(im0[:,:,2]<16)).sum())
    
    report['green_px'] = {
        'art': gpx(a0), 'plate': gpx(pa), 'M': gpx(pm), 'X': gpx(px)
    }
    print('green px: art', gpx(a0), 'plate', gpx(pa), 'M', gpx(pm), 'X', gpx(px))
    
    report['diff_vs_plate'] = {}
    for nm, p in (('M', pm), ('X', px)):
        dd = np.abs(p.astype(np.int32)-pa.astype(np.int32)).sum(2)
        ys2, xs2 = np.where(dd>6)
        diff_px = int((dd>6).sum())
        report['diff_vs_plate'][nm] = diff_px
        print(nm, 'vs plate diff px', diff_px, 'bbox x %d-%d y %d-%d'%(xs2.min(),xs2.max(),ys2.min(),ys2.max()) if len(xs2) else 'none')
        
    lap = lambda z: float(cv2.Laplacian(cv2.cvtColor(z, cv2.COLOR_BGR2GRAY), cv2.CV_64F).var())
    sharp_a0 = lap(a0)
    sharp_pa = lap(pa)
    retain = 100.0 * sharp_pa / sharp_a0 if sharp_a0 > 0 else 0
    
    report['sharpness'] = {
        'art': sharp_a0,
        'plate': sharp_pa,
        'retain_pct': retain
    }
    print('sharpness art %.0f plate %.0f  (retain %.1f%%)' % (sharp_a0, sharp_pa, retain))
    
    json.dump(report, open(os.path.join(args.work, 'platecheck_report.json'), 'w'), indent=1)

if __name__ == '__main__':
    main()
