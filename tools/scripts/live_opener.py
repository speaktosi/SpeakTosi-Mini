# -*- coding: utf-8 -*-
"""SpeakTosi 直播控制台一键开启器（纯Python,零bat乱码风险）。
流程: 确保看门狗在跑 -> 等 8011+18091 就绪(最多5min) -> 打开浏览器。
任何服务挂了不用管: 看门狗会自动拉起。"""
import os, sys, time, socket, webbrowser, subprocess

# 1002整合包: 包根自动探测(此脚本在 tools/scripts/ 下, 上溯两级=包根); 探测失败回退开发机旧路径
_PACK = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ENGINE = os.path.join(_PACK, 'engine') if os.path.isdir(os.path.join(_PACK, 'engine')) else r'D:\Software\LiveTalking'

def port_open(port):
    s = socket.socket(); s.settimeout(1)
    try: return s.connect_ex(('127.0.0.1', port)) == 0
    finally: s.close()

def lock_held(port=18999):
    s = socket.socket()
    try:
        s.bind(('127.0.0.1', port)); return False
    except OSError:
        return True
    finally:
        s.close()

# 1) 看门狗不在就跑一个（pythonw 无窗, venv 随包）
if not lock_held():
    py = os.path.join(ENGINE, 'venv', 'Scripts', 'pythonw.exe')
    script = os.path.join(ENGINE, 'aiteacher_watchdog.py')
    env = {k: v for k, v in os.environ.items()
           if k.upper() not in ('PYTHONPATH', 'PYTHONHOME', 'PYTHONHOMEW')}
    env['PYTHONIOENCODING'] = 'utf-8'
    subprocess.Popen([py, script], cwd=ENGINE, env=env,
                     creationflags=0x00000008)  # DETACHED
    print('[opener] watchdog launched')

# 2) 等就绪
deadline = time.time() + 300
while time.time() < deadline:
    if port_open(18091) and port_open(8011):
        break
    time.sleep(3)
ok_tts, ok_lt = port_open(18091), port_open(8011)
print('[opener] tts(18091)=%s  livetalking(8011)=%s' % (ok_tts, ok_lt))

# 3) 打开直播 UI
url = 'http://127.0.0.1:8011/index.html'
webbrowser.open(url)
print('[opener] opened ' + url)
if ok_lt and ok_tts:
    print('[opener] ALL READY - 点 Connect 后在文本框选 Chat LLM 即可对话')
    sys.exit(0)
print('[opener] 部分服务未就绪, 看门狗仍在自愈, 稍后刷新页面即可')
sys.exit(0)
