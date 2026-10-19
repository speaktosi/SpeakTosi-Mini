import argparse, os, sys, glob, shutil, time

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--avatar', required=True)
    parser.add_argument('--frames', required=True)
    args = parser.parse_args()
    
    E = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))), 'engine', 'data')
    dst = os.path.join(E, 'customvideo', f"{args.avatar}_idle")
    
    if os.path.exists(dst):
        # 只保留最近一次备份：先清旧的（避免反复重跑堆积 87MB 级目录）
        for old in glob.glob(dst + "_bak_*"):
            shutil.rmtree(old, ignore_errors=True)
        bak = dst + f"_bak_{int(time.time())}"
        print(f"Backing up existing {dst} to {bak}")
        shutil.move(dst, bak)
        
    os.makedirs(dst, exist_ok=True)
    
    src_frames = sorted(glob.glob(os.path.join(args.frames, '*.png')))
    print(f"Found {len(src_frames)} frames in {args.frames}")
    if not src_frames:
        print("Error: No frames found to install.")
        sys.exit(1)
        
    for i, p in enumerate(src_frames):
        shutil.copyfile(p, os.path.join(dst, f"{i:04d}.png"))
        
    cfg = os.path.join(E, f'custom_config_{args.avatar}.json')
    if os.path.exists(cfg):
        cfg_bak = cfg + f".bak_{int(time.time())}"
        shutil.copyfile(cfg, cfg_bak)
        
    cfg_content = f"""[
 {{
  "audiotype": 1,
  "imgpath": "data/customvideo/{args.avatar}_idle"
 }}
]
"""
    with open(cfg, 'w', encoding='utf-8') as f:
        f.write(cfg_content)
        
    print(f"Config written to {cfg}")
    print(f"Installed {len(glob.glob(os.path.join(dst, '*.png')))} frames successfully.")

if __name__ == '__main__':
    main()
