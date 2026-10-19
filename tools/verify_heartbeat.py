import asyncio, json, os, subprocess, time, urllib.request
import psutil, websockets

ELECTRON = r'C:\Speaktosi\app\node_modules\electron\dist\electron.exe'
APPDIR = r'C:\Speaktosi\app'
ENG = r'C:\Speaktosi\engine'
PACK = r'C:\Speaktosi'

def hb(d):
    try:
        return [f for f in os.listdir(d) if f.startswith('.ui_alive_')]
    except Exception:
        return []

def up(port):
    import socket
    s = socket.socket()
    s.settimeout(1)
    r = s.connect_ex(('127.0.0.1', port)) == 0
    s.close()
    return r

async def _eval(port, expr):
    try:
        ts = json.loads(urllib.request.urlopen('http://127.0.0.1:%d/json' % port, timeout=3).read().decode('utf-8', 'replace'))
    except Exception:
        return None
    tgt = next((t for t in ts if 'overlay' in (t.get('url') or '')), None)
    if not tgt:
        return None
    async with websockets.connect(tgt['webSocketDebuggerUrl'], max_size=None) as ws:
        await ws.send(json.dumps({'id': 1, 'method': 'Runtime.evaluate',
                                  'params': {'expression': expr, 'returnByValue': True}}))
        while True:
            m = json.loads(await asyncio.wait_for(ws.recv(), timeout=8))
            if m.get('id') == 1:
                return m.get('result', {}).get('result', {}).get('value')

async def main():
    p = subprocess.Popen([ELECTRON, '.', '--remote-debugging-port=9335'], cwd=APPDIR)
    print('electron pid=%d %s' % (p.pid, time.strftime('%H:%M:%S')), flush=True)
    await asyncio.sleep(16)
    print('[运行中] engine/心跳=%s  包根/心跳=%s  8765 up=%s' % (hb(ENG), hb(PACK), up(8765)), flush=True)
    r = await _eval(9335, "(window.teacher && window.teacher.quit) ? (window.teacher.quit(), 'quit-sent') : 'no-api'")
    print('优雅退出 -> %s' % r, flush=True)
    await asyncio.sleep(10)
    print('[退出后] engine/心跳=%s  8765 up=%s  electron残留=%s'
          % (hb(ENG), up(8765), [q.pid for q in psutil.process_iter() if q.name().lower().startswith('electron')]), flush=True)

asyncio.run(main())