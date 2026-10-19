# -*- coding: utf-8 -*-
"""清旧看门狗 -> 重启新看门狗 -> 等端口回来。路径从脚本位置推导(不再写死旧安装路径)."""
import os, subprocess, sys, time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ENG = os.path.join(ROOT, 'engine')
PYW = os.path.join(ENG, 'venv', 'Scripts', 'pythonw.exe')
WD = os.path.join(ENG, 'aiteacher_watchdog.py')


def ps(cmd, t=60):
    r = subprocess.run(['powershell', '-NoProfile', '-Command', cmd], capture_output=True, text=True, timeout=t)
    return (r.stdout + r.stderr).strip()


def listeners():
    out = subprocess.run(['netstat', '-ano'], capture_output=True, text=True).stdout
    d = {}
    for ln in out.splitlines():
        f = ln.split()
        if len(f) >= 5 and f[3] == 'LISTENING' and f[1].startswith('127.0.0.1:'):
            d[int(f[1].split(':')[1])] = f[4]
    return d


# 1) 杀旧看门狗(仅 python/pythonw, 避免自匹配)
old = ps("Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*aiteacher_watchdog*' "
         "-and ($_.Name -eq 'python.exe' -or $_.Name -eq 'pythonw.exe') } | Select-Object -ExpandProperty ProcessId")
for pid in [x.strip() for x in old.splitlines() if x.strip().isdigit()]:
    ps("Stop-Process -Id %s -Force -ErrorAction SilentlyContinue" % pid)
    print('killed old watchdog', pid)

# 2) 起新看门狗
qs = ("$env:PYTHONPATH=$null; $env:PYTHONHOME=$null; $env:PYTHONIOENCODING='utf-8'; "
      "Start-Process -FilePath '%s' -ArgumentList '%s' -WorkingDirectory '%s' -WindowStyle Hidden" % (PYW, WD, ENG))
ps(qs)
print('launched new watchdog')

# 3) 等端口(8011/8010/18080)回来
t0 = time.time()
while time.time() - t0 < 420:
    ls = listeners()
    if all(p in ls for p in (8011, 8010, 18080)):
        print('ALL UP after %ds: %s' % (int(time.time() - t0), {p: ls[p] for p in (8010, 8011, 18080)}))
        sys.exit(0)
    time.sleep(15)
print('TIMEOUT;现在监听: %s' % listeners())
sys.exit(3)
