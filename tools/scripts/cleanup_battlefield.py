# -*- coding: utf-8 -*-
"""战场清理：只删本轮已验证的冗余件。大模型缓存不动（列给用户拍板）。"""
import os, glob, shutil, subprocess
BS = chr(92)
freed = [0]
def rm(f, why):
    if not os.path.exists(f): return
    try:
        if os.path.isfile(f):
            sz = os.path.getsize(f); os.remove(f)
        else:
            sz = sum(os.path.getsize(os.path.join(r, x)) for r, _, fs in os.walk(f) for x in fs)
            shutil.rmtree(f)
        freed[0] += sz
        print('  DEL %9d  %-28s [%s]' % (sz, os.path.basename(f), why))
    except Exception as e:
        print('  SKIP %s -> %s' % (f, e))

W = r'C:\Users\owenlee\hermes_scripts\aiteacher'
AG = r'D:\Software\ai-girlfriend'
LT = r'D:\Software\LiveTalking'

print('== 1) aiteacher 工作目录取证媒体 ==')
for p in ['_probe_cleanup.py', 'aiteacher_bust_v2.png', 'chat_frame.png',
          'livetalk_frame0.png', 'livetalk_frame1.png', 'livetalk_frame2.png',
          'mouth_calib_check.png', 'clone_hello.wav', 'clone_nuance.wav',
          'clone_q3.wav', 'ref_article.html']:
    rm(os.path.join(W, p), 'evidence-seen')

print('== 2) ai-girlfriend 旧渲染件/备份/旧日志 ==')
refs = ''
for src in glob.glob(AG + BS + 'renderer' + BS + '*.js') + glob.glob(AG + BS + 'renderer' + BS + '*.html'):
    try:
        refs += open(src, encoding='utf-8', errors='ignore').read()
    except OSError:
        pass
for f in glob.glob(AG + BS + '*.png') + glob.glob(AG + BS + '*.jpg'):
    b = os.path.basename(f)
    if b == 'teacher_bust.png':
        continue
    if os.path.isfile(f) and b not in refs:
        rm(f, 'unreferenced')
for p in [r'backend\server.py.bak-review-1001', 'backend_run.log', 'electron_run.log']:
    rm(os.path.join(AG, p), 'verified-backup')
rm(os.path.join(LT, r'启动SpeakTosi直播口型.bat'), 'superseded-by-watchdog')
rm(os.path.join(LT, 'watchdog_boot.log'), 'empty-boot-log')

print('== 3) 包缓存(可再生) ==')
for cmd in ['pip cache purge', 'uv cache clean']:
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=300)
        out = (r.stdout or r.stderr or '').strip().splitlines()
        print('  %s -> %s' % (cmd, out[-1] if out else 'done'))
    except Exception as e:
        print('  %s -> %s' % (cmd, e))

print('== 总计(仅直接删除部分) %.1f MB ==' % (freed[0] / 1048576))
