import argparse, os, sys

def get_args():
    parser = argparse.ArgumentParser()
    parser.add_argument('--avatar', required=True)
    parser.add_argument('--src', required=True)
    parser.add_argument('--art', default='')
    parser.add_argument('--work', default='')
    parser.add_argument('--out', default='')
    parser.add_argument('--fps', type=int, default=24)
    parser.add_argument('--frames', type=int, default=125)
    args = parser.parse_args()
    
    if not args.art:
        args.art = fr"C:\Speaktosi\engine\data\avatars\{args.avatar}\full_imgs\00000000.png"
    if not args.work:
        args.work = fr"C:\Speaktosi\coach_runs\idle_work\{args.avatar}"
    if not args.out:
        args.out = os.path.join(args.work, "final")
        
    os.makedirs(args.work, exist_ok=True)
    os.makedirs(args.out, exist_ok=True)
    return args
