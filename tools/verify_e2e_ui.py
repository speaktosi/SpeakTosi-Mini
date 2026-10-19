# 1020 端到端实测: A)热态秒连  B)冷启动"老师准备中"->在线 (CDP 读 overlay 真实DOM)
import asyncio, json, os, subprocess, sys, time, urllib.request
import psutil
import websockets

ELECTRON = r'C:\Speaktosi\app\node_modules\electron\dist\electron.exe'
APPDIR = r'C:\Speaktosi\app'
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


def listen_map():
    out = {}
    for c in psutil.net_connections(kind='tcp'):
        try:
            if c.status == psutil.CONN_LISTEN and c.laddr and c.laddr.port in PORTS and c.pid:
                out[c.laddr.port] = c.pid
        except Exception:
            pass
    return out


def kill_tree(pid):
    if pid in anc:
        return 0
    n = 0
    try:
        pr = psutil.Process(pid)
        for ch in pr.children(recursive=True):
            try:
                ch.kill(); n += 1
            except Exception:
                pass
        pr.kill(); n += 1
    except Exception:
        pass
    return n


def wipe_services():
    m = listen_map()
    killed = []
    for port, pid in m.items():
        killed.append('%s(%d)' % (PORTS.get(port, port), pid))
        kill_tree(pid)
        try:
            par = psutil.Process(pid).parent()
            if par and 'python' in par.name().lower():
                kill_tree(par.pid)
        except Exception:
            pass
    time.sleep(6)
    return killed, listen_map()


def cdp_targets(port):
    try:
        raw = urllib.request.urlopen('http://127.0.0.1:%d/json' % port, timeout=3).read().decode()
        return json.loads(raw)
    except Exception:
        return []


async def read_overlay(port):
    for t in cdp_targets(port):
        u = t.get('url') or ''
        if 'overlay' in u and t.get('webSocketDebuggerUrl'):
            try:
                async with websockets.connect(t['webSocketDebuggerUrl'], max_size=None) as ws:
                    expr = ("(document.getElementById('stxt')||{}).textContent + ' ## ' + "
                            "((document.getElementById('status')||{}).className||'')")
                    await ws.send(json.dumps({'id': 1, 'method': 'Runtime.evaluate',
                                              'params': {'expression': expr, 'returnByValue': True}}))
                    while True:
                        m = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
                        if m.get('id') == 1:
                            return (m.get('result', {}).get('result', {}) or {}).get('value')
            except Exception as e:
                return 'CDP-ERR %s' % e
    return None


async def phase(tag, cdp_port, cold, budget):
    if cold:
        print('[%s] 清场...' % tag, flush=True)
        killed, left = wipe_services()
        print('   killed: %s   remaining: %s' % (killed, left), flush=True)
    t0 = time.time()
    env = {k: v for k, v in os.environ.items()
           if k.upper() not in ('PYTHONPATH', 'PYTHONHOME', 'PYTHONSTARTUP')}
    p = subprocess.Popen([ELECTRON, '.', '--remote-debugging-port=%d' % cdp_port],
                         cwd=APPDIR, env=env,
                         stdout=open(r'C:\Speaktosi\tools\_e2e_%d.out' % cdp_port, 'ab'),
                         stderr=subprocess.STDOUT)
    print('[%s] electron pid=%d  %s' % (tag, p.pid, time.strftime('%H:%M:%S')), flush=True)
    seen = []
    online = None
    while time.time() - t0 < budget:
        el = time.time() - t0
        st = await read_overlay(cdp_port)
        if st is None:
            if el > 3 and not seen:
                ts = cdp_targets(cdp_port)
                print('   +%5.1fs  (no overlay target yet; targets=%s)' %
                      (el, [(x.get('type'), (x.get('url') or '')[-28:]) for x in ts]), flush=True)
        else:
            if not seen or seen[-1][1] != st:
                seen.append((el, st))
                print('   +%5.1fs  %s' % (el, st), flush=True)
            if '在线' in st and online is None:
                online = el
                break
        await asyncio.sleep(2.5)
    print('[%s] %s' % (tag, ('ONLINE at %.1fs' % online) if online else ('timeout %.0fs' % budget)), flush=True)
    # 收尾: 关 electron(不关服务栈, 便于判断)
    kill_tree(p.pid)
    await asyncio.sleep(2)
    return seen, online


async def main():
    print('########## PHASE A: 热态(服务已在跑) 打开程序 ##########', flush=True)
    a, aon = await phase('A-热态', 9333, cold=False, budget=40)
    print('########## PHASE B: 冷启动(清场后) 打开程序 ##########', flush=True)
    b, bon = await phase('B-冷启动', 9334, cold=True, budget=200)
    print('########## 汇总 ##########', flush=True)
    print('A 热态: %s' % ('在线 %.1fs' % aon if aon else '未连上'), flush=True)
    print('A 文案轨迹: %s' % [(round(x, 1), s) for x, s in a], flush=True)
    print('B 冷启动: %s' % ('在线 %.1fs' % bon if bon else '未连上'), flush=True)
    print('B 文案轨迹: %s' % [(round(x, 1), s) for x, s in b], flush=True)


asyncio.run(main())