# 1020 VRAM 归属探针: 停看门狗 -> 逐个杀服务测 GPU delta -> 重启看门狗恢复
import os, sys, time, json, subprocess, urllib.request
import psutil

WD = r'C:\Speaktosi\engine\aiteacher_watchdog.py'
PYW = r'C:\Speaktosi\engine\venv\Scripts\pythonw.exe'
PORTS = {8011: 'musetalk', 18091: 'lx_tts', 18092: 'lx_tts2', 18999: 'watchdog'}

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


def gpu_used():
    try:
        o = subprocess.run(['nvidia-smi', '--query-gpu=memory.used', '--format=csv,noheader,nounits'],
                           capture_output=True, text=True, timeout=15).stdout
        return int(o.strip().split('\n')[0])
    except Exception as e:
        return -1


def listen_map():
    out = {}
    for c in psutil.net_connections(kind='tcp'):
        try:
            if c.status == psutil.CONN_LISTEN and c.laddr and c.laddr.port in PORTS and c.pid:
                out[c.laddr.port] = c.pid
        except Exception:
            pass
    return out


def kill_pid(pid):
    if pid in anc:
        return 'SKIP-self'
    try:
        pr = psutil.Process(pid)
        nm = pr.name()
        pr.kill()
        return 'killed %s' % nm
    except Exception as e:
        return 'fail %s' % e


def kill_port(port):
    m = listen_map()
    pid = m.get(port)
    if not pid:
        return 'not-listening'
    r = kill_pid(pid)
    try:
        par = psutil.Process(pid).parent()
        if par and 'python' in par.name().lower():
            kill_pid(par.pid)
    except Exception:
        pass
    return r


# ---------- 1) 真实合成测试(双口) ----------
def tts_test(port, voice='arthur', text='你好，我是你的Arthur 老师，今天我们一起学习。'):
    body = json.dumps({'input': text, 'voice': voice}).encode()
    req = urllib.request.Request('http://127.0.0.1:%d/v1/audio/speech' % port, data=body,
                                 headers={'Content-Type': 'application/json'})
    t = time.time()
    try:
        r = urllib.request.urlopen(req, timeout=90)
        data = r.read()
        return 'OK %d bytes in %.2fs (%.2fs audio)' % (len(data), time.time() - t, max(len(data) - 44, 0) / 2 / 24000.0)
    except Exception as e:
        return 'FAIL %s' % e


print('=== [1] 真实合成测试 ===', flush=True)
for port in (18091, 18092):
    print('  TTS %d: %s' % (port, tts_test(port)), flush=True)

print('=== [2] 引擎 HTTP ===', flush=True)
for port in (8011, 8011):
    try:
        r = urllib.request.urlopen(urllib.request.Request('http://127.0.0.1:%d/' % port,
                                  headers={'User-Agent': 'probe'}), timeout=10)
        print('  engine %d: HTTP %d' % (port, r.status), flush=True)
    except urllib.error.HTTPError as e:
        print('  engine %d: HTTP %d (alive)' % (port, e.code), flush=True)
    except Exception as e:
        print('  engine %d: %s' % (port, e), flush=True)

base = gpu_used()
print('=== [3] VRAM delta (baseline=%d MiB) ===' % base, flush=True)

# ---------- 停看门狗(避免它自动重拉干扰测量) ----------
print('  stop watchdog: %s' % kill_port(18999), flush=True)
time.sleep(4)

deltas = {}
prev = gpu_used()
for port in (8011, 18091, 18092, 8011):
    r = kill_port(port)
    time.sleep(7)
    now = gpu_used()
    deltas[PORTS[port]] = prev - now
    print('  kill %-9s -> %-14s  GPU %d -> %d  (delta %+d MiB)' % (PORTS[port], r, prev, now, prev - now), flush=True)
    prev = now

print('=== [4] 结果 ===', flush=True)
print('  baseline(all up)=%d MiB' % base, flush=True)
for k, v in deltas.items():
    print('  %-9s = %+5d MiB' % (k, v), flush=True)
stack = sum(v for k, v in deltas.items() if v > 0)
print('  ---- SpeakTosi 栈合计 ≈ %d MiB (%.2f GB)' % (stack, stack / 1024.0), flush=True)
print('  ---- 去 QQ 后全栈 = %d MiB; 若换 12G 卡余量 = %d MiB' % (stack, 12288 - stack), flush=True)

# ---------- 重启看门狗恢复 ----------
env = {k: v for k, v in os.environ.items()
       if k.upper() not in ('PYTHONPATH', 'PYTHONHOME', 'PYTHONSTARTUP',
                            'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY')}
t0 = time.time()
subprocess.Popen([PYW, WD], cwd=r'C:\Speaktosi\engine',
                 creationflags=0x00000200 | 0x08000000, close_fds=True, env=env)
print('=== [5] 看门狗重启, 等待恢复 ===', flush=True)
ready = set()
while time.time() - t0 < 240:
    m = listen_map()
    for port in (8011, 18091, 18092, 8011):
        if port in m and port not in ready:
            ready.add(port)
            print('  +%6.1fs %d up' % (time.time() - t0, port), flush=True)
    if len(ready) == 4:
        break
    time.sleep(2)
print('  [restored] %s  total %.1fs  GPU=%d MiB' % (sorted(ready), time.time() - t0, gpu_used()), flush=True)
print('  TTS recheck 18091: %s' % tts_test(18091, 'arthur', '恢复后自检。'), flush=True)