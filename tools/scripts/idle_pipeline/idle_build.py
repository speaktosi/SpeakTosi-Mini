import argparse, os, sys, subprocess, json, time

def run_step(cmd, desc):
    print(f"\n[{desc}]")
    t0 = time.time()
    try:
        subprocess.check_call(cmd, env={k: v for k, v in os.environ.items() if k not in ['PYTHONPATH', 'PYTHONHOME']})
    except subprocess.CalledProcessError as e:
        print(f"\n[Error] {desc} failed with exit code {e.returncode}")
        sys.exit(1)
    t1 = time.time()
    print(f"[{desc}] done in {t1-t0:.1f}s")
    return t1 - t0

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--avatar', required=True)
    parser.add_argument('--src', required=True)
    parser.add_argument('--fps', type=int, default=24)
    parser.add_argument('--frames', type=int, default=125)
    args = parser.parse_args()
    
    art = fr"C:\Speaktosi\engine\data\avatars\{args.avatar}\full_imgs\00000000.png"
    work = fr"C:\Speaktosi\coach_runs\idle_work\{args.avatar}"
    out = os.path.join(work, "final")
    
    if not os.path.exists(art):
        print(f"Error: Art image {art} not found.")
        sys.exit(1)
    if not os.path.exists(args.src):
        print(f"Error: Source video {args.src} not found.")
        sys.exit(1)
        
    python_exe = r"C:\Speaktosi\engine\venv\Scripts\python.exe"
    script_dir = os.path.dirname(os.path.abspath(__file__))
    
    base_args = [
        '--avatar', args.avatar,
        '--src', args.src,
        '--art', art,
        '--work', work,
        '--out', out,
        '--fps', str(args.fps),
        '--frames', str(args.frames)
    ]
    
    times = {}
    
    # 1. eyeframes
    times['eyeframes'] = run_step([python_exe, os.path.join(script_dir, 'eyeframes.py')] + base_args, "Eyeframes (ECC)")
    
    # 2. eyescan
    times['eyescan'] = run_step([python_exe, os.path.join(script_dir, 'eyescan.py')] + base_args, "Eyescan")
    
    # 3. plates
    times['plates'] = run_step([python_exe, os.path.join(script_dir, 'plates.py')] + base_args, "Plates")
    
    # 3b. blink_seq (v3 1010c): Brenda式真实眨眼帧序列 (失败则 render 自动回退单板斜坡)
    times['blink_seq'] = run_step([python_exe, os.path.join(script_dir, 'blink_seq.py')] + base_args, "BlinkSeq (真实眨眼序列)")
    
    # 4. motion
    times['motion'] = run_step([python_exe, os.path.join(script_dir, 'motion.py')] + base_args, "Motion")
    
    # 5. render
    times['render'] = run_step([python_exe, os.path.join(script_dir, 'render.py')] + base_args, "Render")
    
    # 6. checks
    times['platecheck'] = run_step([python_exe, os.path.join(script_dir, 'platecheck.py')] + base_args, "Platecheck")
    times['blinkcheck'] = run_step([python_exe, os.path.join(script_dir, 'blinkcheck.py')] + base_args, "Blinkcheck")
    times['perfcheck'] = run_step([python_exe, os.path.join(script_dir, 'perfcheck.py')] + base_args, "Perfcheck")
    times['eyegazecheck'] = run_step([python_exe, os.path.join(script_dir, 'eyegazecheck.py'),
                                      '--art', art, '--dir', out], "Eyegazecheck (眼睛出界)")
    times['blinkzone'] = run_step([python_exe, os.path.join(script_dir, 'blinkzone.py')] + base_args,
                                  "Blinkzone (眉颊稳定+闭合单调+双眼对称)")
    
    # Build master report
    print("\nBuilding master report...")
    master = {'times': times}
    
    def load_j(name):
        try: return json.load(open(os.path.join(work, name)))
        except: return {}
        
    eyescan = load_j('eyescan.json')
    render = load_j('render_report.json')
    platecheck = load_j('platecheck_report.json')
    blinkcheck = load_j('blinkcheck_report.json')
    perfcheck = load_j('perfcheck_report.json')
    
    master['blink_video_frames'] = {'VX': eyescan.get('VX'), 'VM': eyescan.get('VM')}
    master['blink_output_frames'] = render.get('blink', [])
    master['frames_rendered'] = render.get('frames')
    master['resolution'] = {'w': render.get('w'), 'h': render.get('h')}
    master['loop_seam'] = blinkcheck.get('loop_seam', {})
    
    master['checks'] = {
        'smile_max_diff': platecheck.get('smile_max_diff'),
        'sharpness_retain_pct': platecheck.get('sharpness', {}).get('retain_pct')
    }
    
    report_path = os.path.join(work, 'build_report.json')
    json.dump(master, open(report_path, 'w'), indent=2)
    print(f"Master report saved to {report_path}")
    print(json.dumps(master, indent=2))
    
    print(f"\nPipeline complete! Output frames are in {out}")
    print(f"To install, run: {python_exe} {os.path.join(script_dir, 'idle_install.py')} --avatar {args.avatar} --frames {out}")

if __name__ == '__main__':
    main()
