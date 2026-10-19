# 1020 冷启动实测 v2: 清场(修 pid 遍历) -> 起新看门狗 -> 计时
import os, sys, time, subprocess
import psutil

VENV = r'C:\Speaktosi\engine\venv\Scripts'
PYW = os.path.join(VENV, 'pythonw.exe')
WD = r'C:\Speaktosi\engine\aiteacher_watchdog.py'
LOGF = r'C:\Speaktosi\engine\watchdog.log'
WATCH = (8010, 18091, 18092, 8011)
TARGET_PORTS = WATCH + (18999,)

me = os.getpid()
anc = set()
try:
    p = psutil.Process(me)
    while p:
        anc.add(p.pid)
        p = p.parent()
        if not p:
            break
except Exception:
    pass
print('[excluded self+ancestors] %s' % sorted(anc), flush=True)


def listening():
    out = {}
    try:
        for c in psutil.net_connections(kind='tcp'):
            try:
                if (c.status == psutil.CONN_LISTEN and c.laddr
                        and c.laddr.port in TARGET_PORTS and c.pid):
                    out[c.laddr.port] = c.pid
            except Exception:
                pass
    except Exception as e:
        print('net_connections fail: %s' % e, flush=True)
    return out


cur = listening()
print('[before] %s' % cur, flush=True)

victims = {}
for port, pid in cur.items():
    victims[pid] = 'port:%d' % port
    if pid in anc:
        continue
    try:
        par = psutil.Process(pid).parent()
        if par and 'python' in par.name().lower() and par.pid not in anc:
            victims[par.pid] = 'parent-of:%d' % pid
    except Exception:
        pass

print('[victims] %s' % victims, flush=True)
for pid in victims:
    if pid in anc:
        print('  SKIP self/anc %d' % pid, flush=True)
        continue
    try:
        pr = psutil.Process(pid)
        pr.kill()
        print('  killed %d %s' % (pid, pr.name()), flush=True)
    except Exception as e:
        print('  killfail %d %s' % (pid, e), flush=True)

time.sleep(8)
left = listening()
print('[after-kill] %s' % left, flush=True)
if left:
    print('!! 仍有残留, 中止以避免污染实测', flush=True)
    sys.exit(3)

try:
    n0 = sum(1 for _ in open(LOGF, encoding='utf-8', errors='replace'))
except Exception:
    n0 = 0

env = {k: v for k, v in os.environ.items()
       if k.upper() not in ('PYTHONPATH', 'PYTHONHOME', 'PYTHONSTARTUP',
                            'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY')}
t0 = time.time()
subprocess.Popen([PYW, WD], cwd=r'C:\Speaktosi\engine',
                 creationflags=0x00000200 | 0x08000000, close_fds=True, env=env)
print('[T0] watchdog relaunched at %s' % time.strftime('%H:%M:%S'), flush=True)

ready = {}
deadline = t0 + 300
while time.time() < deadline:
    el = time.time() - t0
    l = listening()
    for port in WATCH:
        if port not in ready and port in l:
            ready[port] = el
            print('  +%6.1fs  port %-6d LISTEN pid=%d' % (el, port, l[port]), flush=True)
    if len(ready) == len(WATCH):
        break
    time.sleep(2)

print('[READY] %s' % {k: round(v, 1) for k, v in sorted(ready.items())}, flush=True)
print('[TOTAL] %s   (旧版实测=129s)' % (('%.1fs' % max(ready.values())) if ready else 'NONE'), flush=True)

try:
    lines = open(LOGF, encoding='utf-8', errors='replace').read().splitlines()
    print('--- watchdog.log new lines ---')
    for ln in lines[n0:]:
        print(ln)
except Exception as e:
    print('log read fail %s' % e)