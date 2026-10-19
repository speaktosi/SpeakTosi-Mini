import asyncio, json, os, subprocess, time, urllib.request
import psutil, websockets

ELECTRON = r'C:\Speaktosi\app\node_modules\electron\dist\electron.exe'
APPDIR = r'C:\Speaktosi\app'
PACK = r'C:\Speaktosi'
PORTS = (8010, 8011, 18091, 18092)

def targets(port):
    try:
        return json.loads(urllib.request.urlopen('http://127.0.0.1:%d/json' % port, timeout=3).read().decode('utf-8', 'replace'))
    except Exception:
        return []

def listening():
    out = set()
    for c in psutil.net_connections(kind='tcp'):
        try:
            if c.status == 'LISTEN' and c.laddr and c.laddr.port in PORTS:
                out.add(c.laddr.port)
        except Exception:
            pass
    return out

def wd_up():
    for c in psutil.net_connections(kind='tcp'):
        try:
            if c.status == 'LISTEN' and c.laddr and c.laddr.port == 18999:
                return True
        except Exception:
            pass
    return False

def hbs():
    try:
        return [f for f in os.listdir(PACK) if f.startswith('.ui_alive_')]
    except Exception:
        return []

async def _eval(port, expr):
    tgt = None
    for t in targets(port):
        if 'overlay' in (t.get('url') or ''):
            tgt = t
            break
    if not tgt:
        return None
    async with websockets.connect(tgt['webSocketDebuggerUrl'], max_size=None) as ws:
        await ws.send(json.dumps({'id': 1, 'method': 'Runtime.evaluate',
                                  'params': {'expression': expr, 'returnByValue': True}}))
        while True:
            m = json.loads(await asyncio.wait_for(ws.recv(), timeout=8))
            if m.get('id') == 1:
                return m.get('result', {}).get('result', {}).get('value')

async def phase(port, label, tmax=240):
    p = subprocess.Popen([ELECTRON, '.', '--remote-debugging-port=%d' % port], cwd=APPDIR)
    t0 = time.time()
    print('[%s] electron pid=%d  %s' % (label, p.pid, time.strftime('%H:%M:%S')), flush=True)
    on, last = None, None
    while time.time() - t0 < tmax:
        s = await _eval(port, "(document.getElementById('stxt')||{}).textContent + ' ## ' + ((document.getElementById('status')||{}).className||'')")
        el = time.time() - t0
        if s and s != last:
            print('   +%5.1fs  %s' % (el, s), flush=True)
            last = s
        if s and s.startswith('桌面老师在线'):
            on = el
            break
        await asyncio.sleep(2.5)
    print('[%s] ONLINE=%s  心跳=%s' % (label, ('%.1fs' % on) if on else 'TIMEOUT', hbs()), flush=True)
    return p, on, port

async def main():
    print('########## A: 打开程序 ##########', flush=True)
    p, onA, portA = await phase(9333, 'A')
    print('  [退出前] 心跳=%s 端口=%s watchdog=%s' % (hbs(), sorted(listening()), wd_up()), flush=True)
    r = await _eval(portA, "(window.teacher && window.teacher.quit) ? (window.teacher.quit(), 'quit-sent') : 'no-api'")
    print('  优雅退出(window.teacher.quit) -> %s' % r, flush=True)
    await asyncio.sleep(12)
    al = [q.pid for q in psutil.process_iter() if q.name().lower().startswith('electron')]
    print('  [退出后] electron残留=%s 心跳=%s 端口=%s watchdog=%s' % (al, hbs(), sorted(listening()), wd_up()), flush=True)

    print('########## B: 再次打开程序 ##########', flush=True)
    p2, onB, portB = await phase(9334, 'B')
    await asyncio.sleep(1)
    r2 = await _eval(portB, "(window.teacher && window.teacher.quit) ? (window.teacher.quit(), 'quit-sent') : 'no-api'")
    print('  收尾退出 -> %s' % r2, flush=True)
    await asyncio.sleep(8)
    print('########## 汇总 ##########', flush=True)
    print('  A 打开耗时 %s ; B 再次打开耗时 %s ; 服务栈仍存活=%s' % (
        ('%.1fs' % onA) if onA else 'TIMEOUT', ('%.1fs' % onB) if onB else 'TIMEOUT', sorted(listening())), flush=True)

asyncio.run(main())